"""
Bathymetry Map Application — Python/Flask Backend
► Run:   python app.py
► Open:  http://localhost:5050
"""

from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import re, io, os, math, tempfile, sys

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024   # 32 MB

NOAA = 'https://gis.ngdc.noaa.gov/arcgis/rest/services'
USGS = 'https://earthquake.usgs.gov/arcgis/rest/services'

LAYER_CATALOG = {
    "bathy_hillshade":  f"{NOAA}/DEM_mosaics/DEM_global_mosaic_hillshade/ImageServer",
    "bathy_dem":        f"{NOAA}/DEM_mosaics/DEM_global_mosaic/ImageServer",
    "etopo1":           f"{NOAA}/DEM_mosaics/ETOPO1_bedrock/ImageServer",
    "etopo1_ice":       f"{NOAA}/DEM_mosaics/ETOPO1_ice_surface/ImageServer",
    "magnetic_anomaly": f"{NOAA}/EMAG2v3/ImageServer",
    "gebco_contours":   f"{NOAA}/wgs84/gebco_2019_contours/MapServer",
    "multibeam":        f"{NOAA}/wgs84/multibeam_footprints/MapServer",
    "coastlines":       f"{NOAA}/reference/coastlines/MapServer",
    "plate_boundaries": f"{USGS}/eq/map_plateboundaries/MapServer",
    "faults":           f"{USGS}/eq/map_faults/MapServer",
}

# ── Log pipeline imports ──────────────────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
PIPELINE_AVAILABLE = False
PIPELINE_ERROR     = ""

try:
    from ruleScript import process_file as colour_file          # noqa: E402
    from borehole_3d_viz import (                               # noqa: E402
        load_boreholes, BUILTIN_INDEX,
        build_normaliser, build_traces, build_layout, build_html,
        depth_gridlines,
    )
    PIPELINE_AVAILABLE = True
except ImportError as _ie:
    PIPELINE_ERROR = str(_ie)

# ── In-memory 3D cache ────────────────────────────────────────────────────────
_3d_html_cache: dict = {}        # { bh_id (int) → html_string }
_processed_xlsx_path: str = ""   # path kept alive for /api/download_processed


# ══════════════════════════════════════════════════════════════════════════════
# UTM → WGS-84 (pure math, no external lib)
# ══════════════════════════════════════════════════════════════════════════════
def utm_to_latlon(easting, northing, zone_number, northern=True):
    k0 = 0.9996; a = 6378137.0; e = 0.00669438; e2 = e / (1 - e)
    x  = easting - 500000.0
    y  = northing if northern else northing - 10_000_000.0
    lon_origin = (zone_number - 1) * 6 - 180 + 3
    M  = y / k0
    mu = M / (a * (1 - e/4 - 3*e**2/64 - 5*e**3/256))
    e1 = (1 - math.sqrt(1 - e)) / (1 + math.sqrt(1 - e))
    phi1 = (mu
            + (3*e1/2    - 27*e1**3/32)  * math.sin(2*mu)
            + (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
            + (151*e1**3/96)               * math.sin(6*mu))
    N1 = a / math.sqrt(1 - e * math.sin(phi1)**2)
    T1 = math.tan(phi1)**2
    C1 = e2 * math.cos(phi1)**2
    R1 = a * (1 - e) / (1 - e * math.sin(phi1)**2)**1.5
    D  = x / (N1 * k0)
    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
          D**2/2
          - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*e2) * D**4/24
          + (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*e2 - 3*C1**2) * D**6/720)
    lon = (D - (1 + 2*T1 + C1)*D**3/6
             + (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*e2 + 24*T1**2)*D**5/120) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon) + lon_origin


