// ESP-NOW <-> USB serial bridge for trailmesh (see trailmesh/espnow_serial.py).
//
// The ESP32 only moves frames between the radio and the host; all protocol
// logic lives in Python. It also applies the partner filter: while the host
// has set a partner, frames whose first 4 bytes (sender device id) don't match
// are dropped here, so 2.4 GHz noise never reaches the host.
//
// Targets Arduino-ESP32 core 3.x (ESP-IDF 5.x). On core 2.x, change onRecv's
// signature to (const uint8_t *mac, const uint8_t *data, int len) and drop rssi.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const uint8_t CHANNEL = 1;  // every device must use the same channel
static const uint32_t BAUD = 460800;

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
  uint8_t data[ESP_NOW_MAX_DATA_LEN];
};

static QueueHandle_t rxQueue;
static volatile uint32_t partner = 0;  // 0 = accept everyone
static bool radioOn = false;

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

static void logHost(const char *msg) { sendToHost(EVT_LOG, (const uint8_t *)msg, strlen(msg)); }

// ---- radio ----------------------------------------------------------------

// Runs in the Wi-Fi task: copy and return immediately.
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len < 4 || len > ESP_NOW_MAX_DATA_LEN) return;
  uint32_t sender;
  memcpy(&sender, data, 4);  // little-endian, same as the Python side
  uint32_t p = partner;
  if (p != 0 && sender != p) return;
  RxItem item;
  memcpy(item.mac, info->src_addr, 6);
  item.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  item.len = len;
  memcpy(item.data, data, len);
  xQueueSend(rxQueue, &item, 0);  // drop if the host is falling behind
}

static bool ensurePeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  esp_err_t err = esp_now_add_peer(&peer);
  if (err == ESP_ERR_ESPNOW_FULL) {
    // Peer table holds ~20 entries. Evict any unicast peer to make room.
    esp_now_peer_info_t old;
    for (bool first = true; esp_now_fetch_peer(first, &old) == ESP_OK; first = false) {
      if (memcmp(old.peer_addr, BROADCAST, 6) != 0) { esp_now_del_peer(old.peer_addr); break; }
    }
    err = esp_now_add_peer(&peer);
  }
  return err == ESP_OK;
}

static void radioStart() {
  if (radioOn) return;
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { logHost("esp_now_init failed"); return; }
  esp_now_register_recv_cb(onRecv);
  ensurePeer(BROADCAST);
  radioOn = true;
}

static void radioStop() {
  if (!radioOn) return;
  esp_now_deinit();
  WiFi.mode(WIFI_OFF);
  radioOn = false;
}

static void radioSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  if (!radioOn || len > ESP_NOW_MAX_DATA_LEN || !ensurePeer(mac)) return;
  // The ESP-NOW tx queue is small; when the host sends a burst, wait briefly for room.
  for (int tries = 0; tries < 20; tries++) {
    esp_err_t err = esp_now_send(mac, data, len);
    if (err != ESP_ERR_ESPNOW_NO_MEM) return;
    delay(1);
  }
}

// ---- host commands --------------------------------------------------------

static uint8_t cmdBuf[1 + 6 + ESP_NOW_MAX_DATA_LEN + 8];
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
    case CMD_GET_INFO: {
      uint8_t mac[6];
      WiFi.macAddress(mac);
      sendToHost(EVT_INFO, mac, 6);
      break;
    }
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
  Serial.setRxBufferSize(8192);
  Serial.begin(BAUD);
  rxQueue = xQueueCreate(64, sizeof(RxItem));
  radioStart();
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
