// Map settings: a gear button over the map that opens a searchable list of toggles.

const mapLayers = {};  // option id -> Leaflet layer while the option is on

const MAP_OPTIONS = [
  {
    id: "last-seen",
    label: "Last seen location",
    description: "Pin where each person was last seen",
    enable: showLastSeen,
  },
];

function settingsEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so names can't inject HTML
  if (className) node.className = className;
  return node;
}

// One pin per user in the geolocations table (only their most recent position is kept)
async function showLastSeen() {
  const [gps, deviceRes] = await Promise.all([
    fetch("/api/geolocations").then((r) => r.json()),
    fetch("/api/device"),
  ]);
  const me = deviceRes.ok ? (await deviceRes.json()).name : null;

  return L.layerGroup(gps.map((g) => {
    const popup = settingsEl("div");
    const isMe = me && g.user.toLowerCase() === me.toLowerCase();
    popup.appendChild(settingsEl("strong", isMe ? `${g.user} (you)` : g.user));
    popup.appendChild(settingsEl("div", `Last seen ${new Date(g.time_created * 1000).toLocaleString()}`));
    return L.marker([g.latitude, g.longitude], { title: g.user }).bindPopup(popup);
  }));
}

const optionWanted = {};  // option id -> latest requested state, so a slow fetch can't undo a toggle

async function setOption(option, on) {
  optionWanted[option.id] = on;
  const layer = on ? await option.enable() : null;
  if (optionWanted[option.id] !== on) return;  // toggled again while loading
  if (mapLayers[option.id]) map.removeLayer(mapLayers[option.id]);
  if (layer) mapLayers[option.id] = layer.addTo(map);
  else delete mapLayers[option.id];
}

function initMapSettings() {
  const panel = document.getElementById("map-settings-panel");
  const search = document.getElementById("map-settings-search");
  const list = document.getElementById("map-settings-list");

  document.getElementById("map-settings-toggle").addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) search.focus();
  });

  const rows = MAP_OPTIONS.map((option) => {
    const row = settingsEl("label", undefined, "map-setting");
    const checkbox = settingsEl("input");
    checkbox.type = "checkbox";
    checkbox.addEventListener("change", () => setOption(option, checkbox.checked));
    const text = settingsEl("span");
    text.appendChild(settingsEl("span", option.label, "map-setting-label"));
    text.appendChild(settingsEl("span", option.description, "map-setting-desc"));
    row.append(checkbox, text);
    return { option, row };
  });
  const empty = settingsEl("p", "No matching settings", "map-setting-empty");
  list.replaceChildren(...rows.map((r) => r.row), empty);

  search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach(({ option, row }) => {
      const match = `${option.label} ${option.description}`.toLowerCase().includes(q);
      row.hidden = !match;
      if (match) shown++;
    });
    empty.hidden = shown > 0;
  });
  empty.hidden = true;

  // Keep the map from panning/zooming when interacting with the menu
  const container = document.getElementById("map-settings");
  L.DomEvent.disableClickPropagation(container);
  L.DomEvent.disableScrollPropagation(container);
}

// Refresh pins that are on, e.g. when coming back to the Map tab
function refreshMapSettings() {
  MAP_OPTIONS.filter((o) => mapLayers[o.id]).forEach((o) => setOption(o, true));
}
