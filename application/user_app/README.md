# User App

Offline demo app. `main.py` runs everything: a small Flask server serves the web
interface and the downloaded map. Leaflet and protomaps-leaflet are in
`static/vendor/`, so nothing is loaded from the internet at runtime.

The map is a [Protomaps](https://protomaps.com) vector basemap built from
OpenStreetMap data. (OpenStreetMap's own tile servers don't allow downloading
tiles for offline use, so we don't use them.)

## Setup (needs internet once)

```bash
cd application/user_app
pip install -r requirements.txt
python main.py download-map          # Gainesville, FL, zoom 8-15 (a few MB)
```

`download-map` finds the latest daily Protomaps world build, uses HTTP range
requests to pull only the tiles inside the area, and saves them as one file,
`data/map.pmtiles` (git-ignored). Running it again replaces the old map.

Other areas and sources:

```bash
python main.py download-map --place "Ocala, Florida"
python main.py download-map --bbox -82.42 29.59 -82.25 29.73
python main.py download-map --source https://build.protomaps.com/20261015.pmtiles
python main.py download-map --source some-local-file.pmtiles
```

## Run (works offline)

```bash
python main.py run                   # http://127.0.0.1:5000
python main.py run --host 0.0.0.0    # reachable from other devices on the LAN
```

## Tabs

- **Messages**: blank for now
- **Map**: offline Leaflet map
