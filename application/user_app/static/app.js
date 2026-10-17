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
    if (!map) initMap();
    map.invalidateSize();  // Leaflet needs this after the container becomes visible
  }
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => showTab(t.dataset.tab)));
