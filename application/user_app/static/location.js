// Report this device's position to the local server once a minute.
//
// Works offline: the browser's Geolocation API reads the device's GPS, which
// needs no internet. (Devices without GPS, like most laptops, locate themselves
// from Wi-Fi networks, which does need internet, so offline they may time out.)
// Geolocation only runs on localhost or HTTPS.

const LOCATION_INTERVAL_MS = 60 * 1000;
const LOCATION_OPTIONS = {
  enableHighAccuracy: true,  // prefer GPS over network-based positioning
  timeout: 30 * 1000,        // a GPS fix can take a while, especially offline
  maximumAge: 30 * 1000,     // a fix from the last 30 s is good enough
};

async function sendLocation(pos) {
  const res = await fetch("/api/location", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ latitude: pos.coords.latitude, longitude: pos.coords.longitude }),
  });
  // 409 means the device has no identity yet; try again next minute
  if (!res.ok && res.status !== 409) console.warn("location update failed:", res.status);
}

function reportLocation() {
  navigator.geolocation.getCurrentPosition(
    (pos) => sendLocation(pos).catch((err) => console.warn("location update failed:", err)),
    (err) => console.warn("geolocation error:", err.message),
    LOCATION_OPTIONS,
  );
}

function startLocationUpdates() {
  if (!("geolocation" in navigator)) {
    console.warn("geolocation is not available (needs localhost or HTTPS)");
    return;
  }
  reportLocation();
  setInterval(reportLocation, LOCATION_INTERVAL_MS);
}
