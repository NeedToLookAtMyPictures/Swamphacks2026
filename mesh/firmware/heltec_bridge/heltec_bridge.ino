// Dual-radio (LoRa + ESP-NOW) <-> USB serial bridge for trailmesh (see trailmesh/heltec_serial.py).
//
// Board: Heltec WiFi LoRa 32 V3 and its clones (ESP32-S3 + SX1262, CP2102 USB-UART).
// Needs Arduino-ESP32 core 3.x and RadioLib 7.x (Arduino Library Manager: "RadioLib").
//
// The ESP32 only moves frames between the two radios and the host; all protocol
// logic, including when to use which radio, lives in Python. Both radios drop
// frames whose first 4 bytes (sender device id) aren't the current partner.
//
//   LoRa (SX1262, sub-GHz): long range, slow. Every frame is a broadcast. Raw
//     LoRa has no listen-before-talk, so before each send this sketch checks
//     that no packet is arriving and runs channel activity detection (CAD).
//     Sends are queued, since a frame takes 0.1-1 s on air.
//   ESP-NOW (the ESP32's 2.4 GHz Wi-Fi radio): ~60 m, fast. Same code as
//     firmware/espnow_bridge. Python turns it on only for an exchange with a
//     close peer.
//
// Host protocol: espnow_bridge's commands (SEND / RADIO / RECV are ESP-NOW),
// plus CONFIG, LORA_SEND, LORA_RADIO and LORA_RECV. The board resets when the
// host opens the port (CP2102 auto-reset), so it announces itself with an INFO
// at boot and the host re-sends its state.

#include <Arduino.h>
#include <RadioLib.h>
#include <SPI.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <esp_now.h>
#include <esp_wifi.h>

// Heltec V3 wiring of the SX1262
static const int PIN_NSS = 8, PIN_SCK = 9, PIN_MOSI = 10, PIN_MISO = 11, PIN_RST = 12, PIN_BUSY = 13, PIN_DIO1 = 14;
static const float TCXO_VOLTAGE = 1.8;  // the V3's SX1262 runs from a TCXO powered by DIO3

static const uint32_t BAUD = 115200;     // CP2102 bridge: must match HeltecSerialLink
static const uint8_t MAX_PAYLOAD = 250;  // trailmesh frame limit (ESP-NOW v1; the SX1262 allows 255)
static const uint8_t ESPNOW_CHANNEL = 6;  // must match on all boards (same as espnow_bridge)
static const uint8_t SYNC_WORD = 0x24;   // not LoRaWAN (0x34), Meshtastic (0x2B) or RadioLib's default (0x12)
static const uint16_t PREAMBLE = 8;      // must match LoRaParams.preamble
static const uint8_t TX_QUEUE_LEN = 16;
static const uint8_t MAX_DEFERRALS = 8;  // after this many busy-channel checks, send anyway

enum : uint8_t {
  CMD_SEND = 0x01, CMD_SET_PARTNER = 0x02, CMD_RADIO = 0x03, CMD_GET_INFO = 0x04,  // as espnow_bridge
  CMD_CONFIG = 0x05, CMD_LORA_SEND = 0x06, CMD_LORA_RADIO = 0x07,
  EVT_RECV = 0x81, EVT_INFO = 0x84, EVT_LOG = 0x85, EVT_LORA_RECV = 0x86,
};

static const uint8_t SLIP_END = 0xC0, SLIP_ESC = 0xDB, SLIP_ESC_END = 0xDC, SLIP_ESC_ESC = 0xDD;
static const uint8_t BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

// Host-settable LoRa settings (layout must match CONFIG in trailmesh/lora.py).
struct __attribute__((packed)) RadioConfig {
  uint32_t freqHz;
  uint32_t bwHz;
  uint8_t sf;
  uint8_t cr;  // coding rate 4/cr
  int8_t powerDbm;
};

// INFO reply: ESP-NOW part (same layout as espnow_bridge, STATUS in espnow_serial.py),
// then the LoRa part (LORA_STATUS in heltec_serial.py).
struct __attribute__((packed)) EspNowStatus {
  uint8_t radioOn;
  uint8_t channel;
  int32_t lastErr;  // esp_err_t of the most recent failure, 0 if none
  uint32_t txOk, txErr, rx, rxFiltered, rxDropped;
};
struct __attribute__((packed)) LoRaStatus {
  uint8_t radioOn;
  int32_t lastErr;  // RadioLib status code of the most recent failure, 0 if none
  uint32_t txOk, txErr, rx, rxFiltered;
  RadioConfig cfg;
  uint8_t txQueued;
  uint32_t airtimeMs;  // total time spent transmitting
  uint32_t deferrals;  // sends postponed because the channel was busy
  uint32_t rxCrcErr;
};

