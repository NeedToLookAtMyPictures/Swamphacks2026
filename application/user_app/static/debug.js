// Mesh tab (panel id "debug"): shows everything in the local database via the /api endpoints.

const DEBUG_TABLES = ["messages", "locales", "geolocations", "tombstones"];
const GLOBAL_DEBUG_TABLES = { global_locales: "/api/global/locales",
                              global_trails: "/api/global/trails", global_info: "/api/global/info" };
const TIME_FIELDS = new Set(["time_created", "time_received", "last_server_sync",
                             "loaded_at"]);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so data can't inject HTML
  if (className) node.className = className;
  return node;
}

function formatValue(key, value) {
  // received_via: how the row got here (server, LoRa, ESP-NOW, sync API); never shared
  if (key === "received_via") return value || "this device";
  if (value === null || value === undefined) return "—";
  if (TIME_FIELDS.has(key) && typeof value === "number") {
    return new Date(value * 1000).toLocaleString();
  }
  return String(value);
}

function renderTable(title, rows) {
  const section = el("div", undefined, "debug-section");
  section.appendChild(el("h3", `${title} (${rows.length})`));
  if (rows.length === 0) {
    section.appendChild(el("p", "empty", "debug-empty"));
    return section;
  }
  const columns = Object.keys(rows[0]);
  const table = el("table");
  const head = table.createTHead().insertRow();
  columns.forEach((c) => head.appendChild(el("th", c)));
  const body = table.createTBody();
  rows.forEach((row) => {
    const tr = body.insertRow();
    columns.forEach((c) => {
      const td = tr.insertCell();
      td.textContent = formatValue(c, row[c]);
      if (c === "is_dead" && row[c]) tr.classList.add("dead");
    });
  });
  const wrap = el("div", undefined, "debug-table-wrap");
  wrap.appendChild(table);
  section.appendChild(wrap);
  return section;
}

let gpsPath = null;  // positionPath() as of the last Refresh

function renderGps() {
  // This device's GPS, from location.js: is it getting fixes, where the position
  // got to (database, server), and if something fails, why
  const section = el("div", undefined, "debug-section");
  section.id = "gps-debug";
  section.appendChild(el("h3", "gps"));
  const rows = [
    ["attempts / fixes", `${gpsDebug.attempts} / ${gpsDebug.fixes}`],
    ["from the phone (last fix)", gpsDebug.last_fix ? `${formatValue("time_created", gpsDebug.last_fix.time)}: ` +
      `${gpsDebug.last_fix.latitude.toFixed(6)}, ${gpsDebug.last_fix.longitude.toFixed(6)} ` +
      `±${gpsDebug.last_fix.accuracy_m} m` : "none yet"],
    ["last error", gpsDebug.last_error ? `${formatValue("time_created", gpsDebug.last_error.time)}: ` +
      `${gpsDebug.last_error.error} ${gpsDebug.last_error.message}` : "none"],
    ["last save", gpsDebug.last_save || "—"],
    ...(gpsPath ? gpsPath.rows : []),
    ...(gpsPath ? gpsPath.advice.map((a, i) => [i === 0 ? "problem" : "", a]) : []),
  ];
  const check = gpsDebug.check;
  if (check) {
    rows.push(["check", check.running ? "running…" : `ran ${formatValue("time_created", check.time)}`]);
    check.findings.forEach(([k, v]) => rows.push([k, v]));
    check.advice.forEach((a, i) => rows.push([i === 0 ? "what to do" : "", a]));
  }
  const table = el("table", undefined, "debug-kv");
  const body = table.createTBody();
  rows.forEach(([k, v]) => {
    const tr = body.insertRow();
    tr.appendChild(el("th", k));
    tr.insertCell().textContent = v;
  });
  const wrap = el("div", undefined, "debug-table-wrap");
  wrap.appendChild(table);
  const button = el("button", check && check.running ? "Checking…" : "Run GPS check");
  button.disabled = Boolean(check && check.running);
  button.addEventListener("click", () => {
    runGpsCheck(gpsDebug.fixes === 0 ? gpsDebug.last_error : null);  // why the last try failed, if none worked
    refreshGpsDebug();
  });
  section.append(wrap, button);
  return section;
}

function refreshGpsDebug() {
  const old = document.getElementById("gps-debug");
  if (old) old.replaceWith(renderGps());
}

function renderKill() {
  // --phone only: stop the app (Termux keeps it running in the background otherwise)
  const section = el("div", undefined, "debug-section");
  section.appendChild(el("h3", "app"));
  const button = el("button", "Kill app");
  const status = el("p", undefined, "debug-error");
  button.addEventListener("click", async () => {
    if (!confirm("Stop the app? It has to be started again from Termux.")) return;
    button.disabled = true;
    try {
      const res = await fetch("/api/quit", { method: "POST" });
      status.textContent = res.ok ? "Stopped. Start it again from Termux." : (await res.json()).error;
    } catch (err) {
      status.textContent = `Error: ${err.message}`;
    }
  });
  section.append(button, status);
  return section;
}