# ══════════════════════════════════════════════════════════════════════════════
# Coordinate parser  (UTM "m E, m N"  or  decimal-degree)
# ══════════════════════════════════════════════════════════════════════════════
def parse_coordinates(value, utm_zone=43):
    if not value or (isinstance(value, float) and math.isnan(value)):
        return None
    val = str(value).strip()

    m = re.compile(r'([\d]+\.?\d*)\s*m?\s*E,?\s*([\d]+\.?\d*)\s*m?\s*N', re.I).search(val)
    if m:
        try:
            lat, lon = utm_to_latlon(float(m.group(1)), float(m.group(2)), utm_zone)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except Exception:
            pass

    m = re.compile(
        r'([+-]?\d+\.?\d*)\s*°?\s*([NSns]?)\s*[,\s/]+\s*([+-]?\d+\.?\d*)\s*°?\s*([EWew]?)'
    ).search(val)
    if m:
        a, hns = float(m.group(1)), m.group(2).upper()
        b, hew = float(m.group(3)), m.group(4).upper()
        if hns == 'S': a = -a
        if hew == 'W': b = -b
        if abs(a) <= 90 and abs(b) <= 180: return a, b
        if abs(b) <= 90 and abs(a) <= 180: return b, a
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Regular borehole CSV/XLSX processor  (existing logic — unchanged)
# ══════════════════════════════════════════════════════════════════════════════
def process_borehole_df(df, utm_zone=43):
    df.columns = [str(c).strip() for c in df.columns]

    def find_col(keywords):
        for kw in keywords:
            for c in df.columns:
                if kw in c.lower():
                    return c
        return None

    coord_col  = find_col(['co-ord', 'coord'])
    lat_col    = find_col(['latitude', 'lat']) if not coord_col else None
    lon_col    = find_col(['longitude', 'lon', 'long']) if not coord_col else None
    strat_col  = find_col(['strat', 'lith', 'desc', 'soil', 'rock', 'formation', 'material'])
    depth_col  = find_col(['depth'])
    wdepth_col = find_col(['water depth', 'waterdepth', 'water_depth'])
    srno_col   = find_col(['sr. no', 'sr.no', 'sr no', 'bh no', 'borehole no',
                           'site no', 'well no', 'id', 'no.'])

    if depth_col and wdepth_col and depth_col == wdepth_col:
        depth_col = None
    if depth_col and wdepth_col and 'water' in depth_col.lower():
        for c in df.columns:
            if 'depth' in c.lower() and 'water' not in c.lower():
                depth_col = c
                break

    if not coord_col and not (lat_col and lon_col):
        return None, (
            f'No coordinate column found. Detected columns: {list(df.columns)}. '
            'Expected "co-ord", "coord", or separate "lat"/"lon" columns.'
        )

    records    = []
    current_bh = None
    bh_index   = 0

    for _, row in df.iterrows():
        is_new = False
        if srno_col:
            v      = row[srno_col]
            is_new = pd.notna(v) and str(v).strip() not in ('', 'nan')
        elif coord_col:
            v      = row[coord_col]
            is_new = pd.notna(v) and str(v).strip() not in ('', 'nan')
        elif lat_col:
            is_new = pd.notna(row[lat_col])

        if is_new:
            bh_index += 1
            lat = lon = None
            if coord_col and pd.notna(row[coord_col]):
                parsed = parse_coordinates(str(row[coord_col]), utm_zone=utm_zone)
                if parsed: lat, lon = parsed
            elif lat_col and lon_col:
                try:
                    lat = float(row[lat_col])
                    lon = float(row[lon_col])
                except Exception:
                    pass

            if lat is None or lon is None:
                current_bh = None
                continue

            bh_no = (str(int(float(row[srno_col])))
                     if srno_col and pd.notna(row[srno_col]) else str(bh_index))
            current_bh = {
                'id':             bh_index - 1,
                'borehole_no':    bh_no,
                'lat':            round(lat, 6),
                'lon':            round(lon, 6),
                'coordinates_raw': (str(row[coord_col]).strip()
                                    if coord_col and pd.notna(row[coord_col])
                                    else f'{lat:.5f}, {lon:.5f}'),
                'water_depth':    (str(row[wdepth_col]).strip()
                                   if wdepth_col and pd.notna(row[wdepth_col]) else '—'),
                'stratigraphy':   [],
                'has_3d':         False,
            }
            structural = {srno_col, coord_col, lat_col, lon_col,
                          wdepth_col, depth_col, strat_col, None}
            for c in df.columns:
                if c not in structural and pd.notna(row[c]):
                    current_bh[c.lower().replace(' ', '_')] = str(row[c]).strip()
            records.append(current_bh)

        if current_bh is not None:
            dv = str(row[depth_col]).strip()  if depth_col  and pd.notna(row[depth_col])  else ''
            sv = str(row[strat_col]).strip()  if strat_col  and pd.notna(row[strat_col])  else ''
            if dv or sv:
                current_bh['stratigraphy'].append({'depth': dv, 'description': sv})

    if not records:
        return None, 'No valid borehole records could be parsed from this file.'
    return records, None


