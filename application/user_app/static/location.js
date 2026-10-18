// Report this device's position to the local server once a minute.
//
// Works offline: the browser's Geolocation API reads the device's GPS, which
// needs no internet. (Devices without GPS, like most laptops, locate themselves
// from Wi-Fi networks, which does need internet, so offline they may time out.)
// Geolocation only runs on localhost or HTTPS, so when a phone opens the app
// on another device (e.g. a Raspberry Pi over Bluetooth), start it with --https.
//
// The position is watched continuously so the GPS stays on: a cold first fix
// can take minutes (more without assist data, e.g. on GrapheneOS), and a
// one-off request with a timeout would turn the GPS off before it locks.
//
// If there's no fix within FIRST_FIX_WAIT_MS, runGpsCheck() works out why (the
// watch keeps trying). Its findings show in the Debug tab's gps section (gpsDebug).

const LOCATION_INTERVAL_MS = 60 * 1000;
const FIRST_FIX_WAIT_MS = 30 * 1000;
const WATCH_OPTIONS = {
  enableHighAccuracy: true,  // prefer GPS over network-based positioning
  maximumAge: 30 * 1000,     // a fix from the last 30 s is good enough
  // no timeout: keep the GPS on until it gets a fix
};
// The check's second try: any position at all, even an old or network-based one
const CHECK_OPTIONS = { enableHighAccuracy: false, timeout: 15 * 1000, maximumAge: Infinity };

const GEO_ERRORS = { 1: "PERMISSION_DENIED", 2: "POSITION_UNAVAILABLE", 3: "TIMEOUT" };

// Read by the Debug tab
const gpsDebug = {
  watching_since: null,
  fixes: 0,
  last_fix: null,    // {time, latitude, longitude, accuracy_m}
  last_error: null,  // {time, code, error, message}
  last_save: null,   // what PUT /api/location answered
  last_sent: null,   // {time, latitude, longitude} as sent to PUT /api/location
  check: null,       // runGpsCheck() result: {time, running, findings: [[name, value]], advice: [...]}
};

function formatPosition(lat, lon) {
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
}

function formatTime(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}

// Where the position got to after the browser: this device's database, then the
// server. device, server and geolocations are what /api/device, /api/server and
// /api/geolocations answered. Returns [[step, result]] and advice for any gap.
function positionPath(device, server, geolocations) {
  const rows = [];
  const advice = [];
  const sent = gpsDebug.last_sent;
  rows.push(["sent to this app", sent ? `${formatPosition(sent.latitude, sent.longitude)} at ${formatTime(sent.time)}` +
    ` (${gpsDebug.last_save})` : "nothing yet"]);
  if (!device) {
    rows.push(["saved in this device's database", "no: device not set up"]);
    return { rows, advice };
  }
  const mine = geolocations.find((g) => g.user.toLowerCase() === device.name.toLowerCase());
  rows.push(["saved in this device's database", mine
    ? `${formatPosition(mine.latitude, mine.longitude)} at ${formatTime(mine.time_created)}` : "none"]);
  if (sent && !mine) advice.push("A position was sent but isn't in the database: see 'sent to this app' above.");
  if (!mine) return { rows, advice };

  if (!server.url) {
    rows.push(["sent to the server", `no: no server connected (looking for ${server.looking_for})`]);
    advice.push("The position is saved on this device, but no server answers, so the server can't have it. " +
                "Connect to the internet or the server's wifi; it's sent at the next sync.");
  } else if (!server.last_server_sync || server.last_server_sync < mine.time_received) {
    rows.push(["sent to the server", `not yet: connected to ${server.url}, last sync ` +
      `${server.last_server_sync ? formatTime(server.last_server_sync) : "never"}`]);
    advice.push("The position is saved but the server hasn't synced since. Refresh in a few seconds; if it stays " +
                "this way, the terminal running main.py shows why the sync fails.");
  } else {
    rows.push(["sent to the server", `yes: synced with ${server.url} at ${formatTime(server.last_server_sync)}`]);
  }
  return { rows, advice };
}

async function sendLocation(pos) {
  gpsDebug.last_sent = { time: Date.now() / 1000, fix_time: pos.timestamp / 1000,
                         latitude: pos.coords.latitude, longitude: pos.coords.longitude };
  const res = await fetch("/api/location", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ latitude: pos.coords.latitude, longitude: pos.coords.longitude }),
  });
  // 409 means the device has no identity yet; try again next minute
  gpsDebug.last_save = res.ok ? "saved"
    : res.status === 409 ? "not saved: device not set up yet (Debug tab)" : `failed: HTTP ${res.status}`;
  if (!res.ok && res.status !== 409) console.warn("location update failed:", res.status);
}

function getPosition(options) {
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, options));
}

function describeError(err) {
  return `${GEO_ERRORS[err.code] || "error"}: ${err.message || "no details"}`;
}

let latestPosition = null;  // the watch's newest fix, not yet sent if newer than gpsDebug.last_sent

function onFix(pos) {
  latestPosition = pos;
  gpsDebug.fixes += 1;
  gpsDebug.last_fix = { time: Date.now() / 1000, latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude, accuracy_m: Math.round(pos.coords.accuracy) };
  if (gpsDebug.fixes === 1) reportLocation();  // don't wait up to a minute for the first one
}

function onError(err) {
  gpsDebug.last_error = { time: Date.now() / 1000, code: err.code, error: GEO_ERRORS[err.code] || "error",
                          message: err.message };
  console.warn("geolocation error:", err.message);
  if (gpsDebug.fixes === 0 && gpsDebug.check === null) runGpsCheck(err);  // nothing has worked yet
}

