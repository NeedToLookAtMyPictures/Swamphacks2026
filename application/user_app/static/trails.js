// Global trails (from the server, as GPX). The Explore tab lists them; only the
// selected trail is drawn on the map, and picking one opens the Map tab zoomed to it.

const TRAIL_COLOR = "#ff6f00";
const SELECTED_TRAIL_KEY = "selectedTrail";  // localStorage key, so the choice survives a reload

let globalTrails = [];      // rows from /api/global/trails: {id, name, description, segments, length_km}
let trailsLayer = null;
let selectedTrailId = loadSelectedTrail();

// Trails we plan to add whose GPX isn't on the server yet; shown in Explore as
// "coming soon" until a global trail matching them arrives
const UPCOMING_TRAILS = [
  { match: /pct|pacific crest/i, name: "Pacific Crest Trail (section)",
    description: "GPX coming soon" },
];

function loadSelectedTrail() {
  try {
    return localStorage.getItem(SELECTED_TRAIL_KEY);
  } catch (err) {
    return null;  // storage blocked; fall back to the first trail
  }
}

function saveSelectedTrail() {
  try {
    if (selectedTrailId) localStorage.setItem(SELECTED_TRAIL_KEY, selectedTrailId);
    else localStorage.removeItem(SELECTED_TRAIL_KEY);
  } catch (err) {
    // Only a convenience; the selection still works for this visit
  }
}

function formatTrailLength(km) {
  return `${(km * 0.621371).toFixed(1)} mi (${km.toFixed(1)} km)`;
}

async function loadTrails() {
  globalTrails = await fetch("/api/global/trails").then((r) => r.json());
  if (!globalTrails.some((t) => t.id === selectedTrailId)) {
    selectedTrailId = globalTrails.length ? globalTrails[0].id : null;
  }

  if (map) drawTrails();
  renderExplore();
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

// Select a trail from Explore: switch to the Map tab and zoom to it
function selectTrail(id) {
  selectedTrailId = id;
  saveSelectedTrail();
  renderExplore();
  showTab("map");
  drawTrails();
  const trail = globalTrails.find((t) => t.id === id);
  if (trail) map.fitBounds(L.latLngBounds(trail.segments.flat()), { padding: [30, 30] });
}

function exploreCard(name, detail, { selected = false, disabled = false, onClick } = {}) {
  const card = settingsEl("button", undefined, "explore-card");
  card.type = "button";
  card.disabled = disabled;
  card.classList.toggle("selected", selected);
  card.appendChild(settingsEl("span", name, "explore-card-name"));
  if (detail) card.appendChild(settingsEl("span", detail, "explore-card-detail"));
  card.appendChild(settingsEl("span", disabled ? "Coming soon" : selected ? "On the map" : "Show on map",
                              "explore-card-action"));
  if (onClick) card.addEventListener("click", onClick);
  return card;
}

function renderExplore() {
  const list = document.getElementById("explore-list");
  const cards = globalTrails.map((t) => exploreCard(t.name,
    [formatTrailLength(t.length_km), t.description].filter(Boolean).join(" · "),
    { selected: t.id === selectedTrailId, onClick: () => selectTrail(t.id) }));
  UPCOMING_TRAILS
    .filter((u) => !globalTrails.some((t) => u.match.test(`${t.id} ${t.name}`)))
    .forEach((u) => cards.push(exploreCard(u.name, u.description, { disabled: true })));
  if (!cards.length) cards.push(settingsEl("p", "No trails yet. They arrive from the server on the next sync.",
                                           "settings-empty"));
  list.replaceChildren(...cards);
}

function initTrails() {
  map.createPane("trails").style.zIndex = 350;  // above the basemap, below locale dots and pins
  loadTrails();
}
