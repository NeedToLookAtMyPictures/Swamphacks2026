// Mesh status, at the top of the Mesh tab (drawn as tables like the database below): what the radio is doing (board, nearby
// hikers, the current sync, recent activity). Also arrivalText(), used by the Messages,
// Map and Debug views to say how a record reached this device.

function meshEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent: names and messages can't inject HTML
  if (className) node.className = className;
  return node;
}

// ---- how a record got here (received_via) ------------------------------------

const VIA_LABELS = {
  "LoRa": "LoRa radio",
  "ESP-NOW": "ESP-NOW (Wi-Fi radio)",
  "server": "the server",
  "sync API": "a direct sync",
};

function duration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min`;
  const hours = Math.round(seconds / 3600);
  if (hours < 36) return `${hours} hr`;
  const days = Math.round(seconds / 86400);
  return `${days} day${days === 1 ? "" : "s"}`;
}

// "via LoRa radio · arrived 3 min after it was written", or null for rows made here.
// The delay is the delay-tolerant part: news that rode with other hikers to get here.
function arrivalText(row) {
  if (!row.received_via) return null;
  let text = `via ${VIA_LABELS[row.received_via] || row.received_via}`;
  const delay = row.time_received - row.time_created;
  if (delay >= 60) text += ` · arrived ${duration(delay)} after it was written`;
  return text;
}

// A small line under a record, coloured by radio; null if it was made on this device
function arrivalNote(row) {
  const text = arrivalText(row);
  if (!text) return null;
  const via = (row.received_via || "").toLowerCase().replace(/[^a-z]/g, "");
  return meshEl("div", text, `via-note via-${via}`);
}

// ---- mesh status (top of the Mesh tab) --------------------------------------------

const MESH_REFRESH_MS = 2000;
const NEARBY_S = 60;  // matches NEARBY in api.py

function ago(at, now) {
  return `${duration(Math.max(0, now - at))} ago`;
}

// Rough signal quality; LoRa still works far below where Wi-Fi gives up
function signalBars(rssi, radio) {
  if (rssi === null || rssi === undefined) return "";
  const steps = radio === "LoRa" ? [-80, -100, -115, -125] : [-55, -67, -75, -85];
  const bars = steps.filter((limit) => rssi >= limit).length;
  return "▂▄▆█".slice(0, bars).padEnd(4, "·");
}

// The mesh status is drawn as tables, so it shares the Mesh tab's table styling
// (#debug table / th / td in style.css) with the database tables below it.
function kv(label, value) {
  const row = meshEl("tr");
  row.append(meshEl("th", label), meshEl("td", value));
  return row;
}

function meshTable(columns, rows) {
  const table = meshEl("table");
  const head = table.createTHead().insertRow();
  columns.forEach((c) => head.appendChild(meshEl("th", c)));
  const body = table.createTBody();
  rows.forEach((cells) => {
    const tr = body.insertRow();
    cells.forEach((value) => tr.appendChild(meshEl("td", value)));
  });
  const wrap = meshEl("div", undefined, "debug-table-wrap");
  wrap.appendChild(table);
  return wrap;
}

function renderBoard(data) {
  const box = document.getElementById("mesh-board");
  const rows = [];
  const b = data.board;
  rows.push(kv("board", `${data.board_kind === "heltec" ? "Heltec LoRa (LoRa + ESP-NOW)" : "ESP-NOW"} on ${data.port}`));
  if (b) {
    rows.push(kv("connection", b.connected ? `connected${b.mac ? ` · ${b.mac}` : ""}` : "board unplugged, waiting for it"));
    if (b.lora) {
      const lo = b.lora;
      rows.push(kv("lora", `${lo.on ? "on" : "off"} · ${lo.freq_mhz.toFixed(1)} MHz, SF${lo.sf}, ${lo.bw_khz} kHz, ${lo.power_dbm} dBm`));
      if (lo.sent !== null && lo.sent !== undefined) {
        rows.push(kv("lora_traffic", `sent ${lo.sent} · received ${lo.received} · corrupted ${lo.crc_errors} · on air ${(lo.airtime_ms / 1000).toFixed(1)} s`));
      }
    }
    const en = b.espnow;
    rows.push(kv("esp_now", `${en.on ? "on" : "off"}${en.sent !== null && en.sent !== undefined ? ` · sent ${en.sent} · received ${en.received}` : ""}`));
  }
  const node = data.node;
  if (node) {
    rows.push(kv("mesh_id", node.id));
    rows.push(kv("mode", node.always_on ? "radio always on (demo)" : `duty cycled · radio ${node.awake ? "awake" : "asleep"}`));
  } else if (data.enabled) {
    rows.push(kv("mesh", "starts once this device is set up (below)"));
  }
  box.replaceChildren(...rows);
}

function renderPeers(node) {
  const now = node.now;
  const session = document.getElementById("mesh-session");
  session.hidden = !node.session;
  if (node.session) {
    const sy = node.session;
    session.textContent = `Syncing with ${sy.peer_name} over ${sy.radio} (started ${ago(sy.started, now)})`;
  }

  const nearby = node.peers.filter((p) => p.last_heard && now - p.last_heard < NEARBY_S).length;
  document.getElementById("mesh-nearby-title").textContent =
    node.peers.length ? `nearby (${nearby} of ${node.peers.length} heard in the last minute)` : "nearby (0)";

  const list = document.getElementById("mesh-peers");
  if (!node.peers.length) {
    list.replaceChildren(meshEl("p", "nobody heard yet: other hikers show up here when their radio is in range", "debug-empty"));
    return;
  }
  const rows = node.peers.map((p) => {
    const y = p.last_sync;
    return [
      p.name,
      p.heard_on ? `${signalBars(p.rssi, p.heard_on)} ${p.rssi} dBm` : "–",
      p.heard_on || "–",
      p.last_heard ? ago(p.last_heard, now) : "–",
      !y ? "never" : y.ok ? `ok ${ago(y.at, now)} over ${y.radio} (got ${y.received}, sent ${y.sent})`
                          : `failed ${ago(y.at, now)} over ${y.radio}`,
    ];
  });
  const table = meshTable(["name", "signal", "heard_on", "last_heard", "last_sync"], rows);
  // Hikers not heard in the last minute: dimmed, like dead rows in the database tables
  table.querySelectorAll("tbody tr").forEach((tr, i) => {
    const p = node.peers[i];
    if (!(p.last_heard && now - p.last_heard < NEARBY_S)) tr.classList.add("stale");
  });
  list.replaceChildren(table);
}

function renderEvents(data, now) {
  const list = document.getElementById("mesh-events");
  document.getElementById("mesh-events-title").textContent = `recent_activity (${data.events.length})`;
  if (!data.events.length) {
    list.replaceChildren(meshEl("p", "empty", "debug-empty"));
    return;
  }
  list.replaceChildren(meshTable(["when", "what_happened"], data.events.map((e) => [ago(e.at, now), e.text])));
}

let lastMesh = null;

function renderMesh(data) {
  document.getElementById("mesh-off").hidden = data.enabled;
  document.getElementById("mesh-on").hidden = !data.enabled;
  if (!data.enabled) return;
  const now = data.node ? data.node.now : Date.now() / 1000;
  renderBoard(data);
  if (data.node) renderPeers(data.node);
  else {
    document.getElementById("mesh-session").hidden = true;
    document.getElementById("mesh-peers").replaceChildren();
    document.getElementById("mesh-nearby-title").textContent = "nearby";
  }
  renderEvents(data, now);
}

async function loadMesh() {
  const data = await fetch("/api/mesh").then((r) => r.json());
  lastMesh = data;
  renderMesh(data);
}

// Refresh while the Mesh tab is open (showTab() in app.js loads it when the tab opens)
setInterval(() => {
  if (!document.hidden && document.getElementById("debug").classList.contains("active")) {
    loadMesh().catch(() => {});  // the local app is always reachable; try again next time
  }
}, MESH_REFRESH_MS);
