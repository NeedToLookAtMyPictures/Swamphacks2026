// Locale sheet: clicking a global locale opens a panel below the map (the map
// shrinks to make room) with information about the site and its comments (newest first).
//
// Comments are user `locales` records (database.py) whose `location` is the
// global locale's id, so they spread to other devices like messages do.
//
// The distance is walked along the trails from your last location
// (/api/distances), and the route is drawn while the sheet is open.

let sheetLocale = null;  // the global locale shown in the sheet, or null when closed
let routeLayer = null;   // the route to the locale in the sheet
let sheetSnapshot = null;  // JSON of the data last drawn, so refreshes skip unchanged redraws

const SHEET_REFRESH_MS = 1000;

function sheetEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so comments can't inject HTML
  if (className) node.className = className;
  return node;
}

function formatDistance(meters) {
  const km = meters / 1000;
  const miles = km * 0.621371;
  return miles < 0.1 ? `${Math.round(meters)} m` : `${miles.toFixed(1)} mi (${km.toFixed(1)} km)`;
}

function describeRoute(route) {
  if (route.distance === null) {
    return `${formatDistance(route.straight)} in a straight line from your last location; ` +
           "no trail connects you";
  }
  const offTrail = [];
  if (route.to_trail >= 1) offTrail.push(`${formatDistance(route.to_trail)} to the trail`);
  if (route.from_trail >= 1) offTrail.push(`${formatDistance(route.from_trail)} from it`);
  return `${formatDistance(route.distance)} along the trail from your last location` +
         (offTrail.length ? ` (${offTrail.join(", ")})` : "");
}

function drawRoute(route) {
  if (routeLayer) map.removeLayer(routeLayer);
  routeLayer = route && route.path
    ? L.polyline(route.path, { color: "#1565c0", weight: 4, dashArray: "8 6", interactive: false })
        .addTo(map)
    : null;
}

function openLocaleSheet(locale) {
  sheetLocale = locale;
  document.getElementById("locale-sheet").hidden = false;
  document.getElementById("locale-comment-text").value = "";
  document.getElementById("locale-comment-status").textContent = "";
  sheetSnapshot = null;
  drawGlobalLocales();  // highlight the selected locale
  map.invalidateSize();  // the map just got shorter
  map.setView([locale.latitude, locale.longitude], map.getZoom(), { animate: false });
  loadLocaleSheet();
}

function closeLocaleSheet() {
  sheetLocale = null;
  drawRoute(null);
  document.getElementById("locale-sheet").hidden = true;
  map.invalidateSize();  // the map gets its full height back, keeping its center
  drawGlobalLocales();
}

async function loadLocaleSheet() {
  const locale = sheetLocale;
  const [records, deviceRes, distanceRes] = await Promise.all([
    fetch("/api/locales").then((r) => r.json()),
    fetch("/api/device"),
    fetch(`/api/distances?to=${encodeURIComponent(locale.id)}`),  // fails until you have a position
  ]);
  if (sheetLocale !== locale) return;  // another locale was opened meanwhile
  const me = deviceRes.ok ? (await deviceRes.json()).name : null;
  const route = distanceRes.ok ? (await distanceRes.json()).locales[0] : null;
  const comments = records
    .filter((r) => r.location === locale.id && r.comment)
    .sort((a, b) => b.time_created - a.time_created);  // newest first

  const snapshot = JSON.stringify([me, comments, route]);
  if (snapshot === sheetSnapshot) return;
  sheetSnapshot = snapshot;

  renderLocaleInfo(locale, comments, route);
  drawRoute(route);
  renderLocaleComments(comments, me);
  document.getElementById("locale-comment-form").hidden = !me;
  document.getElementById("locale-comment-setup").hidden = Boolean(me);
}

function renderLocaleInfo(locale, comments, route) {
  const color = categoryColor(locale.category);
  const head = document.getElementById("locale-sheet-head");
  const dot = sheetEl("span", undefined, "settings-dot");
  dot.style.background = color;
  const category = sheetEl("span", categoryLabel(locale.category), "locale-category");
  category.prepend(dot);
  head.replaceChildren(sheetEl("h2", locale.name), category);

  const facts = [];
  if (locale.description) facts.push(["About", locale.description]);
  facts.push(["Location", `${locale.latitude.toFixed(5)}, ${locale.longitude.toFixed(5)}`]);
  if (route) facts.push(["Distance", describeRoute(route)]);
  facts.push(["Comments", comments.length
    ? `${comments.length}, latest ${timeAgo(comments[0].time_created)}`
    : "none yet"]);
  facts.push(["Site ID", locale.id]);

  const list = sheetEl("dl", undefined, "locale-facts");
  facts.forEach(([term, value]) => list.append(sheetEl("dt", term), sheetEl("dd", value)));
  document.getElementById("locale-info").replaceChildren(list);
}

function renderLocaleComments(comments, me) {
  const list = document.getElementById("locale-comments");
  if (!comments.length) {
    list.replaceChildren(sheetEl("p", "No comments yet. Be the first to add one.", "locale-note"));
    return;
  }
  list.replaceChildren(...comments.map((c) => {
    const card = sheetEl("div", undefined, "locale-comment");
    const head = sheetEl("div", undefined, "locale-comment-head");
    const isMe = me && c.sender.toLowerCase() === me.toLowerCase();
    head.append(sheetEl("strong", isMe ? `${c.sender} (you)` : c.sender),
                sheetEl("span", `${timeAgo(c.time_created)} · ${new Date(c.time_created * 1000).toLocaleString()}`));
    card.append(head, sheetEl("div", c.comment, "locale-comment-body"));
    const via = arrivalNote(c);  // how it got here (mesh.js); nothing for comments written here
    if (via) card.appendChild(via);
    return card;
  }));
}

async function submitLocaleComment(event) {
  event.preventDefault();
  const text = document.getElementById("locale-comment-text");
  const status = document.getElementById("locale-comment-status");
  const comment = text.value.trim();
  if (!comment || !sheetLocale) return;
  status.textContent = "Posting…";
  const res = await fetch("/api/locales", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ location: sheetLocale.id, name: sheetLocale.name, comment }),
  });
  if (!res.ok) {
    status.textContent = (await res.json()).error || "Couldn't post the comment";
    return;
  }
  text.value = "";
  status.textContent = "";
  loadLocaleSheet();
}

function initLocaleSheet() {
  document.getElementById("locale-sheet-close").addEventListener("click", closeLocaleSheet);
  document.getElementById("locale-comment-form").addEventListener("submit", submitLocaleComment);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sheetLocale) closeLocaleSheet();
  });
  map.on("click", () => { if (sheetLocale) closeLocaleSheet(); });  // tapping the map closes it

  const sheet = document.getElementById("locale-sheet");
  L.DomEvent.disableClickPropagation(sheet);
  L.DomEvent.disableScrollPropagation(sheet);

  // Pick up comments that arrive in the background (sync, other devices)
  setInterval(() => {
    if (sheetLocale && document.getElementById("map").classList.contains("active") && !document.hidden) {
      loadLocaleSheet().catch(() => {});  // try again next time
    }
  }, SHEET_REFRESH_MS);
}
