let map = null;

function initMap() {
  const info = window.MAP_INFO;
  const [minLon, minLat, maxLon, maxLat] = info.bbox;

  map = L.map("leaflet-map", {
    minZoom: info.min_zoom,
    maxZoom: info.max_zoom + 3,  // past the data's max zoom the vectors are scaled up
    maxBounds: [[minLat, minLon], [maxLat, maxLon]],
    maxBoundsViscosity: 0.8,
  }).setView(info.center, Math.min(info.min_zoom + 5, info.max_zoom));

  if (!info.name) return;  // no map downloaded yet

  // Vector map served locally by main.py from data/map.pmtiles
  protomapsL.leafletLayer({
    url: "/map.pmtiles",
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
  if (name === "messages") loadMessages();
  if (name === "debug") loadDebug();
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => showTab(t.dataset.tab)));

loadMessages();  // Messages is the tab shown on load
startLocationUpdates();

// The global data (water sources, campsites, trails) arrives from the server
// during sync; redraw it when a new copy lands, without reloading the page.
const GLOBAL_CHECK_MS = 10000;
let globalVersion;
async function checkGlobalData() {
  try {
    const { version } = await fetch("/api/global/info").then((r) => r.json());
    // Before the Map tab is first opened there's nothing drawn yet to update
    if (map && globalVersion !== undefined && version !== globalVersion) {
      loadGlobalLocales();
      loadTrails();
    }
    globalVersion = version;
  } catch (err) {
    // The local app is always reachable; just try again next time
  }
}
checkGlobalData();
setInterval(checkGlobalData, GLOBAL_CHECK_MS);
