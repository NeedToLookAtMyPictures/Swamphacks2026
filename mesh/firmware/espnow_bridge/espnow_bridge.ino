// ESP-NOW <-> USB serial bridge for trailmesh (see trailmesh/espnow_serial.py).
//
// The ESP32 only moves frames between the radio and the host; all protocol
// logic lives in Python. It also applies the partner filter: while the host
// has set a partner, frames whose first 4 bytes (sender device id) don't match
// are dropped here, so 2.4 GHz noise never reaches the host.
//
// Radio bring-up follows the team's working esp-now-modem sketch:
//   * Wi-Fi starts OFF; the radio only comes up when the host sends RADIO on,
//     so any error is reported while the host is listening.
//   * Wait for the STA interface to finish starting before setting the channel
//     and calling esp_now_init (doing it too early fails silently).
//   * Every ESP-IDF call is checked; failures are logged to the host and kept
//     in the status block returned by GET_INFO.
//
// Requires Arduino-ESP32 core 3.x (WiFi.STA.started() and esp_now_recv_info_t).

#include <Arduino.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const uint8_t CHANNEL = 6;  // must match on all boards (same as esp-now-modem)
static const uint32_t BAUD = 460800;  // ignored by native-USB boards; must match the host for UART bridges
static const uint8_t MAX_PAYLOAD = 250;  // ESP-NOW v1 limit

enum : uint8_t {
  CMD_SEND = 0x01, CMD_SET_PARTNER = 0x02, CMD_RADIO = 0x03, CMD_GET_INFO = 0x04,
  EVT_RECV = 0x81, EVT_INFO = 0x84, EVT_LOG = 0x85,
};

static const uint8_t SLIP_END = 0xC0, SLIP_ESC = 0xDB, SLIP_ESC_END = 0xDC, SLIP_ESC_ESC = 0xDD;
static const uint8_t BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

struct RxItem {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[MAX_PAYLOAD];
};

// Reported to the host in the INFO reply (layout must match STATUS in espnow_serial.py).
struct __attribute__((packed)) Status {
  uint8_t radioOn;
  uint8_t channel;
  int32_t lastErr;  // esp_err_t of the most recent failure, 0 if none
  uint32_t txOk, txErr, rx, rxFiltered, rxDropped;
};

static QueueHandle_t rxQueue;
static volatile uint32_t partner = 0;  // 0 = accept everyone
static Status status = {};
static volatile uint32_t rxCount = 0, rxFiltered = 0, rxDropped = 0;  // written in the Wi-Fi task

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
  sendToHost(EVT_LOG, (const uint8_t *)buf, n < 0 ? 0 : min((size_t)n, sizeof(buf) - 1));
}

static bool fail(esp_err_t err, const char *what) {
  status.lastErr = err;
  logHost("%s failed: %s (0x%x)", what, esp_err_to_name(err), err);
  return false;
}

// ---- radio ----------------------------------------------------------------

// Runs in the Wi-Fi task: copy and return immediately.
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len < 4 || len > MAX_PAYLOAD) return;
  rxCount++;
  uint32_t sender;
  memcpy(&sender, data, 4);  // little-endian, same as the Python side
  uint32_t p = partner;
  if (p != 0 && sender != p) { rxFiltered++; return; }
  RxItem item;
  memcpy(item.mac, info->src_addr, 6);
  item.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  item.len = len;
  memcpy(item.data, data, len);
  if (xQueueSend(rxQueue, &item, 0) != pdTRUE) rxDropped++;  // host is falling behind
}

static bool ensurePeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = CHANNEL;
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
  return err == ESP_OK || fail(err, "esp_now_add_peer");
}

static bool radioStart() {
  if (status.radioOn) return true;
  WiFi.mode(WIFI_STA);
  uint32_t t0 = millis();
  while (!WiFi.STA.started()) {
    if (millis() - t0 > 3000) return fail(ESP_ERR_TIMEOUT, "wifi STA start");
    delay(10);
  }
  esp_err_t err = esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) return fail(err, "esp_wifi_set_channel");
  if ((err = esp_now_init()) != ESP_OK) return fail(err, "esp_now_init");
  esp_now_register_recv_cb(onRecv);
  if (!ensurePeer(BROADCAST)) {
    esp_now_deinit();
    return false;
  }
  status.radioOn = 1;
  logHost("radio on, channel %u", CHANNEL);
  return true;
}