# ══════════════════════════════════════════════════════════════════════════════
# Helper: compact 3D HTML for a single borehole
# ══════════════════════════════════════════════════════════════════════════════
def _generate_bh_html(bh_id: int, bh_data: dict,
                      global_nx, global_ny, hw: float = 9.5) -> str:
    """
    Self-contained Plotly HTML for ONE borehole.
    - Cylindrical tube shape (monkey-patches box_mesh + pattern_traces_for_layer)
    - Pure-black background
    - Camera tuned to show the ENTIRE column top-to-bottom
    - Soil Index legend filtered to only types present in this borehole
    """
    import json, math
    import borehole_3d_viz as _bv

    # ════════════════════════════════════════════════════════════════
    # CYLINDER REPLACEMENTS for box_mesh + pattern_traces_for_layer
    # ════════════════════════════════════════════════════════════════
    N_SEG = 24   # circumference segments — more = smoother cylinder

    def cylinder_mesh(cx, cy, z_top, z_bot, hw):
        """Drop-in replacement for box_mesh: returns a smooth cylinder."""
        r      = hw
        angles = [2 * math.pi * k / N_SEG for k in range(N_SEG)]

        # Vertices: top ring [0..N-1], bottom ring [N..2N-1],
        #           top-cap centre [2N], bottom-cap centre [2N+1]
        xs = [cx + r * math.cos(a) for a in angles] \
           + [cx + r * math.cos(a) for a in angles] \
           + [cx, cx]
        ys = [cy + r * math.sin(a) for a in angles] \
           + [cy + r * math.sin(a) for a in angles] \
           + [cy, cy]
        zs = [-z_top] * N_SEG + [-z_bot] * N_SEG + [-z_top, -z_bot]

        ii, jj, kk = [], [], []
        for seg in range(N_SEG):
            nxt = (seg + 1) % N_SEG
            # Side wall — two triangles per segment
            ii += [seg,       nxt + N_SEG]
            jj += [nxt,       seg  + N_SEG]
            kk += [seg + N_SEG, nxt]
        # Caps
        top_c, bot_c = 2 * N_SEG, 2 * N_SEG + 1
        for seg in range(N_SEG):
            nxt = (seg + 1) % N_SEG
            ii += [top_c,       bot_c]
            jj += [nxt,         seg + N_SEG]
            kk += [seg,         nxt + N_SEG]

        return xs, ys, zs, ii, jj, kk

    def cylinder_pattern_traces_for_layer(cx, cy, z_top, z_bot, hw, pat_type):
        """Drop-in replacement for pattern_traces_for_layer: wraps patterns
        around the cylinder surface parameterized by angle θ ∈ [0, 2π]."""
        depth_range = z_bot - z_top
        if depth_range <= 0:
            return []

        r      = hw
        z_step = max(2.0, depth_range / max(int(depth_range / 2.5), 3))
        z_vals = [z_top + z_step / 2 + k * z_step
                  for k in range(int(depth_range / z_step) + 1)
                  if z_top + z_step / 2 + k * z_step < z_bot]
        ang_pts = [2 * math.pi * k / N_SEG for k in range(N_SEG)]

        def surf(ang, z):
            return cx + r * math.cos(ang), cy + r * math.sin(ang), -z

        px, py, pz = [], [], []

        if pat_type == "lightGray":       # Clay — horizontal dashes
            for z in z_vals:
                for i in range(0, N_SEG, 2):   # every other segment
                    a_mid = ang_pts[i]
                    x1, y1, z1 = surf(a_mid - 0.25, z)
                    x2, y2, z2 = surf(a_mid + 0.25, z)
                    px += [x1, x2, None]; py += [y1, y2, None]; pz += [z1, z2, None]

        elif pat_type == "darkGray":      # Sand — dense dot grid
            for z in z_vals:
                for a in ang_pts:
                    xp, yp, zp = surf(a, z)
                    px.append(xp); py.append(yp); pz.append(zp)

        elif pat_type == "gray0625":      # Silt — sparse dots
            for z in z_vals[::2]:
                for a in ang_pts[::2]:
                    xp, yp, zp = surf(a, z)
                    px.append(xp); py.append(yp); pz.append(zp)

        elif pat_type == "lightUp":       # Calcarenite — diagonal /
            n_diag = 10
            for i in range(n_diag):
                ang_off = (i / n_diag) * 2 * math.pi
                for k in range(16):
                    t   = k / 15
                    ang = (ang_off + t * 2 * math.pi) % (2 * math.pi)
                    z_v = z_top + t * depth_range
                    xp, yp, zp = surf(ang, z_v)
                    px.append(xp); py.append(yp); pz.append(zp)
                px.append(None); py.append(None); pz.append(None)

        elif pat_type == "lightTrellis":  # Sandstone — cross-hatch #
            n_diag = 8
            for i in range(n_diag):
                ang_off = (i / n_diag) * 2 * math.pi
                for direction in (+1, -1):
                    for k in range(16):
                        t   = k / 15
                        ang = (ang_off + direction * t * 2 * math.pi) % (2 * math.pi)
                        z_v = z_top + t * depth_range
                        xp, yp, zp = surf(ang, z_v)
                        px.append(xp); py.append(yp); pz.append(zp)
                    px.append(None); py.append(None); pz.append(None)

        if not px:
            return []

        is_line = None in px
        trace = {
            "type": "scatter3d", "x": px, "y": py, "z": pz,
            "mode": "lines" if is_line else "markers",
            "showlegend": False, "hoverinfo": "skip",
        }
        if is_line:
            trace["line"]   = {"color": "rgba(0,0,0,0.80)", "width": 1.5}
        else:
            trace["marker"] = {"color": "rgba(0,0,0,0.85)", "size": 2.5,
                               "symbol": "circle"}
        return [trace]

    # ── Monkey-patch borehole_3d_viz with cylinder functions ──────────────
    _orig_box_mesh     = _bv.box_mesh
    _orig_pat_traces   = _bv.pattern_traces_for_layer
    _bv.box_mesh                  = cylinder_mesh
    _bv.pattern_traces_for_layer  = cylinder_pattern_traces_for_layer

    try:
        single_bh = {bh_id: bh_data}
        traces    = build_traces(single_bh, BUILTIN_INDEX, global_nx, global_ny, hw)
        traces   += depth_gridlines(single_bh, global_nx, global_ny, hw)
    finally:
        # Always restore originals even if build_traces raises
        _bv.box_mesh                 = _orig_box_mesh
        _bv.pattern_traces_for_layer = _orig_pat_traces

    # ── 1. Filter ALL Plotly legend swatches — we replace with custom HTML ───────
    traces = [t for t in traces if not t.get('showlegend', False)]

    # Texture → (bg_colour, label, SVG pattern definition, pattern fill ref)
    # Each SVG pattern is 28×28px and matches the visual appearance of each soil type
    TEXTURE_DEFS = {
        "lightGray": {
            "label": "Clay",
            # Cylinder shows: evenly-spaced horizontal parallel lines
            "svg_pattern": '''<pattern id="pi-lightGray" x="0" y="0" width="36" height="8" patternUnits="userSpaceOnUse">
              <rect width="36" height="8" fill="white"/>
              <line x1="0" y1="4" x2="36" y2="4" stroke="#222" stroke-width="1.4"/>
            </pattern>''',
        },
        "darkGray": {
            "label": "Sand",
            # Cylinder shows: large scattered black dots, fairly sparse
            "svg_pattern": '''<pattern id="pi-darkGray" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="white"/>
              <circle cx="2.5" cy="2.5" r="1.6" fill="#111"/>
              <circle cx="7.5" cy="7.5" r="1.6" fill="#111"/>
              <circle cx="7.5" cy="2.5" r="1.6" fill="#111"/>
              <circle cx="2.5" cy="7.5" r="1.6" fill="#111"/>
            </pattern>''',
        },
        "gray0625": {
            "label": "Silt",
            # Cylinder shows: sparse large dots, fewer than sand
            "svg_pattern": '''<pattern id="pi-gray0625" x="0" y="0" width="14" height="14" patternUnits="userSpaceOnUse">
              <rect width="14" height="14" fill="white"/>
              <circle cx="3.5" cy="3.5" r="1.5" fill="#111"/>
              <circle cx="10.5" cy="10.5" r="1.5" fill="#111"/>
            </pattern>''',
        },
        "lightUp": {
            "label": "Calcarenite",
            # Cylinder shows: diagonal lines going up-right /
            "svg_pattern": '''<pattern id="pi-lightUp" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="white"/>
              <line x1="0" y1="10" x2="10" y2="0" stroke="#222" stroke-width="1.4"/>
              <line x1="-5" y1="10" x2="5"  y2="0" stroke="#222" stroke-width="1.4"/>
              <line x1="5"  y1="10" x2="15" y2="0" stroke="#222" stroke-width="1.4"/>
            </pattern>''',
        },
        "lightTrellis": {
            "label": "Sandstone",
            # Cylinder shows: cross-hatch, both diagonals clearly visible
            "svg_pattern": '''<pattern id="pi-lightTrellis" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="white"/>
              <line x1="0"  y1="10" x2="10" y2="0"  stroke="#222" stroke-width="1.4"/>
              <line x1="-5" y1="10" x2="5"  y2="0"  stroke="#222" stroke-width="1.4"/>
              <line x1="5"  y1="10" x2="15" y2="0"  stroke="#222" stroke-width="1.4"/>
              <line x1="0"  y1="0"  x2="10" y2="10" stroke="#222" stroke-width="1.4"/>
              <line x1="-5" y1="0"  x2="5"  y2="10" stroke="#222" stroke-width="1.4"/>
              <line x1="5"  y1="0"  x2="15" y2="10" stroke="#222" stroke-width="1.4"/>
            </pattern>''',
        },
        "darkTrellis": {
            "label": "Gravel",
            # Grid: vertical + horizontal lines forming squares
            "svg_pattern": '''<pattern id="pi-darkTrellis" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="white"/>
              <line x1="0" y1="5" x2="10" y2="5" stroke="#222" stroke-width="1.4"/>
              <line x1="5" y1="0" x2="5" y2="10" stroke="#222" stroke-width="1.4"/>
            </pattern>''',
        },
        "lightGrid": {
            "label": "Limestone",
            # Fine grid
            "svg_pattern": '''<pattern id="pi-lightGrid" x="0" y="0" width="8" height="8" patternUnits="userSpaceOnUse">
              <rect width="8" height="8" fill="white"/>
              <line x1="0" y1="4" x2="8" y2="4" stroke="#333" stroke-width="0.9"/>
              <line x1="4" y1="0" x2="4" y2="8" stroke="#333" stroke-width="0.9"/>
            </pattern>''',
        },
        "lightDown": {
            "label": "Shale",
            # Diagonal lines going down-right \
            "svg_pattern": '''<pattern id="pi-lightDown" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="white"/>
              <line x1="0"  y1="0"  x2="10" y2="10" stroke="#222" stroke-width="1.4"/>
              <line x1="-5" y1="0"  x2="5"  y2="10" stroke="#222" stroke-width="1.4"/>
              <line x1="5"  y1="0"  x2="15" y2="10" stroke="#222" stroke-width="1.4"/>
            </pattern>''',
        },
        "solid": {
            "label": "Massive",
            "svg_pattern": '''<pattern id="pi-solid" x="0" y="0" width="4" height="4" patternUnits="userSpaceOnUse">
              <rect width="4" height="4" fill="#aaa"/>
            </pattern>''',
        },
    }

    used_textures = {l["texture"] for l in bh_data["layers"] if l.get("texture")}
    present_items = [(tex, TEXTURE_DEFS[tex]) for tex in TEXTURE_DEFS if tex in used_textures]

    legend_rows = ""
    for tex, d in present_items:
        legend_rows += f'''
        <div class="leg-row">
          <svg width="48" height="24" style="flex-shrink:0;border-radius:3px;border:1px solid rgba(0,0,0,0.4)">
            <defs>{d["svg_pattern"]}</defs>
            <rect width="48" height="24" fill="url(#pi-{tex})"/>
          </svg>
          <span>{d["label"]}</span>
        </div>'''

    legend_html = f'''
<div id="custom-legend">
  <div class="leg-title">Soil Index</div>
  {legend_rows}
</div>'''

    # ── 2. Tag ALL traces with depth customdata from their own z-geometry ────────
    # Reading depths from the trace z-values directly is the only reliable approach.
    # Order-based iteration breaks because water column, spine, and BH label traces
    # interleave with layer traces unpredictably.
    #
    # Geometry rule (both cylinder and box):
    #   - Mesh3d z-values are exactly [-depth_top]*N and [-depth_bot]*N
    #     → depth_top = -max(z),  depth_bot = -min(z)
    #   - Scatter3d pattern z-values span that same range (may include None)
    #     → same formula after filtering None values
    #
    # We only tag traces that clearly belong to a stratigraphic layer:
    #   mesh3d   → must have "Depth:" in hovertemplate (excludes water column)
    #   scatter3d→ must not be a legend swatch, spine, or label
    #              (we detect spine/label by hoverinfo=="skip" or showlegend==True)

    def _z_to_depths(z_list):
        """Extract (depth_top, depth_bot) from a z-coordinate list."""
        real_z = [v for v in z_list if v is not None]
        if not real_z:
            return None, None
        return round(-max(real_z), 4), round(-min(real_z), 4)

    # Build a lookup: (depth_top, depth_bot) → layer dict
    layer_by_depth = {
        (round(l["depth_top"], 4), round(l["depth_bot"], 4)): l
        for l in bh_data["layers"]
    }

    for t in traces:
        if t.get("showlegend") or t.get("hoverinfo") == "skip":
            continue   # legend swatch, spine, or gridline — skip

        z_list = t.get("z", [])
        dt, db = _z_to_depths(z_list)
        if dt is None:
            continue

        # Match to nearest layer (tolerance 0.1 m handles float rounding)
        matched_layer = None
        for (lt, lb), lyr in layer_by_depth.items():
            if abs(lt - dt) < 0.1 and abs(lb - db) < 0.1:
                matched_layer = lyr
                break

        if matched_layer is None:
            # Could be water column or BH label — suppress tooltip only
            t["hovertemplate"] = "<extra></extra>"
            continue

        cd = [matched_layer["depth_top"], matched_layer["depth_bot"]]

        if t["type"] == "mesh3d" and "Depth:" in t.get("hovertemplate", ""):
            n_verts = len(t.get("x", []))
            t["customdata"]    = [cd] * max(n_verts, 1)
            t["hovertemplate"] = "<extra></extra>"
        elif t["type"] == "scatter3d":
            n_pts = len([v for v in t.get("x", []) if v is not None])
            # For scatter3d the customdata must align with ALL points (including None)
            n_all = len(t.get("x", []))
            t["customdata"]    = [cd] * n_all
            t["hovertemplate"] = "<extra></extra>"

    # ── 3. Depth-axis ticks ───────────────────────────────────────────────────
    max_depth = max((l["depth_bot"] for l in bh_data["layers"]), default=100)
    step  = 25
    tvals = list(range(0, -(int(max_depth) + step), -step))
    ttxt  = [str(abs(v)) for v in tvals]

    # ── 4. Pure-black layout ──────────────────────────────────────────────────
    BLK       = "#000000"
    GRID_COL  = "rgba(55,75,105,0.40)"
    TICK_COL  = "rgba(155,165,180,0.75)"

    layout = {
        "autosize":      True,
        "paper_bgcolor": BLK,
        "margin":        {"l": 0, "r": 0, "t": 8, "b": 0},

        "title": {"text": ""},

        # Plotly legend disabled — replaced by custom SVG-pattern HTML legend
        "showlegend": False,

        "hoverlabel": {
            "bgcolor":     "rgba(10,15,30,0.96)",
            "bordercolor": "rgba(90,140,200,0.80)",
            "font":        {"color": "white", "size": 12, "family": "Arial"},
        },

        "modebar": {
            "bgcolor":     "rgba(0,0,0,0)",
            "color":       "#888",
            "activecolor": "white",
            "orientation": "h",
        },

        "scene": {
            "xaxis": {
                "title":           {"text": "", "font": {"size": 0}},
                "tickfont":        {"color": TICK_COL, "size": 7},
                "gridcolor":       GRID_COL,
                "zerolinecolor":   GRID_COL,
                "backgroundcolor": BLK,
                "showspikes":      False,
            },
            "yaxis": {
                "title":           {"text": "", "font": {"size": 0}},
                "tickfont":        {"color": TICK_COL, "size": 7},
                "gridcolor":       GRID_COL,
                "zerolinecolor":   GRID_COL,
                "backgroundcolor": BLK,
                "showspikes":      False,
            },
            "zaxis": {
                "title":           {"text": "Depth (m)",
                                    "font": {"color": TICK_COL, "size": 9}},
                "tickfont":        {"color": TICK_COL, "size": 8},
                "gridcolor":       GRID_COL,
                "zerolinecolor":   "rgba(91,155,213,0.55)",
                "backgroundcolor": BLK,
                "tickvals":        tvals,
                "ticktext":        ttxt,
                "showspikes":      False,
            },
            "bgcolor": BLK,

            # Equal x/y aspect = round cross-section, tall z for column
            "aspectmode":  "manual",
            "aspectratio": {"x": 0.45, "y": 0.45, "z": 2.10},

            "domain": {"x": [0.0, 1.0], "y": [0.0, 1.0]},

            # eye.x == eye.y  →  symmetric = round cylinder (not oval)
            # eye.z low        →  side-on view so column appears tall, not top-down disc
            "camera": {
                "eye":    {"x": 1.20, "y": 1.20, "z": 0.28},
                "up":     {"x": 0,    "y": 0,    "z": 1},
                "center": {"x": 0,    "y": 0,    "z": -0.20},
            },

            "annotations": [],
        },
    }

    traces_json = json.dumps(traces)
    layout_json = json.dumps(layout)
    config_json = json.dumps({
        "displayModeBar": True,
        "responsive":     True,
        "scrollZoom":     True,
        "modeBarButtonsToAdd":    ["resetCameraDefault3d"],
        "modeBarButtonsToRemove": ["toImage"],
        "displaylogo":    False,
    })

    cam_json = json.dumps({
        "eye":    {"x": 1.20, "y": 1.20, "z": 0.28},
        "up":     {"x": 0,    "y": 0,    "z": 1},
        "center": {"x": 0,    "y": 0,    "z": -0.20},
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box }}
    html, body {{ width:100%; height:100%; background:#000; overflow:hidden; }}
    #plot {{ position:absolute; inset:0; width:100%; height:100%; background:#000; }}

    /* ── Custom SVG-pattern legend ───────────────────────── */
    #custom-legend {{
      position:absolute; bottom:18px; right:16px; z-index:20;
      background:rgba(0,0,0,0.88);
      border:1px solid rgba(80,110,150,0.50);
      border-radius:7px;
      padding:10px 13px 10px 10px;
      min-width:170px;
      pointer-events:none;
    }}
    .leg-title {{
      font-family:Arial,sans-serif; font-size:11px; font-weight:700;
      color:#ccc; letter-spacing:.06em; text-transform:uppercase;
      margin-bottom:7px; padding-bottom:5px;
      border-bottom:1px solid rgba(255,255,255,0.12);
    }}
    .leg-row {{
      display:flex; align-items:center; gap:8px;
      margin-bottom:5px;
    }}
    .leg-row:last-child {{ margin-bottom:0; }}
    .leg-row span {{
      font-family:Arial,sans-serif; font-size:11px; color:#ccc;
      white-space:nowrap;
    }}
  </style>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
