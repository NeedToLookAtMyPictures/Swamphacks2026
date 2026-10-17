let map = null;

function initMap() {
  const info = window.MAP_INFO;
  const [minLon, minLat, maxLon, maxLat] = info.bbox;

  map = L.map("leaflet-map", {
    minZoom: info.min_zoom,
    maxZoom: info.max_zoom,
    maxBounds: [[minLat, minLon], [maxLat, maxLon]],
    maxBoundsViscosity: 0.8,
  }).setView(info.center, Math.min(info.min_zoom + 3, info.max_zoom));

  // Tiles are served locally by main.py from data/tiles
  L.tileLayer("/tiles/{z}/{x}/{y}.png", {
    minZoom: info.min_zoom,
    maxZoom: info.max_zoom,
    attribution: "&copy; OpenStreetMap contributors",
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
