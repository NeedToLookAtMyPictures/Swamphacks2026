let map = null;
let mapLayer = null;
let currentMap = null;  // the map shown: one of window.MAPS, or a stand-in without tiles (id null)

function initMap() {
  map = L.map("leaflet-map", { maxBoundsViscosity: 0.8 });
  showTrailMap(false);
}

// Limit the map to info's area and draw its tiles; called again when another map is shown
function showMapData(info) {
  currentMap = info;
  const [minLon, minLat, maxLon, maxLat] = info.bbox;
  map.setMinZoom(info.min_zoom);
  map.setMaxZoom(info.max_zoom + 3);  // past the data's max zoom the vectors are scaled up
  map.setMaxBounds([[minLat, minLon], [maxLat, maxLon]]);

  if (mapLayer) map.removeLayer(mapLayer);
  mapLayer = null;
  document.getElementById("no-map").hidden = Boolean(info.id || window.MAPS.length);
  document.getElementById("no-trail-map").hidden = Boolean(info.id || !window.MAPS.length);
  if (!info.id) return;  // no map for this area

  // Vector map served locally by main.py from data/maps/<id>.pmtiles. The version in
  // the URL makes the layer read the new file instead of what it cached from the old one.
  mapLayer = protomapsL.leafletLayer({
    url: `/maps/${encodeURIComponent(info.id)}.pmtiles?v=${encodeURIComponent(info.version || "")}`,
    flavor: "light",
    lang: "en",
    maxDataZoom: info.max_zoom,
  }).addTo(map);
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === name));

  if (name === "map") {
    if (!map) {
      initMap();
      initMapSettings();
    } else {
      refreshMapSettings();
    }
    map.invalidateSize();  // Leaflet needs this after the container becomes visible
  }
  if (name === "explore") loadTrails();
  if (name === "messages") loadMessages();
  if (name === "debug") {
    loadDebug();
    loadMesh();  // the mesh status sits at the top of the Debug tab
  }
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => showTab(t.dataset.tab)));

// Enter sends a message or comment; Shift+Enter adds a new line
["compose-content", "locale-comment-text"].forEach((id) => {
  const box = document.getElementById(id);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      box.form.requestSubmit();
    }
  });
});

loadMessages();  // Messages is the tab shown on load
startLocationUpdates();

// The global data (water sources, campsites, trails) and the offline maps arrive
// from the server during sync; redraw them when a new copy lands, without
// reloading the page.
const GLOBAL_CHECK_MS = 10000;
let globalVersion;
async function checkGlobalData() {
  try {
    const { version } = await fetch("/api/global/info").then((r) => r.json());
    // Before the Map tab is first opened there are no locales drawn yet to update
    if (globalVersion !== undefined && version !== globalVersion) {
      if (map) loadGlobalLocales();
      loadTrails();  // also refreshes the Explore list
    }
    globalVersion = version;

    const maps = await fetch("/api/maps").then((r) => r.json());
    if (JSON.stringify(maps) !== JSON.stringify(window.MAPS)) {
      window.MAPS = maps;  // initMap() uses them if the Map tab isn't open yet
      if (map) showTrailMap(false);
    }
  } catch (err) {
    // The local app is always reachable; just try again next time
  }
}
checkGlobalData();
setInterval(checkGlobalData, GLOBAL_CHECK_MS);