</head>
<body>
<div id="plot"></div>
{legend_html}
<script>
  const traces = {traces_json};
  const layout = {layout_json};
  const config = {config_json};

  Plotly.newPlot('plot', traces, layout, config).then(() => {{
    return Plotly.relayout('plot', {{ 'scene.camera': {cam_json} }});
  }}).then(() => {{

    /* ── Hover: relay layer info to parent via postMessage ── */
    document.getElementById('plot').on('plotly_hover', function(data) {{
      const pt = data.points && data.points[0];
      if (!pt) return;

      // Plotly returns customdata as:
      //   mesh3d   → pt.customdata = [depth_top, depth_bot]   (the vertex's entry)
      //   scatter3d → pt.customdata = [depth_top, depth_bot]   (the point's entry)
      // Both shapes are identical after our fix; just guard against nested arrays.
      let cd = pt.customdata;
      if (!cd) return;
      if (Array.isArray(cd[0])) cd = cd[0];   // unwrap if accidentally nested

      const depth_top = parseFloat(cd[0]);
      const depth_bot = parseFloat(cd[1]);
      if (isNaN(depth_top) || isNaN(depth_bot)) return;

      window.parent.postMessage({{
        type: 'bhLayerHover',
        depth_top,
        depth_bot,
      }}, '*');
    }});

    /* ── Unhover: clear highlight in parent ── */
    document.getElementById('plot').on('plotly_unhover', function() {{
      window.parent.postMessage({{ type: 'bhLayerUnhover' }}, '*');
    }});
  }});

  window.addEventListener('resize', () => Plotly.Plots.resize('plot'));
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html', layers=LAYER_CATALOG)