function renderDeviceSetup(content, server) {
  // No identity yet: create one. Names are reserved on the server, so it must be reachable.
  const section = el("div", undefined, "debug-section");
  section.appendChild(el("h3", "device_info"));
  section.appendChild(el("p", "Device not initialized. Its name and IDs can't be changed later."));
  section.appendChild(el("p", `Creating an account reserves your name on the server, so it ` +
    `needs ${server.looking_for}: connect to the internet or to the server's wifi first.`));
  const form = el("form", undefined, "debug-form");
  const input = el("input");
  input.placeholder = "Name";
  input.required = true;
  const button = el("button", "Create device");
  form.append(input, button);
  const error = el("p", undefined, "debug-error");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    button.disabled = true;
    error.textContent = "Contacting the server…";
    try {
      const res = await fetch("/api/device", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: input.value }),
      });
      if (!res.ok) {
        error.textContent = (await res.json()).error;
        return;
      }
      loadDebug();
    } finally {
      button.disabled = false;
    }
  });
  section.append(form, error);
  content.appendChild(section);
}

// A single record (device_info, server) as two columns: field names in header cells
// down the left and values beside them, like the this_device table above.
function renderRecord(title, record) {
  const section = el("div", undefined, "debug-section");
  section.appendChild(el("h3", title));
  const table = el("table");
  const body = table.createTBody();
  Object.entries(record).forEach(([key, value]) => {
    const tr = body.insertRow();
    tr.append(el("th", key), el("td", formatValue(key, value)));
  });
  const wrap = el("div", undefined, "debug-table-wrap");
  wrap.appendChild(table);
  section.appendChild(wrap);
  return section;
}

// "debug": a heading-style toggle that shows or hides the raw tables below it.
// Hidden by default; the choice is remembered per browser (like the chats sidebar).
const DEBUG_SHOWN_KEY = "debugTablesShown";

function debugShown() {
  try { return localStorage.getItem(DEBUG_SHOWN_KEY) === "1"; } catch { return false; }
}

function debugToggle(extra, count) {
  const button = el("button", undefined, "debug-toggle");
  button.type = "button";
  button.setAttribute("aria-controls", "debug-extra");
  const show = (open) => {
    extra.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.replaceChildren(el("span", open ? "▾ " : "▸ ", "debug-toggle-caret"), "debug",
                           el("span", open ? "" : ` (${count} tables)`, "debug-toggle-count"));
  };
  show(debugShown());
  button.addEventListener("click", () => {
    const open = extra.hidden;
    try { localStorage.setItem(DEBUG_SHOWN_KEY, open ? "1" : ""); } catch {}
    show(open);
  });
  return button;
}

async function loadDebug() {
  const content = document.getElementById("debug-content");
  const status = document.getElementById("debug-status");
  status.textContent = "Loading…";
  try {
    const deviceRes = await fetch("/api/device");
    const server = await fetch("/api/server").then((r) => r.json());
    const tables = await Promise.all(
      DEBUG_TABLES.map((t) => fetch(`/api/${t}`).then((r) => r.json())));
    const globalTables = await Promise.all(
      Object.values(GLOBAL_DEBUG_TABLES).map((url) => fetch(url).then((r) => r.json())));

    content.replaceChildren();
    const device = deviceRes.ok ? await deviceRes.json() : null;
    gpsPath = positionPath(device, server, tables[DEBUG_TABLES.indexOf("geolocations")]);
    if (device) {
      content.appendChild(renderRecord("device_info", device));
    } else {
      renderDeviceSetup(content, server);
    }
    content.appendChild(renderRecord("server", server));
    // Everything from messages down sits behind the "debug" toggle
    const extra = el("div", undefined, "debug-extra");
    extra.id = "debug-extra";
    DEBUG_TABLES.forEach((t, i) => extra.appendChild(renderTable(t, tables[i])));
    extra.appendChild(el("h2", "global_database (from the server, read-only)", "debug-db-title"));
    Object.keys(GLOBAL_DEBUG_TABLES).forEach((t, i) => {
      // Trails come with their parsed line; show its size instead of every point
      const rows = t === "global_trails"
        ? globalTables[i].map(({ segments, ...trail }) => ({
            ...trail, points: segments.reduce((n, seg) => n + seg.length, 0) }))
        : globalTables[i];
      extra.appendChild(renderTable(t, Array.isArray(rows) ? rows : [rows]));
    });
    // This device's GPS check, last (it's below messages, so it's behind the toggle too)
    extra.appendChild(renderGps());
    if (window.PHONE) extra.appendChild(renderKill());  // at the very end
    const count = DEBUG_TABLES.length + Object.keys(GLOBAL_DEBUG_TABLES).length + 1;
    content.append(debugToggle(extra, count), extra);
    status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
}

document.getElementById("debug-refresh").addEventListener("click", loadDebug);