static EspNowStatus en = {};
static LoRaStatus lr = {};
static RadioConfig cfg = {915000000, 500000, 7, 5, 20};
static volatile uint32_t partner = 0;  // 0 = accept everyone; applies to both radios
static bool textMode = false;  // a human typed a command; print instead of SLIP

// ---- serial framing -------------------------------------------------------

static void slipPutByte(uint8_t b) {
  if (b == SLIP_END) { Serial.write(SLIP_ESC); Serial.write(SLIP_ESC_END); }
  else if (b == SLIP_ESC) { Serial.write(SLIP_ESC); Serial.write(SLIP_ESC_ESC); }
  else Serial.write(b);
}

static void sendToHost(uint8_t evt, const uint8_t *a, size_t alen, const uint8_t *b = nullptr, size_t blen = 0,
                       const uint8_t *c = nullptr, size_t clen = 0) {
  Serial.write(SLIP_END);
  slipPutByte(evt);
  for (size_t i = 0; i < alen; i++) slipPutByte(a[i]);
  for (size_t i = 0; i < blen; i++) slipPutByte(b[i]);
  for (size_t i = 0; i < clen; i++) slipPutByte(c[i]);
  Serial.write(SLIP_END);
}

static void logHost(const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (textMode) { Serial.printf("LOG %s\r\n", buf); return; }
  sendToHost(EVT_LOG, (const uint8_t *)buf, n < 0 ? 0 : min((size_t)n, sizeof(buf) - 1));
}

static void printPayload(const uint8_t *data, size_t len) {
  for (size_t i = 0; i < len; i++) Serial.write(isprint(data[i]) ? data[i] : '.');
  Serial.println();
}

// ---- ESP-NOW (as in espnow_bridge) -----------------------------------------

struct RxItem {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[MAX_PAYLOAD];
};
static QueueHandle_t enQueue;
static volatile uint32_t enRx = 0, enFiltered = 0, enDropped = 0;  // written in the Wi-Fi task

static bool enFail(esp_err_t err, const char *what) {
  en.lastErr = err;
  logHost("ESP-NOW: %s failed: %s (0x%x)", what, esp_err_to_name(err), err);
  return false;
}

// Runs in the Wi-Fi task: copy and return immediately.
static void onEspNowRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len < 4 || len > MAX_PAYLOAD) return;
  enRx++;
  uint32_t sender;
  memcpy(&sender, data, 4);  // little-endian, same as the Python side
  uint32_t p = partner;
  if (p != 0 && sender != p) { enFiltered++; return; }
  RxItem item;
  memcpy(item.mac, info->src_addr, 6);
  item.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  item.len = len;
  memcpy(item.data, data, len);
  if (xQueueSend(enQueue, &item, 0) != pdTRUE) enDropped++;  // host is falling behind
}

static bool ensurePeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_err_t err = esp_now_add_peer(&peer);
  if (err == ESP_ERR_ESPNOW_FULL) {
    // Peer table holds ~20 entries. Evict any unicast peer to make room.
    esp_now_peer_info_t old;
    for (bool first = true; esp_now_fetch_peer(first, &old) == ESP_OK; first = false) {
      if (memcmp(old.peer_addr, BROADCAST, 6) != 0) { esp_now_del_peer(old.peer_addr); break; }
    }
    err = esp_now_add_peer(&peer);
  }
  return err == ESP_OK || enFail(err, "esp_now_add_peer");
}

static bool espnowStart() {
  if (en.radioOn) return true;
  WiFi.mode(WIFI_STA);
  uint32_t t0 = millis();
  while (!WiFi.STA.started()) {
    if (millis() - t0 > 3000) return enFail(ESP_ERR_TIMEOUT, "wifi STA start");
    delay(10);
  }
  esp_err_t err = esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) return enFail(err, "esp_wifi_set_channel");
  if ((err = esp_now_init()) != ESP_OK) return enFail(err, "esp_now_init");
  esp_now_register_recv_cb(onEspNowRecv);
  if (!ensurePeer(BROADCAST)) {
    esp_now_deinit();
    return false;
  }
  en.radioOn = 1;
  logHost("ESP-NOW on, channel %u", ESPNOW_CHANNEL);
  return true;
}

