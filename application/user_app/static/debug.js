// Debug tab: shows everything in the local database via the /api endpoints.

const DEBUG_TABLES = ["messages", "locales", "geolocations", "tombstones"];
const TIME_FIELDS = new Set(["time_created", "time_received", "last_server_sync"]);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so data can't inject HTML
  if (className) node.className = className;
  return node;
}

function formatValue(key, value) {
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

function renderDeviceSetup(content) {
  // No identity yet: offer to create one so the rest of the API can be used
  const section = el("div", undefined, "debug-section");
  section.appendChild(el("h3", "device_info"));
  section.appendChild(el("p", "Device not initialized. Its name and IDs can't be changed later."));
  const form = el("form", undefined, "debug-form");
  const input = el("input");
  input.placeholder = "Name";
  input.required = true;
  form.append(input, el("button", "Create device"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await fetch("/api/device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: input.value }),
    });
    loadDebug();
  });
  section.appendChild(form);
  content.appendChild(section);
}

async function loadDebug() {
  const content = document.getElementById("debug-content");
  const status = document.getElementById("debug-status");
  status.textContent = "Loading…";
  try {
    const deviceRes = await fetch("/api/device");
    const tables = await Promise.all(
      DEBUG_TABLES.map((t) => fetch(`/api/${t}`).then((r) => r.json())));

    content.replaceChildren();
    if (deviceRes.ok) {
      content.appendChild(renderTable("device_info", [await deviceRes.json()]));
    } else {
      renderDeviceSetup(content);
    }
    DEBUG_TABLES.forEach((t, i) => content.appendChild(renderTable(t, tables[i])));
    status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
}

document.getElementById("debug-refresh").addEventListener("click", loadDebug);
