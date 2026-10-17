// Global trails (from the server, as GPX) are always drawn on the map.
// Settings > Trails lists them; selecting one highlights it and zooms to it.

let globalTrails = [];      // rows from /api/global/trails: {id, name, description, segments, length_km}
let trailsLayer = null;
let selectedTrailId = null;

const TRAIL_COLOR = "#6d4c41";
const SELECTED_TRAIL_COLOR = "#ff6f00";

function formatTrailLength(km) {
  return `${(km * 0.621371).toFixed(1)} mi (${km.toFixed(1)} km)`;
}

async function loadTrails() {
  globalTrails = await fetch("/api/global/trails").then((r) => r.json());
  if (!globalTrails.some((t) => t.id === selectedTrailId)) selectedTrailId = null;
  drawTrails();
  renderTrailsList();
}

function drawTrails() {
  if (trailsLayer) map.removeLayer(trailsLayer);
  // Selected trail last, so it's drawn on top
  const ordered = [...globalTrails].sort((a, b) =>
    (a.id === selectedTrailId) - (b.id === selectedTrailId));
  trailsLayer = L.layerGroup(ordered.flatMap((t) => {
    const selected = t.id === selectedTrailId;
    // A white casing under the line keeps it visible on any background
    const casing = L.polyline(t.segments, { pane: "trails", color: "#fff", weight: selected ? 10 : 7,
                                            opacity: 0.9, interactive: false });
    const line = L.polyline(t.segments, { pane: "trails", color: selected ? SELECTED_TRAIL_COLOR : TRAIL_COLOR,
                                          weight: selected ? 6 : 4 })
      .bindTooltip(`${t.name} · ${formatTrailLength(t.length_km)}`, { sticky: true });
    return [casing, line];
  })).addTo(map);
}

function selectTrail(id) {
  selectedTrailId = id;
  drawTrails();
  renderTrailsList();
  const trail = globalTrails.find((t) => t.id === id);
  if (trail) map.fitBounds(L.latLngBounds(trail.segments.flat()), { padding: [30, 30] });
}

function renderTrailsList() {
  const list = document.getElementById("trails-list");
  const q = document.getElementById("trails-search").value.trim().toLowerCase();
  const matches = globalTrails.filter((t) => `${t.name} ${t.description}`.toLowerCase().includes(q));

  if (!globalTrails.length) {
    list.replaceChildren(settingsEl("p", "No trails yet; sync with the server", "settings-empty"));
    return;
  }
  const row = (id, name, detail) => {
    const label = settingsEl("label", undefined, "settings-person");
    const radio = settingsEl("input");
    radio.type = "radio";
    radio.name = "trail";
    radio.checked = id === selectedTrailId;
    radio.addEventListener("change", () => selectTrail(id));
    const text = settingsEl("span");
    text.appendChild(settingsEl("span", name, "settings-person-name"));
    if (detail) text.appendChild(settingsEl("span", detail, "settings-person-time"));
    label.append(radio, text);
    return label;
  };
  const rows = matches.map((t) => row(t.id, t.name,
    [formatTrailLength(t.length_km), t.description].filter(Boolean).join(" · ")));
  if (!matches.length) rows.push(settingsEl("p", "No matching trails", "settings-empty"));
  list.replaceChildren(row(null, "None", "No trail selected"), ...rows);
}

function initTrails() {
  map.createPane("trails").style.zIndex = 350;  // above the basemap, below locale dots and pins
  document.getElementById("trails-search").addEventListener("input", renderTrailsList);
  loadTrails();
}
