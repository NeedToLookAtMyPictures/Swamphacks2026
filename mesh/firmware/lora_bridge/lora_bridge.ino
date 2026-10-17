// LoRa <-> USB serial bridge for trailmesh (see trailmesh/lora_serial.py).
//
// Board: Heltec WiFi LoRa 32 V3 and its clones (ESP32-S3 + SX1262, CP2102 USB-UART).
// Library: RadioLib 7.x (Arduino Library Manager: "RadioLib").
//
// Same job and same host protocol as espnow_bridge: the ESP32 only moves frames
// between the radio and the host, and drops frames whose first 4 bytes (sender
// device id) aren't the current partner. All protocol logic lives in Python.
// Differences from ESP-NOW:
//   * LoRa has no addresses: every frame is a broadcast, the MAC in SEND is
//     ignored and RECV reports an all-zero MAC.
//   * Raw LoRa has no listen-before-talk, so this sketch adds it: before each
//     send it checks that no packet is being received and runs channel activity
//     detection (CAD); if the channel is busy it listens and retries later.
//   * Sending takes ~0.1-1 s per frame, so SEND only queues the frame.
//   * One extra command, CONFIG, sets frequency / bandwidth / SF / CR / power.
//   * The board resets when the host opens the port (CP2102 auto-reset), so it
//     announces itself with an INFO message at boot and the host re-sends its state.

#include <Arduino.h>
#include <RadioLib.h>
#include <SPI.h>
#include <esp_mac.h>

// Heltec V3 wiring of the SX1262
static const int PIN_NSS = 8, PIN_SCK = 9, PIN_MOSI = 10, PIN_MISO = 11, PIN_RST = 12, PIN_BUSY = 13, PIN_DIO1 = 14;
static const float TCXO_VOLTAGE = 1.8;  // the V3's SX1262 runs from a TCXO powered by DIO3

static const uint32_t BAUD = 115200;    // CP2102 bridge: must match LoRaSerialLink
static const uint8_t MAX_PAYLOAD = 250;  // trailmesh frame limit (the SX1262 allows 255)
static const uint8_t SYNC_WORD = 0x24;   // not LoRaWAN (0x34), Meshtastic (0x2B) or RadioLib's default (0x12)
static const uint16_t PREAMBLE = 8;      // must match LoRaParams.preamble
static const uint8_t TX_QUEUE_LEN = 16;
static const uint8_t MAX_DEFERRALS = 8;  // after this many busy-channel checks, send anyway

enum : uint8_t {
  CMD_SEND = 0x01, CMD_SET_PARTNER = 0x02, CMD_RADIO = 0x03, CMD_GET_INFO = 0x04, CMD_CONFIG = 0x05,
  EVT_RECV = 0x81, EVT_INFO = 0x84, EVT_LOG = 0x85,
};

static const uint8_t SLIP_END = 0xC0, SLIP_ESC = 0xDB, SLIP_ESC_END = 0xDC, SLIP_ESC_ESC = 0xDD;

// Host-settable radio settings (layout must match CONFIG in trailmesh/lora.py).
struct __attribute__((packed)) RadioConfig {
  uint32_t freqHz;
  uint32_t bwHz;
  uint8_t sf;
  uint8_t cr;  // coding rate 4/cr
  int8_t powerDbm;
};

// INFO reply. The first part has the same layout as espnow_bridge (STATUS in
// espnow_serial.py); the LoRa part follows (LORA_STATUS in lora_serial.py).
struct __attribute__((packed)) Status {
  uint8_t radioOn;
  uint8_t channel;  // spreading factor here (no Wi-Fi channel)
  int32_t lastErr;  // RadioLib status code of the most recent failure, 0 if none
  uint32_t txOk, txErr, rx, rxFiltered, rxDropped;
  RadioConfig cfg;
  uint8_t txQueued;
  uint32_t airtimeMs;  // total time spent transmitting
  uint32_t deferrals;  // sends postponed because the channel was busy
  uint32_t rxCrcErr;
};

SX1262 radio = new Module(PIN_NSS, PIN_DIO1, PIN_RST, PIN_BUSY, SPI);

static RadioConfig cfg = {915000000, 500000, 7, 5, 20};
static Status status = {};
static uint32_t partner = 0;  // 0 = accept everyone
static bool textMode = false;  // a human typed a command; print instead of SLIP
static bool radioReady = false;  // begin() succeeded with the current cfg

enum RadioState : uint8_t { R_OFF, R_RX, R_TX };
static RadioState rstate = R_OFF;
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

// ---- serial framing -------------------------------------------------------

static void slipPutByte(uint8_t b) {
  if (b == SLIP_END) { Serial.write(SLIP_ESC); Serial.write(SLIP_ESC_END); }
  else if (b == SLIP_ESC) { Serial.write(SLIP_ESC); Serial.write(SLIP_ESC_ESC); }
  else Serial.write(b);
}

