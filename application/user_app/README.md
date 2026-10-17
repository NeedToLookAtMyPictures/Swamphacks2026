# User App

Offline demo app. `main.py` runs everything: a small Flask server serves the web
interface and the locally stored map tiles. Leaflet is in `static/vendor/`, so
nothing is loaded from the internet at runtime.

## Setup (needs internet once)

```bash
cd application/user_app
pip install -r requirements.txt
python main.py download-map          # Gainesville, FL, zoom 10-16 (~1350 tiles)
```

Other areas:

```bash
python main.py download-map --place "Ocala, Florida"
python main.py download-map --bbox -82.42 29.59 -82.25 29.73 --max-zoom 17
```

Tiles are saved to `data/tiles/` (git-ignored). Running the command again skips
tiles you already have. Keep the area and zoom small, because OpenStreetMap's
tile servers don't allow bulk downloads.

## Run (works offline)

```bash
python main.py run                   # http://127.0.0.1:5000
python main.py run --host 0.0.0.0    # reachable from other devices on the LAN
```

## Tabs

- **Messages**: blank for now
- **Map**: offline Leaflet map