@app.route('/api/layers')
def get_layers():
    return jsonify(LAYER_CATALOG)

@app.route('/api/pipeline_status')
def pipeline_status():
    return jsonify({'available': PIPELINE_AVAILABLE,
                    'error':     PIPELINE_ERROR if not PIPELINE_AVAILABLE else ''})

# ── Regular borehole upload ────────────────────────────────────────────────────
@app.route('/api/upload_boreholes', methods=['POST'])
def upload_boreholes():
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    raw      = file.read()
    filename = file.filename.lower()
    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(raw))
        elif filename.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            return jsonify({'error': 'Unsupported file type — use .xlsx, .xls or .csv'}), 400
    except Exception as exc:
        return jsonify({'error': f'Could not parse file: {exc}'}), 400

    utm_zone        = int(request.form.get('utm_zone', 43))
    records, error  = process_borehole_df(df, utm_zone=utm_zone)
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'count': len(records), 'boreholes': records})


# ── Log data pipeline  (colour → 3D generation) ───────────────────────────────
@app.route('/api/process_log', methods=['POST'])
def process_log():
    global _3d_html_cache, _processed_xlsx_path

    if not PIPELINE_AVAILABLE:
        return jsonify({'error': (
            'Pipeline modules not found next to app.py. '
            'Place ruleScript.py and borehole_3d_viz.py in the same folder. '
            f'Detail: {PIPELINE_ERROR}'
        )}), 500

    if 'file' not in request.files:
        return jsonify({'error': 'No file in request'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': 'Please upload an Excel file (.xlsx / .xls)'}), 400

    utm_zone    = int(request.form.get('utm_zone', 43))
    tmp_dir     = tempfile.mkdtemp(prefix='bhlog_')
    input_path  = os.path.join(tmp_dir, 'logData.xlsx')
    output_path = os.path.join(tmp_dir, 'my_output.xlsx')

    try:
        file.save(input_path)

        # Step 1 — auto-colour + texture assignment
        colour_file(input_path, output_path)

        # Step 2 — load all boreholes from coloured workbook
        boreholes = load_boreholes(output_path)
        if not boreholes:
            return jsonify({'error': 'No valid borehole records found in log file.'}), 400

        # Step 3 — global normaliser keeps relative positions correct
        nx_fn, ny_fn = build_normaliser(boreholes)

        # Step 4 — generate compact 3D HTML for each borehole
        _3d_html_cache = {}
        for bh_id, bh_data in boreholes.items():
            _3d_html_cache[bh_id] = _generate_bh_html(bh_id, bh_data, nx_fn, ny_fn)

        # Step 5 — convert UTM coords → lat/lon for ArcGIS map
        _processed_xlsx_path = output_path     # keep for download endpoint
        records = []
        for bh_id, bh in boreholes.items():
            try:
                lat, lon = utm_to_latlon(bh['x'], bh['y'], utm_zone, northern=True)
            except Exception:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            max_depth = max((l['depth_bot'] for l in bh['layers']), default=0)
            records.append({
                'id':             bh_id,
                'borehole_no':    str(bh_id),
                'lat':            round(lat, 6),
                'lon':            round(lon, 6),
                'coordinates_raw': f"{bh['x']:.1f} m E, {bh['y']:.1f} m N",
                'water_depth':    f"{bh['water_depth']} m",
                'max_depth':      max_depth,
                'layers_count':   len(bh['layers']),
                'has_3d':         True,
                'stratigraphy': [
                    {'depth': f"{l['depth_top']}–{l['depth_bot']} m",
                     'depth_top': l['depth_top'],
                     'depth_bot': l['depth_bot'],
                     'description': l['strat']}
                    for l in bh['layers']
                ],
            })

        if not records:
            return jsonify({
                'error': 'Coordinates could not be converted to lat/lon — verify UTM zone.'
            }), 400

        return jsonify({'count': len(records), 'boreholes': records,
                        'pipeline': 'log_processed'})

    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Serve per-borehole 3D iframe ──────────────────────────────────────────────
@app.route('/api/borehole_3d/<int:bh_id>')
def borehole_3d(bh_id):
    html = _3d_html_cache.get(bh_id)
    if html is None:
        return ('<html><body style="background:#0d1117;color:#aaa;'
                'font-family:sans-serif;padding:30px;text-align:center">'
                '<h3>3D view not available</h3>'
                '<p>Run the Log Pipeline first.</p></body></html>'), 404
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ── Download processed (coloured) Excel ───────────────────────────────────────
@app.route('/api/download_processed')
def download_processed():
    if not _processed_xlsx_path or not os.path.exists(_processed_xlsx_path):
        return jsonify({'error': 'No processed file — run the pipeline first.'}), 404
    return send_file(
        _processed_xlsx_path,
        as_attachment=True,
        download_name='my_output.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


if __name__ == '__main__':
    app.run(debug=True, port=5050)
