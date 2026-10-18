// Report this device's position to the local server once a minute.
//
// Works offline: the browser's Geolocation API reads the device's GPS, which
// needs no internet. (Devices without GPS, like most laptops, locate themselves
// from Wi-Fi networks, which does need internet, so offline they may time out.)
// Geolocation only runs on localhost or HTTPS, so when a phone opens the app
// on another device (e.g. a Raspberry Pi over Bluetooth), start it with --https.
//
// If the first try fails, runGpsCheck() works out why. Its findings show in the
// Debug tab (gpsDebug), in a banner, and in the terminal running main.py.

const LOCATION_INTERVAL_MS = 60 * 1000;
const LOCATION_OPTIONS = {
  enableHighAccuracy: true,  // prefer GPS over network-based positioning
  timeout: 30 * 1000,        // a GPS fix can take a while, especially offline
  maximumAge: 30 * 1000,     // a fix from the last 30 s is good enough
};
// The check's second try: any position at all, even an old or network-based one
const CHECK_OPTIONS = { enableHighAccuracy: false, timeout: 15 * 1000, maximumAge: Infinity };

const GEO_ERRORS = { 1: "PERMISSION_DENIED", 2: "POSITION_UNAVAILABLE", 3: "TIMEOUT" };

// Read by the Debug tab
const gpsDebug = {
  attempts: 0,
  fixes: 0,
  last_fix: null,    // {time, latitude, longitude, accuracy_m}
  last_error: null,  // {time, error, message}
  last_save: null,   // what PUT /api/location answered
  check: null,       // runGpsCheck() result: {time, running, findings: [[name, value]], advice: [...]}
};

async function sendLocation(pos) {
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

async function reportLocation() {
  gpsDebug.attempts += 1;
  let pos;
  try {
    pos = await getPosition(LOCATION_OPTIONS);
  } catch (err) {
    gpsDebug.last_error = { time: Date.now() / 1000, error: GEO_ERRORS[err.code] || "error", message: err.message };
    console.warn("geolocation error:", err.message);
    if (gpsDebug.fixes === 0 && gpsDebug.check === null) runGpsCheck(err);  // first try failed
    return;
  }
  gpsDebug.fixes += 1;
  gpsDebug.last_fix = { time: Date.now() / 1000, latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude, accuracy_m: Math.round(pos.coords.accuracy) };
  hideGpsBanner();
  sendLocation(pos).catch((err) => {
    gpsDebug.last_save = `failed: ${err.message}`;
    console.warn("location update failed:", err);
  });
}

// Why doesn't the position work? Each finding is [what was checked, result];
// advice lists what to do, most likely cause first.
async function runGpsCheck(firstError) {
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
  if (firstError) findings.push(["first try", describeError(firstError)]);

  let permission = "unknown";
  try {
    permission = (await navigator.permissions.query({ name: "geolocation" })).state;
  } catch (e) { /* older browsers have no Permissions API */ }
  findings.push(["location permission", permission]);
  findings.push(["page visible", document.visibilityState]);

  let device = "unknown";
  try {
    const res = await fetch("/api/device");
    device = res.ok ? "set up" : res.status === 409 ? "NOT set up" : `HTTP ${res.status}`;
  } catch (e) {
    device = `app not reachable (${e.message})`;
  }
  findings.push(["device", device]);

  let anyPosition = null;
  if (available && window.isSecureContext) {
    try {
      const pos = await getPosition(CHECK_OPTIONS);
      anyPosition = `yes, ±${Math.round(pos.coords.accuracy)} m, ` +
                    `${Math.round(Date.now() / 1000 - pos.timestamp / 1000)} s old`;
    } catch (err) {
      anyPosition = `no (${describeError(err)})`;
    }
    findings.push(["any position at all (low accuracy, cached ok)", anyPosition]);
  }

  if (window.isSecureContext && available) {
    const denied = permission === "denied" || (firstError && firstError.code === 1);
    if (denied) {
      advice.push("Location permission is blocked. Allow it in the browser's site settings for this page, " +
                  "and in Android Settings > Apps > (browser) > Permissions > Location.");
    } else if (firstError && firstError.code === 3) {
      advice.push(anyPosition && anyPosition.startsWith("yes")
        ? "Location works but no GPS fix came within 30 s. Go outside with a clear view of the sky; " +
          "it keeps trying every minute."
        : "No fix within 30 s. Turn on Location (high accuracy) in the phone's quick settings, go outside, " +
          "and wait a few minutes for the first fix.");
    } else if (firstError && firstError.code === 2) {
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
  if (advice.length === 0) advice.push("Nothing obviously wrong; it tries again every minute.");

  gpsDebug.check.running = false;
  showGpsBanner(advice[0]);
  // The terminal running main.py: on a phone the browser console isn't visible
  fetch("/api/location/debug", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ lines: [...findings.map(([k, v]) => `${k}: ${v}`), ...advice.map((a) => `-> ${a}`)] }),
  }).catch(() => {});
  if (typeof refreshGpsDebug === "function") refreshGpsDebug();
}

function showGpsBanner(text) {
  let banner = document.getElementById("gps-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "gps-banner";
    const message = document.createElement("span");
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", hideGpsBanner);
    banner.append(message, close);
    document.body.prepend(banner);
  }
  banner.firstChild.textContent = `GPS didn't work: ${text} Details in the Debug tab.`;
  banner.hidden = false;
}

function hideGpsBanner() {
  const banner = document.getElementById("gps-banner");
  if (banner) banner.hidden = true;
}

function startLocationUpdates() {
  if (!("geolocation" in navigator)) {
    console.warn("geolocation is not available: open the app over HTTPS " +
                 "(python main.py --host 0.0.0.0 --https) or on localhost");
    runGpsCheck(null);
    return;
  }
  reportLocation();
  setInterval(reportLocation, LOCATION_INTERVAL_MS);
}
