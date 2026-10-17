// Global locales (water sources, campsites, ... set by the company) on the map.
// Settings > Locales lists them grouped by category; each category and each
// locale can be shown or hidden. Everything is shown until you hide it.

let globalLocales = [];             // rows from /api/global/locales, sorted by category then name
let globalLocalesLayer = null;
const hiddenLocales = new Set();    // ids of locales the user has hidden

const CATEGORY_COLORS = {
  water: "#1e88e5", campsite: "#2e7d32", shelter: "#8d6e63",
  trailhead: "#f4511e", restroom: "#8e24aa", "first aid": "#e53935",
};
const EXTRA_COLORS = ["#00897b", "#c0ca33", "#ffb300", "#d81b60", "#546e7a"];  // other categories

function categoryColor(category) {
  if (CATEGORY_COLORS[category]) return CATEGORY_COLORS[category];
  let hash = 0;
  for (const ch of category) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return EXTRA_COLORS[hash % EXTRA_COLORS.length];
}

function categoryLabel(category) {
  return category.charAt(0).toUpperCase() + category.slice(1);
}

async function loadGlobalLocales() {
  globalLocales = await fetch("/api/global/locales").then((r) => r.json());
  // Close the sheet if its locale is gone, e.g. after the global data was reloaded
  if (sheetLocale && !globalLocales.some((l) => l.id === sheetLocale.id)) closeLocaleSheet();
  drawGlobalLocales();
  renderGlobalLocalesList();
}

function drawGlobalLocales() {
  if (globalLocalesLayer) map.removeLayer(globalLocalesLayer);
  globalLocalesLayer = L.layerGroup(globalLocales
    .filter((l) => !hiddenLocales.has(l.id))
    .map((l) => {
      const color = categoryColor(l.category);
      const selected = sheetLocale && sheetLocale.id === l.id;
      const marker = L.circleMarker([l.latitude, l.longitude], {
        radius: selected ? 11 : 8, color: selected ? "#222" : "#fff", weight: selected ? 3 : 2,
        fillColor: color, fillOpacity: 1,
      }).bindTooltip(l.name, { direction: "top", offset: [0, -8] });
      marker.on("mouseover", () => marker.bringToFront());
      marker.on("click", (e) => {
        L.DomEvent.stopPropagation(e);  // a map click closes the sheet
        openLocaleSheet(l);
      });
      if (selected) setTimeout(() => marker.bringToFront());
      return marker;
    })).addTo(map);
}

function setLocalesHidden(ids, hidden) {
  ids.forEach((id) => (hidden ? hiddenLocales.add(id) : hiddenLocales.delete(id)));
  drawGlobalLocales();
  renderGlobalLocalesList();
}

function renderGlobalLocalesList() {
  const list = document.getElementById("locales-list");
  const q = document.getElementById("locales-search").value.trim().toLowerCase();
  const matches = globalLocales.filter((l) =>
    `${l.name} ${l.category} ${l.description}`.toLowerCase().includes(q));

  if (!matches.length) {
    list.replaceChildren(settingsEl("p", globalLocales.length ? "No matching locales"
                                                              : "No locales yet; sync with the server", "settings-empty"));
    return;
  }

  const groups = new Map();  // category -> locales, in the server's sorted order
  matches.forEach((l) => {
    if (!groups.has(l.category)) groups.set(l.category, []);
    groups.get(l.category).push(l);
  });

  const nodes = [];
  groups.forEach((locales, category) => {
    // Category row: toggles every locale in the category (only the ones matching the search)
    const ids = locales.map((l) => l.id);
    const shown = ids.filter((id) => !hiddenLocales.has(id)).length;
    const row = settingsEl("label", undefined, "settings-category");
    const checkbox = settingsEl("input");
    checkbox.type = "checkbox";
    checkbox.checked = shown === ids.length;
    checkbox.indeterminate = shown > 0 && shown < ids.length;
    checkbox.addEventListener("change", () => setLocalesHidden(ids, !checkbox.checked));
    const dot = settingsEl("span", undefined, "settings-dot");
    dot.style.background = categoryColor(category);
    row.append(checkbox, dot, settingsEl("span", categoryLabel(category), "settings-category-name"),
               settingsEl("span", `${shown}/${ids.length}`, "settings-count"));
    nodes.push(row);

    locales.forEach((l) => {
      const item = settingsEl("label", undefined, "settings-locale");
      const box = settingsEl("input");
      box.type = "checkbox";
      box.checked = !hiddenLocales.has(l.id);
      box.addEventListener("change", () => setLocalesHidden([l.id], !box.checked));
      item.append(box, settingsEl("span", l.name));
      nodes.push(item);
    });
  });
  list.replaceChildren(...nodes);
}

function initGlobalLocales() {
  document.getElementById("locales-search").addEventListener("input", renderGlobalLocalesList);
  document.getElementById("locales-show-all").addEventListener("click", () =>
    setLocalesHidden(globalLocales.map((l) => l.id), false));
  document.getElementById("locales-hide-all").addEventListener("click", () =>
    setLocalesHidden(globalLocales.map((l) => l.id), true));
  loadGlobalLocales();  // shown on the map from the start
}
