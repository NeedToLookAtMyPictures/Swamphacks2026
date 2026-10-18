// Global trails (from the server, as GPX). The Explore tab lists them; only the
// selected trail is drawn on the map, and picking one opens the Map tab zoomed to it,
// on the offline map made for it.

const TRAIL_COLOR = "#ff6f00";
const SELECTED_TRAIL_KEY = "selectedTrail";  // localStorage key, so the choice survives a reload

let globalTrails = [];      // rows from /api/global/trails: {id, name, description, segments, length_km}
let trailsLayer = null;
let selectedTrailId = loadSelectedTrail();

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

  if (map) {
    drawTrails();
    showTrailMap(false);  // the trails may have just arrived, or changed
  }
  renderExplore();
}

function trailBounds(trail) {
  return L.latLngBounds(trail.segments.flat());
}

function mapBounds(info) {
  const [minLon, minLat, maxLon, maxLat] = info.bbox;
  return L.latLngBounds([minLat, minLon], [maxLat, maxLon]);
}

// The offline map for a trail: the one made for it, else one whose area holds the whole
// trail, else a stand-in without tiles around the trail (id null)
function mapForTrail(trail) {
  const bounds = trailBounds(trail);
  const found = window.MAPS.find((m) => m.trail === trail.id)
    || window.MAPS.find((m) => mapBounds(m).contains(bounds));
  if (found) return found;
  const area = bounds.pad(0.2);
  return { id: null, name: null, version: null, min_zoom: 3, max_zoom: 15,
           bbox: [area.getWest(), area.getSouth(), area.getEast(), area.getNorth()],
           center: [bounds.getCenter().lat, bounds.getCenter().lng] };
}

// Show the selected trail's map (or the first map when no trail is selected). Only
// when it isn't already shown, or `fit` asks to zoom to the trail anyway.
function showTrailMap(fit) {
  const trail = globalTrails.find((t) => t.id === selectedTrailId);
  const info = trail ? mapForTrail(trail) : window.MAPS[0] || window.DEFAULT_MAP;
  const changed = !currentMap || info.id !== currentMap.id || info.version !== currentMap.version
    || (!info.id && (info.bbox + "") !== (currentMap.bbox + ""));
  if (changed) showMapData(info);
  if (trail && (fit || changed)) map.fitBounds(trailBounds(trail), { padding: [30, 30] });
  else if (changed) map.setView(info.center, Math.min(info.min_zoom + 5, info.max_zoom));
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
  showTrailMap(true);
}

function exploreCard(name, detail, selected, onClick) {
  const card = settingsEl("button", undefined, "explore-card");
  card.type = "button";
  card.classList.toggle("selected", selected);
  card.appendChild(settingsEl("span", name, "explore-card-name"));
  if (detail) card.appendChild(settingsEl("span", detail, "explore-card-detail"));
  card.appendChild(settingsEl("span", selected ? "On the map" : "Show on map", "explore-card-action"));
  card.addEventListener("click", onClick);
  return card;
}

function renderExplore() {
  const list = document.getElementById("explore-list");
  const cards = globalTrails.map((t) => exploreCard(t.name,
    [formatTrailLength(t.length_km), t.description].filter(Boolean).join(" · "),
    t.id === selectedTrailId, () => selectTrail(t.id)));
  if (!cards.length) cards.push(settingsEl("p", "No trails yet. They arrive from the server on the next sync.",
                                           "settings-empty"));
  list.replaceChildren(...cards);
}

function initTrails() {
  map.createPane("trails").style.zIndex = 350;  // above the basemap, below locale dots and pins
  loadTrails();
}