static void sendToHost(uint8_t evt, const uint8_t *a, size_t alen, const uint8_t *b = nullptr, size_t blen = 0) {
  Serial.write(SLIP_END);
  slipPutByte(evt);
  for (size_t i = 0; i < alen; i++) slipPutByte(a[i]);
  for (size_t i = 0; i < blen; i++) slipPutByte(b[i]);
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

static bool fail(int16_t err, const char *what) {
  status.lastErr = err;
  logHost("%s failed: RadioLib error %d", what, err);
  return false;
}

// ---- radio ----------------------------------------------------------------

#if defined(ESP8266) || defined(ESP32)
ICACHE_RAM_ATTR
#endif
static void onDio1() { dio1Flag = true; }

static bool configValid(const RadioConfig &c) {
  return c.freqHz >= 863000000 && c.freqHz <= 928000000 &&
         (c.bwHz == 125000 || c.bwHz == 250000 || c.bwHz == 500000) &&
         c.sf >= 7 && c.sf <= 12 && c.cr >= 5 && c.cr <= 8 && c.powerDbm >= -9 && c.powerDbm <= 22;
}

// (Re)initialize the SX1262 with cfg and leave it in standby.
static bool radioInit() {
  radioReady = false;
  int16_t err = radio.begin(cfg.freqHz / 1e6, cfg.bwHz / 1e3, cfg.sf, cfg.cr, SYNC_WORD, cfg.powerDbm,
                            PREAMBLE, TCXO_VOLTAGE);
  if (err != RADIOLIB_ERR_NONE) return fail(err, "radio.begin");
  if ((err = radio.setDio2AsRfSwitch(true)) != RADIOLIB_ERR_NONE) return fail(err, "setDio2AsRfSwitch");
  radio.setRxBoostedGainMode(true);  // ~2 dB better sensitivity for a little more current
  radio.setDio1Action(onDio1);
  frameAirMs = radio.getTimeOnAir(MAX_PAYLOAD) / 1000;
  // US 902-928 MHz: a channel narrower than 500 kHz may not be used for more than
  // 400 ms per 20 s without frequency hopping.
  if (cfg.bwHz < 500000 && frameAirMs > 400)
    logHost("warning: a full frame takes %lu ms on air; US rules allow 400 ms per channel at bw < 500 kHz",
            (unsigned long)frameAirMs);
  radioReady = true;
  return true;
}

static void startRx() {
  dio1Flag = false;
  int16_t err = radio.startReceive();
  if (err != RADIOLIB_ERR_NONE) fail(err, "startReceive");
  rstate = R_RX;
}

static bool radioStart() {
  if (status.radioOn) return true;
  if (!radioReady && !radioInit()) return false;
  radio.standby();  // wakes it from sleep
  startRx();
  status.radioOn = 1;
  logHost("radio on: %.3f MHz, bw %lu kHz, SF%u, CR 4/%u, %d dBm (full frame %lu ms)", cfg.freqHz / 1e6,
          (unsigned long)(cfg.bwHz / 1000), cfg.sf, cfg.cr, cfg.powerDbm, (unsigned long)frameAirMs);
  return true;
}

static void radioStop() {
  if (!status.radioOn) return;
  radio.sleep(true);  // keeps the configuration
  rstate = R_OFF;
  status.radioOn = 0;
  status.txErr += txCount;  // frames still queued are lost, like a send to a radio that is off
  txCount = 0;
}

static void applyConfig(const RadioConfig &c) {
  if (!configValid(c)) { fail(RADIOLIB_ERR_INVALID_FREQUENCY, "CONFIG (invalid values)"); return; }
  if (memcmp(&c, &cfg, sizeof(cfg)) == 0 && radioReady) return;
  bool wasOn = status.radioOn;
  cfg = c;
  status.radioOn = 0;
  rstate = R_OFF;
  if (radioInit() && wasOn) radioStart();
}

static void queueSend(const uint8_t *data, size_t len) {
  if (!status.radioOn || len > MAX_PAYLOAD || txCount == TX_QUEUE_LEN) {
    status.txErr++;
    if ((status.txErr & (status.txErr - 1)) == 0)  // 1st, 2nd, 4th, 8th... so it doesn't flood
      logHost("send failed (%lu so far): %s", (unsigned long)status.txErr,
              !status.radioOn ? "radio is off" : len > MAX_PAYLOAD ? "frame too long" : "transmit queue full");
    return;
  }
  TxItem &item = txQueue[(txHead + txCount) % TX_QUEUE_LEN];
  item.len = len;
  memcpy(item.data, data, len);
  txCount++;
}

static void defer() {
  status.deferrals++;
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
  status.airtimeMs += radio.getTimeOnAir(item.len) / 1000;
  dio1Flag = false;
  int16_t err = radio.startTransmit(item.data, item.len);
  if (err != RADIOLIB_ERR_NONE) {
    status.txErr++;
    fail(err, "startTransmit");
    startRx();
    return;
  }
  rstate = R_TX;
}

static void handleRx() {
  uint8_t buf[256];
  size_t len = radio.getPacketLength();
  int16_t err = radio.readData(buf, min(len, sizeof(buf)));
  if (err == RADIOLIB_ERR_CRC_MISMATCH) { status.rxCrcErr++; return; }
  if (err != RADIOLIB_ERR_NONE) { fail(err, "readData"); return; }
  if (len < 4 || len > MAX_PAYLOAD) return;
  status.rx++;
  uint32_t sender;
  memcpy(&sender, buf, 4);  // little-endian, same as the Python side
  if (partner != 0 && sender != partner) { status.rxFiltered++; return; }
  int8_t rssi = (int8_t)constrain(radio.getRSSI(), -128.0f, 127.0f);
  if (textMode) {
    Serial.printf("RX %d dBm snr %.1f ", rssi, radio.getSNR());
    for (size_t i = 0; i < len; i++) Serial.write(isprint(buf[i]) ? buf[i] : '.');
    Serial.println();
    return;
  }
  uint8_t head[7] = {0, 0, 0, 0, 0, 0, (uint8_t)rssi};  // LoRa has no source MAC
  sendToHost(EVT_RECV, head, 7, buf, len);
}

static void serviceRadio() {
  if (!status.radioOn) return;
  if (dio1Flag) {
    dio1Flag = false;
    if (rstate == R_TX) {
      radio.finishTransmit();
      status.txOk++;
      startRx();
    } else if (rstate == R_RX) {
      handleRx();  // the radio stays in continuous receive
    }
  }
  if (rstate == R_RX && txCount > 0 && (int32_t)(millis() - nextTxAt) >= 0) tryTransmit();
}

static void sendInfo() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);  // a unique board id; Wi-Fi itself stays off
  status.channel = cfg.sf;
  status.cfg = cfg;
  status.txQueued = txCount;
  sendToHost(EVT_INFO, mac, 6, (const uint8_t *)&status, sizeof(status));
}