static void espnowStop() {
  if (!en.radioOn) return;
  esp_now_deinit();  // also clears the peer table; peers are re-added on demand
  WiFi.mode(WIFI_OFF);
  en.radioOn = 0;
}

static void espnowSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  esp_err_t err = ESP_ERR_INVALID_STATE;
  if (en.radioOn && len <= MAX_PAYLOAD && ensurePeer(mac)) {
    // The ESP-NOW tx queue is small; when the host sends a burst, wait briefly for room.
    for (int tries = 0; tries < 20; tries++) {
      err = esp_now_send(mac, data, len);
      if (err != ESP_ERR_ESPNOW_NO_MEM) break;
      delay(1);
    }
  }
  if (err == ESP_OK) { en.txOk++; return; }
  en.txErr++;
  en.lastErr = err;
  if ((en.txErr & (en.txErr - 1)) == 0)  // 1st, 2nd, 4th, 8th... so it doesn't flood
    logHost("ESP-NOW send failed (%lu so far): %s%s", (unsigned long)en.txErr, esp_err_to_name(err),
            en.radioOn ? "" : " (radio is off)");
}

static void espnowForward() {
  RxItem item;
  while (xQueueReceive(enQueue, &item, 0) == pdTRUE) {
    if (textMode) {
      Serial.printf("ESPNOW RX %02x:%02x:%02x:%02x:%02x:%02x %d ", item.mac[0], item.mac[1], item.mac[2],
                    item.mac[3], item.mac[4], item.mac[5], item.rssi);
      printPayload(item.data, item.len);
      continue;
    }
    uint8_t head[7];
    memcpy(head, item.mac, 6);
    head[6] = (uint8_t)item.rssi;
    sendToHost(EVT_RECV, head, 7, item.data, item.len);
  }
}

// ---- LoRa -----------------------------------------------------------------

SX1262 radio = new Module(PIN_NSS, PIN_DIO1, PIN_RST, PIN_BUSY, SPI);
static bool loraReady = false;  // begin() succeeded with the current cfg
enum LoRaState : uint8_t { L_OFF, L_RX, L_TX };
static LoRaState lstate = L_OFF;
static volatile bool dio1Flag = false;

struct TxItem {
  uint8_t len;
  uint8_t data[MAX_PAYLOAD];
};
static TxItem txQueue[TX_QUEUE_LEN];
static uint8_t txHead = 0, txCount = 0;
static uint32_t nextTxAt = 0;  // millis() before which we don't try to send (backoff)
static uint8_t deferralsInARow = 0;
static uint32_t frameAirMs = 100;  // airtime of a full frame with the current cfg

static bool loraFail(int16_t err, const char *what) {
  lr.lastErr = err;
  logHost("LoRa: %s failed: RadioLib error %d", what, err);
  return false;
}

ICACHE_RAM_ATTR static void onDio1() { dio1Flag = true; }

static bool configValid(const RadioConfig &c) {
  return c.freqHz >= 863000000 && c.freqHz <= 928000000 &&
         (c.bwHz == 125000 || c.bwHz == 250000 || c.bwHz == 500000) &&
         c.sf >= 7 && c.sf <= 12 && c.cr >= 5 && c.cr <= 8 && c.powerDbm >= -9 && c.powerDbm <= 22;
}

// (Re)initialize the SX1262 with cfg and leave it in standby.
static bool loraInit() {
  loraReady = false;
  int16_t err = radio.begin(cfg.freqHz / 1e6, cfg.bwHz / 1e3, cfg.sf, cfg.cr, SYNC_WORD, cfg.powerDbm,
                            PREAMBLE, TCXO_VOLTAGE);
  if (err != RADIOLIB_ERR_NONE) return loraFail(err, "radio.begin");
  if ((err = radio.setDio2AsRfSwitch(true)) != RADIOLIB_ERR_NONE) return loraFail(err, "setDio2AsRfSwitch");
  radio.setRxBoostedGainMode(true);  // ~2 dB better sensitivity for a little more current
  radio.setDio1Action(onDio1);
  frameAirMs = radio.getTimeOnAir(MAX_PAYLOAD) / 1000;
  // US 902-928 MHz: a channel narrower than 500 kHz may not be used for more than
  // 400 ms per 20 s without frequency hopping.
  if (cfg.bwHz < 500000 && frameAirMs > 400)
    logHost("warning: a full LoRa frame takes %lu ms on air; US rules allow 400 ms per channel at bw < 500 kHz",
            (unsigned long)frameAirMs);
  loraReady = true;
  return true;
}

