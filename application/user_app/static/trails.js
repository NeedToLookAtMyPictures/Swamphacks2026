// Global trails (from the server, as GPX). Only the trail selected in
// Settings > Trails is drawn on the map; selecting one also zooms to it.

let globalTrails = [];      // rows from /api/global/trails: {id, name, description, segments, length_km}
let trailsLayer = null;
let selectedTrailId = null;

const TRAIL_COLOR = "#ff6f00";

function formatTrailLength(km) {
  return `${(km * 0.621371).toFixed(1)} mi (${km.toFixed(1)} km)`;
}

async function loadTrails() {
  globalTrails = await fetch("/api/global/trails").then((r) => r.json());
  if (!globalTrails.some((t) => t.id === selectedTrailId)) {
  selectedTrailId = globalTrails.length ? globalTrails[0].id : null;
  }

  drawTrails();
  renderTrailsList();
}

function drawTrails() {
  if (trailsLayer) map.removeLayer(trailsLayer);
  const trail = globalTrails.find((t) => t.id === selectedTrailId);
  // A white casing under the line keeps it visible on any background
  trailsLayer = L.layerGroup(trail ? [
    L.polyline(trail.segments, { pane: "trails", color: "#fff", weight: 10, opacity: 0.9, interactive: false }),
    L.polyline(trail.segments, { pane: "trails", color: TRAIL_COLOR, weight: 6, interactive: false }),
  ] : []).addTo(map);
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
    list.replaceChildren(...rows);
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
