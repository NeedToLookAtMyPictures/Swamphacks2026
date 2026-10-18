// Debug tab: shows everything in the local database via the /api endpoints.

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

function renderGps() {
  // This device's GPS, from location.js: is it getting fixes, and if not, why
  const section = el("div", undefined, "debug-section");
  section.id = "gps-debug";
  section.appendChild(el("h3", "gps"));
  const rows = [
    ["attempts / fixes", `${gpsDebug.attempts} / ${gpsDebug.fixes}`],
    ["last fix", gpsDebug.last_fix ? `${formatValue("time_created", gpsDebug.last_fix.time)}: ` +
      `${gpsDebug.last_fix.latitude.toFixed(6)}, ${gpsDebug.last_fix.longitude.toFixed(6)} ` +
      `±${gpsDebug.last_fix.accuracy_m} m` : "none yet"],
    ["last error", gpsDebug.last_error ? `${formatValue("time_created", gpsDebug.last_error.time)}: ` +
      `${gpsDebug.last_error.error} ${gpsDebug.last_error.message}` : "none"],
    ["last save", gpsDebug.last_save || "—"],
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

    content.replaceChildren(renderGps());
    if (deviceRes.ok) {
      content.appendChild(renderTable("device_info", [await deviceRes.json()]));
    } else {
      renderDeviceSetup(content, server);
    }
    content.appendChild(renderTable("server", [server]));
    DEBUG_TABLES.forEach((t, i) => content.appendChild(renderTable(t, tables[i])));
    content.appendChild(el("h2", "Global database (from the server, read-only)", "debug-db-title"));
    Object.keys(GLOBAL_DEBUG_TABLES).forEach((t, i) => {
      // Trails come with their parsed line; show its size instead of every point
      const rows = t === "global_trails"
        ? globalTables[i].map(({ segments, ...trail }) => ({
            ...trail, points: segments.reduce((n, seg) => n + seg.length, 0) }))
        : globalTables[i];
      content.appendChild(renderTable(t, Array.isArray(rows) ? rows : [rows]));
    });
    status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
}

document.getElementById("debug-refresh").addEventListener("click", loadDebug);