static void startRx() {
  dio1Flag = false;
  int16_t err = radio.startReceive();
  if (err != RADIOLIB_ERR_NONE) loraFail(err, "startReceive");
  lstate = L_RX;
}

static bool loraStart() {
  if (lr.radioOn) return true;
  if (!loraReady && !loraInit()) return false;
  radio.standby();  // wakes it from sleep
  startRx();
  lr.radioOn = 1;
  logHost("LoRa on: %.3f MHz, bw %lu kHz, SF%u, CR 4/%u, %d dBm (full frame %lu ms)", cfg.freqHz / 1e6,
          (unsigned long)(cfg.bwHz / 1000), cfg.sf, cfg.cr, cfg.powerDbm, (unsigned long)frameAirMs);
  return true;
}

static void loraStop() {
  if (!lr.radioOn) return;
  radio.sleep(true);  // keeps the configuration
  lstate = L_OFF;
  lr.radioOn = 0;
  lr.txErr += txCount;  // frames still queued are lost, like a send to a radio that is off
  txCount = 0;
}

static void applyConfig(const RadioConfig &c) {
  if (!configValid(c)) { loraFail(RADIOLIB_ERR_INVALID_FREQUENCY, "CONFIG (invalid values)"); return; }
  if (memcmp(&c, &cfg, sizeof(cfg)) == 0 && loraReady) return;
  bool wasOn = lr.radioOn;
  cfg = c;
  lr.radioOn = 0;
  lstate = L_OFF;
  if (loraInit() && wasOn) loraStart();
}

static void loraQueue(const uint8_t *data, size_t len) {
  if (!lr.radioOn || len > MAX_PAYLOAD || txCount == TX_QUEUE_LEN) {
    lr.txErr++;
    if ((lr.txErr & (lr.txErr - 1)) == 0)
      logHost("LoRa send failed (%lu so far): %s", (unsigned long)lr.txErr,
              !lr.radioOn ? "radio is off" : len > MAX_PAYLOAD ? "frame too long" : "transmit queue full");
    return;
  }
  TxItem &item = txQueue[(txHead + txCount) % TX_QUEUE_LEN];
  item.len = len;
  memcpy(item.data, data, len);
  txCount++;
}

static void defer() {
  lr.deferrals++;
  deferralsInARow++;
  nextTxAt = millis() + random(frameAirMs / 4 + 1, frameAirMs + frameAirMs / 4 + 2);
}

// Listen before talk, then start sending the next queued frame.
static void tryTransmit() {
  if (deferralsInARow < MAX_DEFERRALS) {
    // A packet is arriving right now: sending would wreck it (and we'd miss it).
    uint32_t irq = radio.getIrqFlags();
    if ((irq & RADIOLIB_SX126X_IRQ_HEADER_VALID) && !(irq & RADIOLIB_SX126X_IRQ_RX_DONE)) { defer(); return; }
    // Someone else's preamble is on the air.
    int16_t cad = radio.scanChannel();  // a few symbols long; leaves the radio in standby
    dio1Flag = false;  // the CAD-done interrupt isn't a received packet
    if (cad == RADIOLIB_PREAMBLE_DETECTED || cad == RADIOLIB_LORA_DETECTED) { startRx(); defer(); return; }
  }
  deferralsInARow = 0;
  TxItem &item = txQueue[txHead];
  txHead = (txHead + 1) % TX_QUEUE_LEN;
  txCount--;
  lr.airtimeMs += radio.getTimeOnAir(item.len) / 1000;
  dio1Flag = false;
  int16_t err = radio.startTransmit(item.data, item.len);
  if (err != RADIOLIB_ERR_NONE) {
    lr.txErr++;
    loraFail(err, "startTransmit");
    startRx();
    return;
  }
  lstate = L_TX;
}

static void loraReceive() {
  uint8_t buf[256];
  size_t len = radio.getPacketLength();
  int16_t err = radio.readData(buf, min(len, sizeof(buf)));
  if (err == RADIOLIB_ERR_CRC_MISMATCH) { lr.rxCrcErr++; return; }
  if (err != RADIOLIB_ERR_NONE) { loraFail(err, "readData"); return; }
  if (len < 4 || len > MAX_PAYLOAD) return;
  lr.rx++;
  uint32_t sender;
  memcpy(&sender, buf, 4);
  if (partner != 0 && sender != partner) { lr.rxFiltered++; return; }
  int8_t rssi = (int8_t)constrain(radio.getRSSI(), -128.0f, 127.0f);
  if (textMode) {
    Serial.printf("LORA RX %d dBm snr %.1f ", rssi, radio.getSNR());
    printPayload(buf, len);
    return;
  }
  uint8_t head[1] = {(uint8_t)rssi};
  sendToHost(EVT_LORA_RECV, head, 1, buf, len);
}