static void radioStop() {
  if (!status.radioOn) return;
  esp_now_deinit();  // also clears the peer table; peers are re-added on demand
  WiFi.mode(WIFI_OFF);
  status.radioOn = 0;
}

static void radioSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  esp_err_t err = ESP_ERR_INVALID_STATE;
  if (status.radioOn && len <= MAX_PAYLOAD && ensurePeer(mac)) {
    // The ESP-NOW tx queue is small; when the host sends a burst, wait briefly for room.
    for (int tries = 0; tries < 20; tries++) {
      err = esp_now_send(mac, data, len);
      if (err != ESP_ERR_ESPNOW_NO_MEM) break;
      delay(1);
    }
  }
  if (err == ESP_OK) { status.txOk++; return; }
  status.txErr++;
  status.lastErr = err;
  // Log the 1st, 2nd, 4th, 8th... failure so a persistent problem is visible without flooding.
  if ((status.txErr & (status.txErr - 1)) == 0)
    logHost("send failed (%lu so far): %s%s", (unsigned long)status.txErr, esp_err_to_name(err),
            status.radioOn ? "" : " (radio is off)");
}

static void sendInfo() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);  // works even while Wi-Fi is off
  uint8_t primary = 0;
  wifi_second_chan_t second;
  if (status.radioOn) esp_wifi_get_channel(&primary, &second);
  status.channel = primary;
  status.rx = rxCount;
  status.rxFiltered = rxFiltered;
  status.rxDropped = rxDropped;
  sendToHost(EVT_INFO, mac, 6, (const uint8_t *)&status, sizeof(status));
}

// ---- host commands --------------------------------------------------------

static uint8_t cmdBuf[1 + 6 + MAX_PAYLOAD + 8];
static size_t cmdLen = 0;
static bool cmdEsc = false, cmdOverflow = false;

static void handleCommand(const uint8_t *m, size_t n) {
  if (n == 0) return;
  switch (m[0]) {
    case CMD_SEND:
      if (n >= 7) radioSend(m + 1, m + 7, n - 7);
      break;
    case CMD_SET_PARTNER:
      if (n >= 5) { uint32_t id; memcpy(&id, m + 1, 4); partner = id; }
      break;
    case CMD_RADIO:
      if (n >= 2) { if (m[1]) radioStart(); else radioStop(); }
      break;
    case CMD_GET_INFO:
      sendInfo();
      break;
  }
}

static void readHost() {
  while (Serial.available()) {
    uint8_t b = Serial.read();
    if (b == SLIP_END) {
      if (!cmdOverflow) handleCommand(cmdBuf, cmdLen);
      cmdLen = 0; cmdEsc = false; cmdOverflow = false;
      continue;
    }
    if (cmdEsc) { b = (b == SLIP_ESC_END) ? SLIP_END : (b == SLIP_ESC_ESC) ? SLIP_ESC : b; cmdEsc = false; }
    else if (b == SLIP_ESC) { cmdEsc = true; continue; }
    if (cmdLen < sizeof(cmdBuf)) cmdBuf[cmdLen++] = b; else cmdOverflow = true;
  }
}

void setup() {
  Serial.setRxBufferSize(4096);  // the host may send a burst of ~16 frames (~4 KB)
  Serial.begin(BAUD);
  rxQueue = xQueueCreate(32, sizeof(RxItem));
  WiFi.mode(WIFI_OFF);  // radio comes up on the host's RADIO on command
  delay(500);
  logHost("espnow_bridge ready");
}

void loop() {
  readHost();
  RxItem item;
  while (xQueueReceive(rxQueue, &item, 0) == pdTRUE) {
    uint8_t head[7];
    memcpy(head, item.mac, 6);
    head[6] = (uint8_t)item.rssi;
    sendToHost(EVT_RECV, head, 7, item.data, item.len);
  }
}
