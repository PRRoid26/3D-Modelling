# 🌊 Bathymetric Map Explorer

Interactive ocean floor map with ArcGIS basemaps, overlay layers from NOAA/USGS,
borehole data plotting from Excel/CSV, and a geocoding search bar.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the Flask server
python app.py

# 3. Open in browser
http://localhost:5050
```

---

## Features

| UI Element | Location | Description |
|---|---|---|
| 🔍 Search bar | Top-left | Search by place name or `lat, lon` coordinates |
| ⚡ Overlay Layers | Bottom-left | Toggle NOAA/USGS layers (bathymetry, contours, plates, faults…) |
| 🗺 Base Maps | Bottom-right panel | Switch between 8 ArcGIS basemaps (Oceans, Satellite, Topo…) |
| 📂 Borehole Data | Bottom-right panel | Upload `.xlsx` / `.csv` — dots appear on map, click for popup |
| ➕➖ Zoom | Bottom-right | Zoom in / out buttons |
| 📍 Coordinates | Bottom-center | Live lat/lon badge under cursor |

---

## Excel / CSV Format

Your file needs **at minimum** these columns (any name from the list):

| Latitude column | Longitude column |
|---|---|
| `latitude`, `lat`, `y` | `longitude`, `lon`, `long`, `x` |

All additional columns (depth, name, description, etc.) are shown in the popup when you click a borehole dot.

**Example:**
```
name,latitude,longitude,depth_m,formation
BH-001,12.345,76.543,450,Granite
BH-002,12.112,76.891,320,Basalt
```

---

## Overlay Layers (NOAA / USGS)

| Layer | Source | Type |
|---|---|---|
| Bathymetry Hillshade | NOAA NCEI | ImageServer |
| ETOPO1 Bedrock | NOAA NCEI | ImageServer |
| Magnetic Anomaly (EMAG2) | NOAA NCEI | ImageServer |
| GEBCO Depth Contours | NOAA NCEI | MapServer |
| Multibeam Sonar Coverage | NOAA NCEI | MapServer |
| Tectonic Plate Boundaries | USGS | MapServer |
| Fault Lines | USGS | MapServer |
| Coastlines | NOAA NCEI | MapServer |

---

## Project Structure

```
bathy_map/
├── app.py               ← Flask backend (API routes)
├── requirements.txt
├── README.md
└── templates/
    └── index.html       ← ArcGIS JS 4.x frontend
```
