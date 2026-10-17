// Map settings: a gear button over the map that opens a menu of subtabs.
// "Last known location" lists everyone with a known position; checking a person pins them.

let lastKnownLayer = null;          // Leaflet layer group with the pins
let lastKnownPeople = [];           // rows from /api/geolocations, newest first
const lastKnownShown = new Set();   // lowercased names of people whose pin is on
let myMapName = null;

function settingsEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so names can't inject HTML
  if (className) node.className = className;
  return node;
}

function showSettingsView(name) {
  document.querySelectorAll("#map-settings-panel .settings-view").forEach((v) => {
    v.hidden = v.dataset.view !== name;
  });
  if (name === "last-known") {
    const search = document.getElementById("last-known-search");
    search.value = "";
    loadLastKnown().then(() => search.focus());
  }
  if (name === "trails") {
    const search = document.getElementById("trails-search");
    search.value = "";
    loadTrails().then(() => search.focus());
  }
  if (name === "locales") {
    const search = document.getElementById("locales-search");
    search.value = "";
    loadGlobalLocales().then(() => search.focus());
  }
}

function timeAgo(seconds) {
  const mins = Math.round((Date.now() / 1000 - seconds) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function lastSeenText(g) {
  return `Last seen ${timeAgo(g.time_created)} · ${new Date(g.time_created * 1000).toLocaleString()}`;
}

function personLabel(name) {
  return myMapName && name.toLowerCase() === myMapName.toLowerCase() ? `${name} (you)` : name;
}

async function loadLastKnown() {
  const [gps, deviceRes] = await Promise.all([
    fetch("/api/geolocations").then((r) => r.json()),
    fetch("/api/device"),
  ]);
  myMapName = deviceRes.ok ? (await deviceRes.json()).name : null;
  lastKnownPeople = gps.reverse();  // the geolocations table holds only each person's latest position
  renderLastKnownList();
  drawLastKnownPins();
}

function renderLastKnownList() {
  const list = document.getElementById("last-known-list");
  const q = document.getElementById("last-known-search").value.trim().toLowerCase();
  const matches = lastKnownPeople.filter((g) => g.user.toLowerCase().includes(q));

  if (!matches.length) {
    list.replaceChildren(settingsEl("p", lastKnownPeople.length ? "No matching people"
                                                                : "No known locations yet", "settings-empty"));
    return;
  }
  list.replaceChildren(...matches.map((g) => {
    const key = g.user.toLowerCase();
    const row = settingsEl("label", undefined, "settings-person");
    const checkbox = settingsEl("input");
    checkbox.type = "checkbox";
    checkbox.checked = lastKnownShown.has(key);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) lastKnownShown.add(key);
      else lastKnownShown.delete(key);
      drawLastKnownPins();
    });
    const text = settingsEl("span");
    text.appendChild(settingsEl("span", personLabel(g.user), "settings-person-name"));
    text.appendChild(settingsEl("span", lastSeenText(g), "settings-person-time"));
    row.append(checkbox, text);
    return row;
  }));
}

function drawLastKnownPins() {
  if (lastKnownLayer) map.removeLayer(lastKnownLayer);
  lastKnownLayer = L.layerGroup(lastKnownPeople
    .filter((g) => lastKnownShown.has(g.user.toLowerCase()))
    .map((g) => {
      // Name and time always shown above the pin
      const label = settingsEl("div");
      label.appendChild(settingsEl("strong", personLabel(g.user)));
      label.appendChild(settingsEl("div", `Last seen ${timeAgo(g.time_created)}`));
      label.appendChild(settingsEl("div", new Date(g.time_created * 1000).toLocaleString()));
      const marker = L.marker([g.latitude, g.longitude], { title: g.user })
        // the default icon's tooltip anchor sits right of the pin; center it just above the pin
        .bindTooltip(label, { permanent: true, interactive: true, direction: "top",
                              offset: [-16, -14], className: "pin-label" });
      // Hovering the pin or its label brings both above the others
      marker.on("mouseover", () => raisePin(marker));
      marker.on("add", () => marker.getTooltip().getElement()
        .addEventListener("mouseenter", () => raisePin(marker)));
      return marker;
    })).addTo(map);
}

let raisedPinZ = 0;

function raisePin(marker) {
  raisedPinZ++;
  marker.setZIndexOffset(1000 * raisedPinZ);  // marker z-index is its latitude plus this offset
  marker.getTooltip().getElement().style.zIndex = raisedPinZ;
}

function initMapSettings() {
  const panel = document.getElementById("map-settings-panel");

  document.getElementById("map-settings-toggle").addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) showSettingsView("menu");
  });
  panel.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => showSettingsView(b.dataset.open)));
  document.getElementById("last-known-search").addEventListener("input", renderLastKnownList);
  initGlobalLocales();
  initLocaleSheet();
  initTrails();

  // Keep the map from panning/zooming when interacting with the menu
  const container = document.getElementById("map-settings");
  L.DomEvent.disableClickPropagation(container);
  L.DomEvent.disableScrollPropagation(container);
}

// Refresh pins that are on, e.g. when coming back to the Map tab
function refreshMapSettings() {
  if (lastKnownShown.size) loadLastKnown();
  loadGlobalLocales();
  loadTrails();
}