// Send the newest fix, if there's one since the last send
function reportLocation() {
  const pos = latestPosition;
  if (!pos || (gpsDebug.last_sent && pos.timestamp / 1000 <= gpsDebug.last_sent.fix_time)) return;
  sendLocation(pos).catch((err) => {
    gpsDebug.last_save = `failed: ${err.message}`;
    console.warn("location update failed:", err);
  });
}

// Why doesn't the position work? Each finding is [what was checked, result];
// advice lists what to do, most likely cause first.
async function runGpsCheck(lastError) {
  const findings = [];
  const advice = [];
  gpsDebug.check = { time: Date.now() / 1000, running: true, findings, advice };

  findings.push(["page address", location.origin]);
  findings.push(["secure context (localhost or HTTPS)", window.isSecureContext ? "yes" : "NO"]);
  if (!window.isSecureContext) {
    advice.push(`Browsers only give the position to localhost or HTTPS pages. Open http://127.0.0.1:${location.port || 80} ` +
                "on this device, or start main.py with --https.");
  }
  const available = "geolocation" in navigator;
  findings.push(["browser has geolocation", available ? "yes" : "NO"]);
  if (lastError) findings.push(["error checked", describeError(lastError)]);

  let permission = "unknown";
  try {
    permission = (await navigator.permissions.query({ name: "geolocation" })).state;
  } catch (e) { /* older browsers have no Permissions API */ }
  findings.push(["location permission", permission]);
  findings.push(["page visible", document.visibilityState]);

  let device = "unknown";
  let path = null;
  try {
    const res = await fetch("/api/device");
    device = res.ok ? "set up" : res.status === 409 ? "NOT set up" : `HTTP ${res.status}`;
    const [server, geolocations] = await Promise.all(
      ["/api/server", "/api/geolocations"].map((url) => fetch(url).then((r) => r.json())));
    path = positionPath(res.ok ? await res.json() : null, server, geolocations);
  } catch (e) {
    device = `app not reachable (${e.message})`;
  }
  findings.push(["device", device]);

  let anyPosition = null;
  if (available && window.isSecureContext) {
    try {
      const pos = await getPosition(CHECK_OPTIONS);
      anyPosition = `yes: ${formatPosition(pos.coords.latitude, pos.coords.longitude)} ` +
                    `±${Math.round(pos.coords.accuracy)} m, ${Math.round(Date.now() / 1000 - pos.timestamp / 1000)} s old`;
    } catch (err) {
      anyPosition = `no (${describeError(err)})`;
    }
    findings.push(["any position at all (low accuracy, cached ok)", anyPosition]);
  }

  if (window.isSecureContext && available) {
    const denied = permission === "denied" || (lastError && lastError.code === 1);
    if (denied) {
      advice.push("Location permission is blocked. Allow it in the browser's site settings for this page, " +
                  "and in Android Settings > Apps > (browser) > Permissions > Location.");
    } else if (lastError && lastError.code === 3) {
      advice.push(anyPosition && anyPosition.startsWith("yes")
        ? "Location works but no GPS fix came within 30 s. Go outside with a clear view of the sky; " +
          "the GPS stays on and the fix shows up here when it comes."
        : "No fix within 30 s. Turn on Location in the phone's quick settings, go outside, " +
          "and wait a few minutes for the first fix; the GPS stays on meanwhile.");
      advice.push("On GrapheneOS, a first fix without assist data can take 10+ minutes: in Settings > Location > " +
                  "Location services, set SUPL and PSDS to 'GrapheneOS proxy' (not disabled), and turn on " +
                  "network location for a quick rough position.");
    } else if (lastError && lastError.code === 2) {
      advice.push("The phone couldn't get a position. Turn on Location in the phone's quick settings " +
                  "(high accuracy, not battery saving). A laptop without GPS needs internet to locate itself.");
    }
  } else if (!available) {
    advice.push("This browser has no geolocation. Use Chrome or Firefox on the phone itself.");
  }
  if (document.visibilityState !== "visible") {
    advice.push("Keep this page open on screen: the browser pauses location while the tab is hidden or the phone is locked.");
  }
  if (device === "NOT set up") {
    advice.push("Positions are only saved once the device is set up: create it in the Debug tab.");
  }
  if (path) {
    findings.push(...path.rows);
    advice.push(...path.advice);
  }
  if (advice.length === 0) advice.push("Nothing obviously wrong; the GPS stays on and keeps trying.");

  gpsDebug.check.running = false;
  if (typeof refreshGpsDebug === "function") refreshGpsDebug();
}

function startLocationUpdates() {
  if (!("geolocation" in navigator)) {
    console.warn("geolocation is not available: open the app over HTTPS " +
                 "(python main.py --host 0.0.0.0 --https) or on localhost");
    runGpsCheck(null);
    return;
  }
  gpsDebug.watching_since = Date.now() / 1000;
  navigator.geolocation.watchPosition(onFix, onError, WATCH_OPTIONS);
  setTimeout(() => {
    if (gpsDebug.fixes > 0 || gpsDebug.check !== null) return;
    onError({ code: 3, message: `no fix within ${FIRST_FIX_WAIT_MS / 1000} s (still trying)` });
  }, FIRST_FIX_WAIT_MS);
  setInterval(reportLocation, LOCATION_INTERVAL_MS);
}
