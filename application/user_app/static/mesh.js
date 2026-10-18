// Mesh status, at the top of the Debug tab: what the radio is doing (board, nearby
// hikers, the current sync, recent activity), plus the badge on the Debug tab button. Also arrivalText(), used by the Messages,
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

// ---- mesh status (top of the Debug tab) -------------------------------------------

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

function kv(label, value) {
  const row = meshEl("div", undefined, "mesh-kv");
  row.append(meshEl("span", label, "mesh-k"), meshEl("span", value, "mesh-v"));
  return row;
}

function renderBadge(data) {
  const badge = document.getElementById("mesh-badge");
  let state = "off";
  let text = "off";
  if (data.enabled) {
    const node = data.node;
    if (data.board && !data.board.connected) {
      state = "error";
      text = "no board";
    } else if (!node) {
      state = "idle";
      text = "starting";
    } else if (node.session) {
      state = "syncing";
      text = `syncing · ${node.session.radio}`;
    } else {
      state = node.nearby ? "ok" : "idle";
      text = node.nearby ? `${node.nearby} nearby` : "listening";
    }
  }
  badge.className = `mesh-badge ${state}`;
  badge.textContent = text;
}

function renderBoard(data) {
  const box = document.getElementById("mesh-board");
  const rows = [];
  const b = data.board;
  rows.push(kv("Board", `${data.board_kind === "heltec" ? "Heltec LoRa (LoRa + ESP-NOW)" : "ESP-NOW"} on ${data.port}`));
  if (b) {
    rows.push(kv("Connection", b.connected ? `connected${b.mac ? ` · ${b.mac}` : ""}` : "board unplugged, waiting for it"));
    if (b.lora) {
      const lo = b.lora;
      rows.push(kv("LoRa", `${lo.on ? "on" : "off"} · ${lo.freq_mhz.toFixed(1)} MHz, SF${lo.sf}, ${lo.bw_khz} kHz, ${lo.power_dbm} dBm`));
      if (lo.sent !== null && lo.sent !== undefined) {
        rows.push(kv("", `sent ${lo.sent} · received ${lo.received} · corrupted ${lo.crc_errors} · on air ${(lo.airtime_ms / 1000).toFixed(1)} s`));
      }
    }
    const en = b.espnow;
    rows.push(kv("ESP-NOW", `${en.on ? "on" : "off"}${en.sent !== null && en.sent !== undefined ? ` · sent ${en.sent} · received ${en.received}` : ""}`));
  }
  const node = data.node;
  if (node) {
    rows.push(kv("Mesh id", node.id));
    rows.push(kv("Mode", node.always_on ? "radio always on (demo)" : `duty cycled · radio ${node.awake ? "awake" : "asleep"}`));
  } else if (data.enabled) {
    rows.push(kv("Mesh", "starts once this device is set up (Debug tab)"));
  }
  box.replaceChildren(...rows);
}

function renderPeers(node) {
  const now = node.now;
  const session = document.getElementById("mesh-session");
  if (node.session) {
    const s = node.session;
    session.replaceChildren(meshEl("div",
      `Syncing with ${s.peer_name} over ${s.radio} (started ${ago(s.started, now)})`, "mesh-syncing"));
  } else {
    session.replaceChildren();
  }

  const nearby = node.peers.filter((p) => p.last_heard && now - p.last_heard < NEARBY_S).length;
  document.getElementById("mesh-nearby-count").textContent =
    node.peers.length ? `${nearby} of ${node.peers.length} heard in the last minute` : "";

  const list = document.getElementById("mesh-peers");
  if (!node.peers.length) {
    list.replaceChildren(meshEl("p", "Nobody heard yet. Other hikers show up here when their radio is in range.", "mesh-note"));
    return;
  }
  list.replaceChildren(...node.peers.map((p) => {
    const row = meshEl("div", undefined, "mesh-peer");
    const fresh = p.last_heard && now - p.last_heard < NEARBY_S;
    if (!fresh) row.classList.add("stale");
    const head = meshEl("div", undefined, "mesh-peer-head");
    head.append(meshEl("strong", p.name),
                meshEl("span", p.heard_on ? `${signalBars(p.rssi, p.heard_on)} ${p.rssi} dBm · ${p.heard_on}` : "", "mesh-signal"));
    row.appendChild(head);
    const bits = [];
    if (p.last_heard) bits.push(`heard ${ago(p.last_heard, now)}`);
    if (p.last_sync) {
      const y = p.last_sync;
      bits.push(y.ok ? `synced ${ago(y.at, now)} over ${y.radio} (got ${y.received}, sent ${y.sent})`
                     : `last sync failed ${ago(y.at, now)} over ${y.radio}`);
    }
    row.appendChild(meshEl("div", bits.join(" · "), "mesh-note"));
    return row;
  }));
}

function renderEvents(data, now) {
  const list = document.getElementById("mesh-events");
  if (!data.events.length) {
    list.replaceChildren(meshEl("p", "Nothing yet.", "mesh-note"));
    return;
  }
  list.replaceChildren(...data.events.map((e) => {
    const row = meshEl("div", undefined, "mesh-event");
    row.append(meshEl("span", ago(e.at, now), "mesh-event-time"), meshEl("span", e.text));
    return row;
  }));
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
    document.getElementById("mesh-session").replaceChildren();
    document.getElementById("mesh-peers").replaceChildren();
    document.getElementById("mesh-nearby-count").textContent = "";
  }
  renderEvents(data, now);
}

async function loadMesh() {
  const data = await fetch("/api/mesh").then((r) => r.json());
  lastMesh = data;
  renderBadge(data);
  if (document.getElementById("debug").classList.contains("active")) renderMesh(data);
}

// The badge is always visible, so keep polling while the page is shown
loadMesh().catch(() => {});
setInterval(() => {
  if (!document.hidden) loadMesh().catch(() => {});  // the local app is always reachable; try again next time
}, MESH_REFRESH_MS);