static void loraService() {
  if (!lr.radioOn) return;
  if (dio1Flag) {
    dio1Flag = false;
    if (lstate == L_TX) {
      radio.finishTransmit();
      lr.txOk++;
      startRx();
    } else if (lstate == L_RX) {
      loraReceive();  // the radio stays in continuous receive
    }
  }
  if (lstate == L_RX && txCount > 0 && (int32_t)(millis() - nextTxAt) >= 0) tryTransmit();
}

// ---- status ---------------------------------------------------------------

static void sendInfo() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);  // works even while Wi-Fi is off
  uint8_t primary = 0;
  wifi_second_chan_t second;
  if (en.radioOn) esp_wifi_get_channel(&primary, &second);
  en.channel = primary;
  en.rx = enRx;
  en.rxFiltered = enFiltered;
  en.rxDropped = enDropped;
  lr.cfg = cfg;
  lr.txQueued = txCount;
  sendToHost(EVT_INFO, mac, 6, (const uint8_t *)&en, sizeof(en), (const uint8_t *)&lr, sizeof(lr));
}

// ---- text commands (for a human at a serial monitor, 115200 baud) ---------

static void handleText(char *line) {
  if (strcmp(line, "HELP") == 0) {
    Serial.println("commands: ID, STATUS, LORA ON|OFF, ESPNOW ON|OFF, TX <text> (LoRa), ETX <text> (ESP-NOW), "
                   "SF <7-12>, HELP");
  } else if (strcmp(line, "ID") == 0) {
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    Serial.printf("OK heltec_bridge mac=%02x:%02x:%02x:%02x:%02x:%02x lora=%s espnow=%s freq=%.3f bw=%lu sf=%u\r\n",
                  mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], lr.radioOn ? "on" : "off",
                  en.radioOn ? "on" : "off", cfg.freqHz / 1e6, (unsigned long)(cfg.bwHz / 1000), cfg.sf);
  } else if (strcmp(line, "STATUS") == 0) {
    Serial.printf("OK partner=%lu\r\n", (unsigned long)partner);
    Serial.printf("   lora %s: sent=%lu errors=%lu queued=%u received=%lu filtered=%lu crc_errors=%lu "
                  "deferrals=%lu airtime_ms=%lu last_error=%ld\r\n",
                  lr.radioOn ? "on" : "off", (unsigned long)lr.txOk, (unsigned long)lr.txErr, txCount,
                  (unsigned long)lr.rx, (unsigned long)lr.rxFiltered, (unsigned long)lr.rxCrcErr,
                  (unsigned long)lr.deferrals, (unsigned long)lr.airtimeMs, (long)lr.lastErr);
    Serial.printf("   espnow %s: sent=%lu errors=%lu received=%lu filtered=%lu dropped=%lu last_error=%s\r\n",
                  en.radioOn ? "on" : "off", (unsigned long)en.txOk, (unsigned long)en.txErr,
                  (unsigned long)enRx, (unsigned long)enFiltered, (unsigned long)enDropped,
                  en.lastErr ? esp_err_to_name(en.lastErr) : "none");
  } else if (strcmp(line, "LORA ON") == 0) {
    Serial.println(loraStart() ? "OK lora on" : "ERR lora start failed (see LOG line)");
  } else if (strcmp(line, "LORA OFF") == 0) {
    loraStop();
    Serial.println("OK lora off");
  } else if (strcmp(line, "ESPNOW ON") == 0) {
    Serial.println(espnowStart() ? "OK espnow on" : "ERR espnow start failed (see LOG line)");
  } else if (strcmp(line, "ESPNOW OFF") == 0) {
    espnowStop();
    Serial.println("OK espnow off");
  } else if (strncmp(line, "SF ", 3) == 0) {
    RadioConfig c = cfg;
    c.sf = atoi(line + 3);
    if (!configValid(c)) { Serial.println("ERR SF must be 7..12"); return; }
    applyConfig(c);
    Serial.printf("OK sf=%u (every board must match)\r\n", cfg.sf);
  } else if (strncmp(line, "TX ", 3) == 0) {
    if (!lr.radioOn) { Serial.println("ERR lora off (send LORA ON first)"); return; }
    uint32_t before = lr.txErr;
    loraQueue((const uint8_t *)line + 3, strlen(line + 3));
    Serial.println(lr.txErr == before ? "OK queued" : "ERR send failed (see LOG line)");
  } else if (strncmp(line, "ETX ", 4) == 0) {
    if (!en.radioOn) { Serial.println("ERR espnow off (send ESPNOW ON first)"); return; }
    uint32_t before = en.txErr;
    espnowSend(BROADCAST, (const uint8_t *)line + 4, strlen(line + 4));
    Serial.println(en.txErr == before ? "OK sent" : "ERR send failed (see LOG line)");
  } else if (line[0] != '\0') {
    Serial.println("ERR unknown command (try HELP)");
  }
}