// ---- text commands (for a human at a serial monitor, 115200 baud) ---------

static void printMac(const uint8_t *mac) {
  Serial.printf("%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static void handleText(char *line) {
  if (strcmp(line, "HELP") == 0) {
    Serial.println("commands: ID, STATUS, ON, OFF, TX <text>, SF <7-12>, HELP");
  } else if (strcmp(line, "ID") == 0) {
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    Serial.print("OK lora_bridge mac=");
    printMac(mac);
    Serial.printf(" radio=%s freq=%.3f bw=%lu sf=%u cr=4/%u power=%d\r\n", status.radioOn ? "on" : "off",
                  cfg.freqHz / 1e6, (unsigned long)(cfg.bwHz / 1000), cfg.sf, cfg.cr, cfg.powerDbm);
  } else if (strcmp(line, "STATUS") == 0) {
    Serial.printf("OK radio=%s partner=%lu sent=%lu send_errors=%lu queued=%u received=%lu filtered=%lu "
                  "crc_errors=%lu deferrals=%lu airtime_ms=%lu last_error=%ld\r\n",
                  status.radioOn ? "on" : "off", (unsigned long)partner, (unsigned long)status.txOk,
                  (unsigned long)status.txErr, txCount, (unsigned long)status.rx, (unsigned long)status.rxFiltered,
                  (unsigned long)status.rxCrcErr, (unsigned long)status.deferrals, (unsigned long)status.airtimeMs,
                  (long)status.lastErr);
  } else if (strcmp(line, "ON") == 0) {
    Serial.println(radioStart() ? "OK radio on" : "ERR radio start failed (see LOG line)");
  } else if (strcmp(line, "OFF") == 0) {
    radioStop();
    Serial.println("OK radio off");
  } else if (strncmp(line, "SF ", 3) == 0) {
    RadioConfig c = cfg;
    c.sf = atoi(line + 3);
    if (!configValid(c)) { Serial.println("ERR SF must be 7..12"); return; }
    applyConfig(c);
    Serial.printf("OK sf=%u (every board must match)\r\n", cfg.sf);
  } else if (strncmp(line, "TX ", 3) == 0) {
    // Raw broadcast. The other board prints it if it is also in text mode.
    if (!status.radioOn) { Serial.println("ERR radio off (send ON first)"); return; }
    uint32_t before = status.txErr;
    queueSend((const uint8_t *)line + 3, strlen(line + 3));
    Serial.println(status.txErr == before ? "OK queued" : "ERR send failed (see LOG line)");
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
      if (n >= 7) queueSend(m + 7, n - 7);  // the 6-byte MAC is ignored: LoRa only broadcasts
      break;
    case CMD_SET_PARTNER:
      if (n >= 5) memcpy(&partner, m + 1, 4);
      break;
    case CMD_RADIO:
      if (n >= 2) { if (m[1]) radioStart(); else radioStop(); }
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
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_NSS);
  randomSeed(esp_random());
  if (radioInit()) radio.sleep(true);  // the radio comes up on the host's RADIO on command
  delay(100);
  logHost("lora_bridge ready");
  sendInfo();  // tells a host that is already connected that we rebooted (radio off)
}

void loop() {
  readHost();
  serviceRadio();
}