// ---- host commands --------------------------------------------------------

static uint8_t cmdBuf[1 + 6 + MAX_PAYLOAD + 8];
static size_t cmdLen = 0;
static bool cmdEsc = false, cmdOverflow = false;
static bool inFrame = false;  // between a binary message's opening and closing 0xC0
static char textBuf[MAX_PAYLOAD + 8];
static size_t textLen = 0;

static void handleCommand(const uint8_t *m, size_t n) {
  if (n == 0) return;
  switch (m[0]) {
    case CMD_SEND:
      if (n >= 7) espnowSend(m + 1, m + 7, n - 7);
      break;
    case CMD_LORA_SEND:
      loraQueue(m + 1, n - 1);
      break;
    case CMD_SET_PARTNER:
      if (n >= 5) { uint32_t id; memcpy(&id, m + 1, 4); partner = id; }
      break;
    case CMD_RADIO:
      if (n >= 2) { if (m[1]) espnowStart(); else espnowStop(); }
      break;
    case CMD_LORA_RADIO:
      if (n >= 2) { if (m[1]) loraStart(); else loraStop(); }
      break;
    case CMD_GET_INFO:
      sendInfo();
      break;
    case CMD_CONFIG:
      if (n >= 1 + sizeof(RadioConfig)) {
        RadioConfig c;
        memcpy(&c, m + 1, sizeof(c));
        applyConfig(c);
      }
      break;
  }
}

static void readHost() {
  static uint32_t lastByteMs = 0;
  while (Serial.available()) {
    uint8_t b = Serial.read();
    // Python writes each binary message in one go, so a long pause mid-message means
    // it was abandoned (host killed). Drop it so typed text works again.
    if (inFrame && millis() - lastByteMs > 500) { inFrame = false; cmdLen = 0; cmdEsc = false; }
    lastByteMs = millis();
    if (b == SLIP_END) {
      if (cmdLen > 0) {  // closing 0xC0 of a binary message
        textMode = false;
        if (!cmdOverflow) handleCommand(cmdBuf, cmdLen);
        inFrame = false;
      } else {  // opening 0xC0 (or an empty message)
        inFrame = true;
        textLen = 0;
      }
      cmdLen = 0; cmdEsc = false; cmdOverflow = false;
      continue;
    }
    if (!inFrame) {  // typed text: collect a line
      if (b == '\r' || b == '\n') {
        if (textLen > 0) {
          textBuf[textLen] = '\0';
          textMode = true;
          handleText(textBuf);
          textLen = 0;
        }
      } else if (textLen < sizeof(textBuf) - 1) {
        textBuf[textLen++] = (char)b;
      }
      continue;
    }
    if (cmdEsc) { b = (b == SLIP_ESC_END) ? SLIP_END : (b == SLIP_ESC_ESC) ? SLIP_ESC : b; cmdEsc = false; }
    else if (b == SLIP_ESC) { cmdEsc = true; continue; }
    if (cmdLen < sizeof(cmdBuf)) cmdBuf[cmdLen++] = b; else cmdOverflow = true;
  }
}

void setup() {
  Serial.setRxBufferSize(4096);  // the host may send a burst of frames
  Serial.begin(BAUD);
  enQueue = xQueueCreate(32, sizeof(RxItem));
  WiFi.mode(WIFI_OFF);  // both radios come up on the host's command
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_NSS);
  randomSeed(esp_random());
  if (loraInit()) radio.sleep(true);
  delay(100);
  logHost("heltec_bridge ready");
  sendInfo();  // tells a host that is already connected that we rebooted (both radios off)
}

void loop() {
  readHost();
  loraService();
  espnowForward();
}
