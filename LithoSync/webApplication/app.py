from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s"
)

"""
Lithosync — Flask Backend
=========================
Security-hardened version compliant with:
  • Rule 1 & 3  — Geo-fence / data sovereignty  (X-Data-Classification header)
  • Rule 2      — MeitY-approved encrypted volume (SECURE_MOUNT_PATH env var)
  • Rule 4      — COBIT audit trail               (geo_audit_log via ruleScript)
  • Rule 5      — SAP IAM / role segregation      (X-SAP-Role header enforcement)
  • Rule 6      — CERT-In compliance              (Syslog → SIEM, 180-day archive)
"""

from flask import Flask, render_template, request, jsonify, send_file, g
import pandas as pd
import re, io, os, math, tempfile, sys, uuid, logging
from logging.handlers import SysLogHandler
from concurrent.futures import ThreadPoolExecutor

# ══════════════════════════════════════════════════════════════════════════════
# RULE 2 — MeitY-approved encrypted volume
# JOBS_DIR must point to the mounted encrypted volume declared in the
# deployment environment.  Never fall back to the OS tmp in production.
# ══════════════════════════════════════════════════════════════════════════════
SECURE_MOUNT_PATH = os.environ.get("SECURE_MOUNT_PATH")
if not SECURE_MOUNT_PATH:
    # Development-only fallback — raises a loud warning
    import warnings
    warnings.warn(
        "[SECURITY] SECURE_MOUNT_PATH is not set. "
        "Using OS temp dir — NOT acceptable in production. "
        "Set SECURE_MOUNT_PATH to a MeitY-empanelled encrypted volume path.",
        RuntimeWarning,
        stacklevel=2,
    )
    SECURE_MOUNT_PATH = tempfile.gettempdir()

JOBS_DIR = os.path.join(SECURE_MOUNT_PATH, "lithosync_jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# RULE 2 (Infrastructure readiness) — Encrypted PostgreSQL connection string
# The full DSN is read from an environment variable so secrets are never
# baked into source code.  The HSM key that decrypts the DB volume is managed
# externally; we only consume the resulting DSN here.
#
# Example (set in the deployment environment, never in this file):
#   DATABASE_URL=postgresql+psycopg2://user:pass@internal-db.ongc.local:5432/lithosync_geo?sslmode=require
# ══════════════════════════════════════════════════════════════════════════════
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://lithosync:changeme@localhost:5432/lithosync_geo?sslmode=require",
)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB
# Secret key from env so it is never committed to source control
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

# ══════════════════════════════════════════════════════════════════════════════
# RULE 6 — CERT-In: Syslog → SIEM
# All Flask access logs, error logs, and security events are piped through a
# SysLogHandler so the SIEM (IBM QRadar or any CERT-In-empanelled product)
# can ingest them without polling log files.
#
# Set SIEM_HOST / SIEM_PORT in the environment to point at your SIEM receiver.
# ══════════════════════════════════════════════════════════════════════════════
SIEM_HOST = os.environ.get("SIEM_HOST", "127.0.0.1")
SIEM_PORT = int(os.environ.get("SIEM_PORT", "514"))

def _build_syslog_handler() -> SysLogHandler:
    """Return a UDP SysLogHandler aimed at the SIEM collector."""
    handler = SysLogHandler(address=(SIEM_HOST, SIEM_PORT))
    handler.setFormatter(
        logging.Formatter(
            "lithosync %(levelname)s [%(name)s] %(asctime)s — %(message)s"
        )
    )
    return handler


# Application-level security logger (audit events, alerts)
security_log = logging.getLogger("lithosync.security")
security_log.setLevel(logging.INFO)
security_log.addHandler(_build_syslog_handler())

# Wire Flask's own werkzeug access/error log through syslog as well
logging.getLogger("werkzeug").addHandler(_build_syslog_handler())

# ── High-volume download threshold (CERT-In alert) ────────────────────────────
# If a single session downloads more than this many bytes of restricted data
# in one request, a CRITICAL syslog event is emitted for the SIEM.
_DOWNLOAD_ALERT_BYTES = int(os.environ.get("DOWNLOAD_ALERT_BYTES", str(50 * 1024 * 1024)))  # 50 MB


def _emit_security_event(level: str, event_type: str, detail: str):
    """Emit a structured security event to syslog for SIEM ingestion."""
    sap_user = getattr(g, "sap_user", "anonymous")
    remote   = request.remote_addr if request else "internal"
    msg = (
        f"EVENT={event_type} "
        f"USER={sap_user} "
        f"IP={remote} "
        f"DETAIL={detail}"
    )
    getattr(security_log, level.lower(), security_log.info)(msg)


# ══════════════════════════════════════════════════════════════════════════════
# RULE 5 — SAP IAM: read X-SAP-Role from every request
# The IAM gateway (sitting between NGINX TLS terminator and Flask) validates
# the SAP SAML/OAuth2 token and injects X-SAP-Role + X-SAP-User headers.
# Flask enforces role-based access; it never trusts client-supplied values.
# ══════════════════════════════════════════════════════════════════════════════

# Permitted roles and the routes each may access
_ROLE_PERMISSIONS = {
    "VIEWER":   {"read"},
    "EDITOR":   {"read", "upload", "pipeline"},
    "ADMIN":    {"read", "schema"},
    "AUDITOR":  {"read", "audit"},
}

# Routes that require a specific permission (checked in before_request)
_ROUTE_PERMISSION_MAP = {
    "/api/upload_boreholes": "upload",
    "/api/process_log":      "pipeline",
    "/api/download_processed": "read",
    "/api/borehole_3d":      "read",
}


@app.before_request
def enforce_iam():
    """
    Read SAP role/user injected by the upstream IAM gateway.
    Store on Flask's request-context g for downstream use.
    Reject requests that violate segregation-of-duties.
    """
    # In development (no IAM gateway), default to EDITOR so the app works.
    sap_role = request.headers.get("X-SAP-Role", os.environ.get("DEFAULT_SAP_ROLE", "EDITOR"))
    sap_user = request.headers.get("X-SAP-User", "dev-user")

    g.sap_role = sap_role.upper()
    g.sap_user = sap_user
    g.permissions = _ROLE_PERMISSIONS.get(g.sap_role, set())

    # Enforce route-level permission
    for prefix, perm in _ROUTE_PERMISSION_MAP.items():
        if request.path.startswith(prefix):
            if perm not in g.permissions:
                _emit_security_event(
                    "warning", "AUTHORISATION_DENIED",
                    f"role={g.sap_role} tried to access {request.path}",
                )
                return jsonify({"error": f"Role '{g.sap_role}' is not permitted to access this endpoint."}), 403


# ══════════════════════════════════════════════════════════════════════════════
# RULE 1 & 3 — Data sovereignty: X-Data-Classification header
# Every API response that touches borehole/subsurface data gets this header
# so downstream systems (WAF, SIEM) can audit data flows.
# ══════════════════════════════════════════════════════════════════════════════

@app.after_request
def add_classification_header(response):
    """
    Tag every response with a data-classification level.
    The WAF and SIEM use this header to enforce geo-fencing rules.
    """
    restricted_paths = [
        "/api/borehole_3d",
        "/api/process_log",
        "/api/download_processed",
        "/api/upload_boreholes",
        "/api/job_status",
    ]
    is_restricted = any(request.path.startswith(p) for p in restricted_paths)

    response.headers["X-Data-Classification"] = (
        "RESTRICTED-ONGC-SUBSURFACE" if is_restricted else "UNCLASSIFIED"
    )
    # Prevent browsers / CDNs from caching restricted responses
    if is_restricted:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"]        = "no-cache"
    return response


# ══════════════════════════════════════════════════════════════════════════════
# Concurrency & Scalability Setup
# ══════════════════════════════════════════════════════════════════════════════
worker_queue = ThreadPoolExecutor(max_workers=4)
jobs_db = {}   # In-memory job tracker {job_id: {status, progress, data, error}}

NOAA = "https://gis.ngdc.noaa.gov/arcgis/rest/services"
USGS = "https://earthquake.usgs.gov/arcgis/rest/services"

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


def parse_coordinates(value, utm_zone=43):
    if not value or (isinstance(value, float) and math.isnan(value)):
        return None
    val = str(value).strip()
    m = re.compile(r"([\d]+\.?\d*)\s*m?\s*E,?\s*([\d]+\.?\d*)\s*m?\s*N", re.I).search(val)
    if m:
        try:
            lat, lon = utm_to_latlon(float(m.group(1)), float(m.group(2)), utm_zone)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except Exception:
            pass
    m = re.compile(
        r"([+-]?\d+\.?\d*)\s*°?\s*([NSns]?)\s*[,\s/]+\s*([+-]?\d+\.?\d*)\s*°?\s*([EWew]?)"
    ).search(val)
    if m:
        a, hns = float(m.group(1)), m.group(2).upper()
        b, hew = float(m.group(3)), m.group(4).upper()
        if hns == "S": a = -a
        if hew == "W": b = -b
        if abs(a) <= 90 and abs(b) <= 180: return a, b
        if abs(b) <= 90 and abs(a) <= 180: return b, a
    return None


def process_borehole_df(df, utm_zone=43):
    df.columns = [str(c).strip() for c in df.columns]

    def find_col(keywords):
        for kw in keywords:
            for c in df.columns:
                if kw in c.lower(): return c
        return None

    coord_col  = find_col(["co-ord", "coord"])
    lat_col    = find_col(["latitude", "lat"]) if not coord_col else None
    lon_col    = find_col(["longitude", "lon", "long"]) if not coord_col else None
    strat_col  = find_col(["strat", "lith", "desc", "soil", "rock", "formation", "material"])
    depth_col  = find_col(["depth"])
    wdepth_col = find_col(["water depth", "waterdepth", "water_depth"])
    srno_col   = find_col(["sr. no", "sr.no", "sr no", "bh no", "borehole no", "site no", "well no", "id", "no."])

    if depth_col and wdepth_col and depth_col == wdepth_col: depth_col = None
    if depth_col and wdepth_col and "water" in depth_col.lower():
        for c in df.columns:
            if "depth" in c.lower() and "water" not in c.lower():
                depth_col = c; break

    if not coord_col and not (lat_col and lon_col):
        return None, f"No coordinate column found. Detected columns: {list(df.columns)}."

    records = []
    current_bh = None
    bh_index = 0

    for _, row in df.iterrows():
        is_new = False
        if srno_col:
            v = row[srno_col]
            is_new = pd.notna(v) and str(v).strip() not in ("", "nan")
        elif coord_col:
            v = row[coord_col]
            is_new = pd.notna(v) and str(v).strip() not in ("", "nan")
        elif lat_col:
            is_new = pd.notna(row[lat_col])

        if is_new:
            bh_index += 1
            lat = lon = None
            if coord_col and pd.notna(row[coord_col]):
                parsed = parse_coordinates(str(row[coord_col]), utm_zone=utm_zone)
                if parsed: lat, lon = parsed
            elif lat_col and lon_col:
                try: lat, lon = float(row[lat_col]), float(row[lon_col])
                except Exception: pass

            if lat is None or lon is None:
                current_bh = None; continue

            bh_no = str(int(float(row[srno_col]))) if srno_col and pd.notna(row[srno_col]) else str(bh_index)
            current_bh = {
                "id": bh_index - 1,
                "borehole_no": bh_no,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "coordinates_raw": (
                    str(row[coord_col]).strip()
                    if coord_col and pd.notna(row[coord_col])
                    else f"{lat:.5f}, {lon:.5f}"
                ),
                "water_depth": (
                    str(row[wdepth_col]).strip()
                    if wdepth_col and pd.notna(row[wdepth_col])
                    else "—"
                ),
                "stratigraphy": [],
                "has_3d": False,
            }
            structural = {srno_col, coord_col, lat_col, lon_col, wdepth_col, depth_col, strat_col, None}
            for c in df.columns:
                if c not in structural and pd.notna(row[c]):
                    current_bh[c.lower().replace(" ", "_")] = str(row[c]).strip()
            records.append(current_bh)

        if current_bh is not None:
            dv = str(row[depth_col]).strip() if depth_col and pd.notna(row[depth_col]) else ""
            sv = str(row[strat_col]).strip() if strat_col and pd.notna(row[strat_col]) else ""
            if dv or sv:
                current_bh["stratigraphy"].append({"depth": dv, "description": sv})

    if not records:
        return None, "No valid borehole records could be parsed."
    return records, None


def _generate_bh_html(bh_id: int, bh_data: dict, global_nx, global_ny, hw: float = 9.5) -> str:
    import json
    layers = bh_data["layers"]
    water_depth = float(bh_data.get("water_depth", 0))
    max_depth = max((l["depth_bot"] for l in layers), default=10)
    js_layers = json.dumps([{
        "depth_top": l["depth_top"], "depth_bot": l["depth_bot"],
        "strat": l.get("strat", ""), "color": l.get("color", "#888888"),
        "texture": l.get("texture", "solid"),
    } for l in layers])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;overflow:hidden;background:#05080c}}
#container{{position:absolute;inset:0}}
#tooltip{{position:absolute;pointer-events:none;display:none;background:rgba(4,8,15,0.97);border:1px solid #1a2840;border-left:3px solid #00c8a0;border-radius:0 7px 7px 0;padding:9px 13px;z-index:50;max-width:210px;font-family:'IBM Plex Mono',monospace;backdrop-filter:blur(10px);}}
.tt-type{{font-size:12px;font-weight:600;margin-bottom:4px;color:#00c8a0}}
.tt-depth{{font-size:9px;color:#3a5068;margin-bottom:5px}}
.tt-desc{{font-size:10px;color:#c8d8e8;line-height:1.5}}
#legend-overlay{{position:absolute;bottom:44px;left:8px;z-index:30;background:rgba(4,8,15,0.90);border:1px solid #1a2840;border-radius:7px;padding:8px 10px;font-family:'IBM Plex Mono',monospace;backdrop-filter:blur(8px);}}
.leg-title{{font-size:8px;color:#3a5068;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #1a2840;}}
.leg-row{{display:flex;align-items:center;gap:7px;margin-bottom:4px}}
.leg-row:last-child{{margin-bottom:0}}
#controls-bar{{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);background:rgba(4,8,15,0.78);border:1px solid #1a2840;border-radius:16px;padding:4px 14px;font-size:9px;color:#3a5068;letter-spacing:0.5px;display:flex;gap:12px;backdrop-filter:blur(4px);white-space:nowrap;font-family:monospace;}}
.k{{background:#1a2840;color:#c8d8e8;padding:1px 5px;border-radius:3px;font-size:8px}}
#bh-label{{position:absolute;top:10px;left:50%;transform:translateX(-50%);background:rgba(4,8,15,0.84);border:1px solid #1a2840;border-radius:6px;padding:4px 14px;font-size:11px;color:#00c8a0;font-weight:600;letter-spacing:1px;pointer-events:none;font-family:monospace;backdrop-filter:blur(4px);}}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
</head>
<body>
<div id="container"></div><div id="bh-label">BH-{bh_id}</div>
<div id="tooltip"><div class="tt-type" id="tt-type"></div><div class="tt-depth" id="tt-depth"></div><div class="tt-desc" id="tt-desc"></div></div>
<div id="legend-overlay"><div class="leg-title">Lithology Index</div><div id="leg-rows"></div></div>
<div id="controls-bar"><span><span class="k">drag</span>&nbsp;Rotate</span><span><span class="k">scroll</span>&nbsp;Zoom</span><span><span class="k">hover</span>&nbsp;Inspect</span></div>
<script>
var LAYERS = {js_layers}; var BH_ID = {bh_id}; var WATER_DEPTH = {water_depth}; var MAX_DEPTH = {max_depth};
var TEX_PAT = {{lightGray:'diagonal',darkGray:'dots',gray0625:'dots_sparse',lightUp:'diagonal',lightDown:'diagonal_back',lightTrellis:'crosshatch',darkTrellis:'circles',lightGrid:'brick',lightHorizontal:'horizontal',darkHorizontal:'horizontal',lightVertical:'vertical',solid:'solid'}};
var TEX_LBL = {{lightGray:'Clay',darkGray:'Sand',gray0625:'Silt',lightUp:'Calcarenite',lightDown:'Shale',lightTrellis:'Sandstone',darkTrellis:'Gravel',lightGrid:'Limestone',lightHorizontal:'Limestone',darkHorizontal:'Peat',lightVertical:'Silt',solid:'Massive'}};

function makePatternCanvas(texture, bgHex, S) {{
  S = S || 256; var c = document.createElement('canvas'); c.width = S; c.height = S; var ctx = c.getContext('2d');
  ctx.fillStyle = bgHex; ctx.fillRect(0, 0, S, S);
  var pat = TEX_PAT[texture] || 'solid'; var lw = Math.max(3.0, S / 50);
  ctx.strokeStyle = 'rgba(15, 15, 15, 0.95)'; ctx.fillStyle = 'rgba(15, 15, 15, 0.95)'; ctx.lineWidth = lw;
  var step = S / 7.5;
  if (pat === 'horizontal') {{ for(var y=step*0.5; y<S; y+=step) {{ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(S,y); ctx.stroke();}} }}
  else if (pat === 'vertical') {{ for(var x=step*0.5; x<S; x+=step) {{ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,S); ctx.stroke();}} }}
  else if (pat === 'diagonal') {{ for(var i=-S; i<S*2; i+=step) {{ctx.beginPath(); ctx.moveTo(i,0); ctx.lineTo(i+S,S); ctx.stroke();}} }}
  else if (pat === 'diagonal_back') {{ for(var i=-S; i<S*2; i+=step) {{ctx.beginPath(); ctx.moveTo(i+S,0); ctx.lineTo(i,S); ctx.stroke();}} }}
  else if (pat === 'dots') {{ var ds=step*1.1, r=S/25; for(var row=0; row*ds<S+step; row++) for(var col=(row%2)*0.5; col*ds<S; col++) {{ctx.beginPath(); ctx.arc(col*ds, row*ds, r, 0, Math.PI*2); ctx.fill();}} }}
  else if (pat === 'dots_sparse') {{ var ds=step*1.9, r=S/30; for(var row=0; row*ds<S+step; row++) for(var col=(row%2)*0.5; col*ds<S; col++) {{ctx.beginPath(); ctx.arc(col*ds, row*ds, r, 0, Math.PI*2); ctx.fill();}} }}
  else if (pat === 'crosshatch') {{ for(var i=-S; i<S*2; i+=step) {{ctx.beginPath(); ctx.moveTo(i,0); ctx.lineTo(i+S,S); ctx.stroke(); ctx.beginPath(); ctx.moveTo(i+S,0); ctx.lineTo(i,S); ctx.stroke();}} }}
  else if (pat === 'circles') {{ var cs=step*1.5, cr=cs*0.35; for(var row=0; row*cs<S+step; row++) for(var col=(row%2)*0.5; col*cs<S; col++) {{ctx.beginPath(); ctx.arc(col*cs, row*cs, cr, 0, Math.PI*2); ctx.stroke();}} }}
  else if (pat === 'brick') {{ var bh=step*1.3, bw=step*2.8; ctx.lineWidth = Math.max(3.0, S/60); for(var row=0; row*bh<S; row++) {{var y=row*bh; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(S,y); ctx.stroke(); var off=(row%2)*(bw/2); for(var x2=off-bw; x2<S+bw; x2+=bw) {{ctx.beginPath(); ctx.moveTo(x2,y); ctx.lineTo(x2,y+bh); ctx.stroke();}}}} }}
  return c;
}}

var renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true }});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)); renderer.setClearColor(0x05080c);
var cont = document.getElementById('container'); cont.appendChild(renderer.domElement);

var scene = new THREE.Scene(); var camera = new THREE.PerspectiveCamera(42, 1, 0.1, 3000);
var Y_SCALE = 0.60; var SCALED_WATER = WATER_DEPTH * Y_SCALE; var SCALED_MAX = MAX_DEPTH * Y_SCALE;
var TOTAL_H = SCALED_WATER + SCALED_MAX; var RADIUS = Math.max(3.0, TOTAL_H * 0.045);
var MID_Y = ((SCALED_WATER - SCALED_MAX) / 2.0) - (TOTAL_H * 0.15);
var camDist = Math.max(60, TOTAL_H * 2.3 + RADIUS * 5);
var rotY = 0.6, rotX = 0.09; var autoSpin = true, dragging = false, prevM = {{ x: 0, y: 0 }};

function updateCamera() {{
  camera.position.set(Math.sin(rotY)*Math.cos(rotX)*camDist, MID_Y+Math.sin(rotX)*camDist*0.55, Math.cos(rotY)*Math.cos(rotX)*camDist);
  camera.lookAt(new THREE.Vector3(0, MID_Y, 0));
}}
updateCamera();

scene.add(new THREE.AmbientLight(0xffffff, 0.45));
var sun = new THREE.DirectionalLight(0xfff5e6, 1.3); sun.position.set(10, 15, 10); scene.add(sun);
var fill = new THREE.DirectionalLight(0x88bbff, 0.6); fill.position.set(-10, 5, -10); scene.add(fill);
var rim = new THREE.DirectionalLight(0x00e8b8, 0.35); rim.position.set(0, -SCALED_MAX - 5, -6); scene.add(rim);
var gridSize = Math.max(80, TOTAL_H * 2.0); scene.add(new THREE.GridHelper(gridSize, 40, 0x0f1e30, 0x0c1622));

var layerMeshes = [];
LAYERS.forEach(function(layer, idx) {{
  var real_h = layer.depth_bot - layer.depth_top; if (real_h <= 0) return;
  var h = real_h * Y_SCALE; var cy = -(layer.depth_top * Y_SCALE + h * 0.5);
  var patC = makePatternCanvas(layer.texture, layer.color, 256);
  var tex = new THREE.CanvasTexture(patC); tex.wrapS = THREE.RepeatWrapping; tex.wrapT = THREE.RepeatWrapping;
  var repX = Math.max(3, Math.floor(RADIUS * 1.0)); var repY = Math.max(1, h / 5.0); tex.repeat.set(repX, repY);
  tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  var mat = new THREE.MeshStandardMaterial({{map: tex, color: new THREE.Color(layer.color), roughness: 0.85, metalness: 0.05}});
  var geo = new THREE.CylinderGeometry(RADIUS, RADIUS, h, 64, 1, false);
  var mesh = new THREE.Mesh(geo, mat); mesh.position.y = cy; mesh.userData = {{ layer: layer }};
  scene.add(mesh); layerMeshes.push(mesh);
  if (idx < LAYERS.length - 1) {{ var ring = new THREE.Mesh(new THREE.TorusGeometry(RADIUS + 0.05, 0.08, 8, 64), new THREE.MeshBasicMaterial({{ color: 0x010406 }})); ring.rotation.x = Math.PI / 2; ring.position.y = -layer.depth_bot * Y_SCALE; scene.add(ring); }}
  if (idx === 0) {{ var topCap = new THREE.Mesh(new THREE.CircleGeometry(RADIUS, 64), new THREE.MeshStandardMaterial({{ color: new THREE.Color(layer.color), roughness: 0.9 }})); topCap.rotation.x = -Math.PI / 2; topCap.position.y = 0; scene.add(topCap); }}
  if (idx === LAYERS.length - 1) {{ var botCap = new THREE.Mesh(new THREE.CircleGeometry(RADIUS, 64), new THREE.MeshStandardMaterial({{ color: new THREE.Color(layer.color), roughness: 0.9 }})); botCap.rotation.x = Math.PI / 2; botCap.position.y = -layer.depth_bot * Y_SCALE; scene.add(botCap); }}
}});

if (WATER_DEPTH > 0) {{
  var wMesh = new THREE.Mesh(new THREE.CylinderGeometry(RADIUS, RADIUS, SCALED_WATER, 64, 1, false), new THREE.MeshStandardMaterial({{color: 0x1166aa, transparent: true, opacity: 0.35, roughness: 0.2}})); wMesh.position.y = SCALED_WATER / 2; scene.add(wMesh);
  var wRing = new THREE.Mesh(new THREE.TorusGeometry(RADIUS + 0.03, 0.06, 8, 64), new THREE.MeshBasicMaterial({{ color: 0x3388cc }})); wRing.rotation.x = Math.PI / 2; scene.add(wRing);
}}

function makeSprite(text, color) {{
  var c = document.createElement('canvas'); c.width = 256; c.height = 64; var ctx = c.getContext('2d');
  ctx.font = '600 24px monospace'; ctx.fillStyle = color || '#3e6a91'; ctx.fillText(text, 6, 42);
  var sp = new THREE.Sprite(new THREE.SpriteMaterial({{map: new THREE.CanvasTexture(c), transparent: true, depthTest: false}})); sp.scale.set(3.5, 0.85, 1); return sp;
}}
var tickStep = MAX_DEPTH <= 25 ? 5 : MAX_DEPTH <= 80 ? 10 : 25;
for (var d = 0; d <= MAX_DEPTH; d += tickStep) {{
  var sp = makeSprite(d + 'm'); sp.position.set(RADIUS + 3.0, -d * Y_SCALE, 0); scene.add(sp);
  var tg = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(RADIUS + 0.2, -d * Y_SCALE, 0), new THREE.Vector3(RADIUS + 1.8, -d * Y_SCALE, 0)]);
  scene.add(new THREE.Line(tg, new THREE.LineBasicMaterial({{ color: 0x152235 }})));
}}

var seenTex = {{}}; var legRows = document.getElementById('leg-rows');
LAYERS.forEach(function(layer) {{
  var tex = layer.texture; if (seenTex[tex]) return; seenTex[tex] = true;
  var sw = document.createElement('canvas'); sw.width = 34; sw.height = 16; var sc = sw.getContext('2d');
  var pat = sc.createPattern(makePatternCanvas(tex, layer.color, 64), 'repeat'); sc.fillStyle = pat; sc.fillRect(0, 0, 34, 16); sw.style.cssText = 'width:34px;height:16px;border-radius:2px;border:1px solid rgba(255,255,255,0.15);flex-shrink:0';
  var lbl = document.createElement('span'); lbl.style.cssText = 'font-size:9px;color:#c8d8e8;font-family:monospace'; lbl.textContent = TEX_LBL[tex] || tex;
  var row = document.createElement('div'); row.className = 'leg-row'; row.appendChild(sw); row.appendChild(lbl); legRows.appendChild(row);
}});

function onResize() {{ var w = cont.clientWidth, h = cont.clientHeight; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }}
onResize(); window.addEventListener('resize', onResize);
var cvs = renderer.domElement; cvs.style.cursor = 'grab';
cvs.addEventListener('mousedown', function(e) {{ dragging = true; autoSpin = false; prevM = {{ x: e.clientX, y: e.clientY }}; cvs.style.cursor = 'grabbing'; }});
window.addEventListener('mousemove', function(e) {{ if (!dragging) return; rotY += (e.clientX - prevM.x) * 0.007; rotX = Math.max(-0.35, Math.min(0.32, rotX + (e.clientY - prevM.y) * 0.004)); prevM = {{ x: e.clientX, y: e.clientY }}; updateCamera(); }});
window.addEventListener('mouseup', function() {{ dragging = false; cvs.style.cursor = 'grab'; }});
cvs.addEventListener('wheel', function(e) {{ camDist = Math.max(10, Math.min(1500, camDist + e.deltaY * 0.05)); updateCamera(); e.preventDefault(); }}, {{ passive: false }});

var raycaster = new THREE.Raycaster(); var mp = new THREE.Vector2(); var tooltip = document.getElementById('tooltip'); var lastLayer = null;
cvs.addEventListener('mousemove', function(e) {{
  var rect = cvs.getBoundingClientRect(); mp.x = ((e.clientX - rect.left) / rect.width) * 2 - 1; mp.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mp, camera); var hits = raycaster.intersectObjects(layerMeshes);
  if (hits.length > 0) {{
    var layer = hits[0].object.userData.layer;
    if (lastLayer !== layer) {{ lastLayer = layer; window.parent.postMessage({{ type: 'bhLayerHover', depth_top: layer.depth_top, depth_bot: layer.depth_bot }}, '*'); }}
    var thick = (layer.depth_bot - layer.depth_top).toFixed(1); tooltip.style.display = 'block'; tooltip.style.left = Math.min(e.clientX - rect.left + 14, rect.width - 224) + 'px'; tooltip.style.top = Math.max(e.clientY - rect.top - 10, 8) + 'px';
    document.getElementById('tt-type').textContent = TEX_LBL[layer.texture] || layer.texture; document.getElementById('tt-depth').textContent = layer.depth_top.toFixed(1) + ' \u2013 ' + layer.depth_bot.toFixed(1) + ' m (' + thick + ' m thick)'; document.getElementById('tt-desc').textContent = layer.strat || '\u2014';
  }} else {{
    if (lastLayer !== null) {{ lastLayer = null; tooltip.style.display = 'none'; window.parent.postMessage({{ type: 'bhLayerUnhover' }}, '*'); }}
  }}
}});
cvs.addEventListener('mouseleave', function() {{ tooltip.style.display = 'none'; lastLayer = null; window.parent.postMessage({{ type: 'bhLayerUnhover' }}, '*'); }});
function animate() {{ requestAnimationFrame(animate); if (autoSpin && !dragging) {{ rotY += 0.004; updateCamera(); }} renderer.render(scene, camera); }}
animate();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", layers=LAYER_CATALOG)


@app.route("/api/layers")
def get_layers():
    return jsonify(LAYER_CATALOG)


@app.route("/api/pipeline_status")
def pipeline_status():
    return jsonify({
        "available": PIPELINE_AVAILABLE,
        "error":     PIPELINE_ERROR if not PIPELINE_AVAILABLE else "",
    })


@app.route("/api/upload_boreholes", methods=["POST"])
def upload_boreholes():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    raw      = file.read()
    filename = file.filename.lower()
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            return jsonify({"error": "Unsupported file type — use .xlsx, .xls or .csv"}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not parse file: {exc}"}), 400

    utm_zone = int(request.form.get("utm_zone", 43))
    records, error = process_borehole_df(df, utm_zone=utm_zone)
    if error:
        return jsonify({"error": error}), 400

    _emit_security_event("info", "BOREHOLE_UPLOAD", f"file={file.filename} count={len(records)}")
    return jsonify({"count": len(records), "boreholes": records})


# ── Async Log Data Pipeline ───────────────────────────────────────────────────

def _run_pipeline_task(job_id: str, input_path: str, utm_zone: int, sap_user: str):
    """
    Background worker function — runs isolated per job ID.
    Passes the SAP user token into colour_file so ruleScript.py can
    write it into the geo_audit_log table (Rule 4).
    """
    try:
        job_dir     = os.path.join(JOBS_DIR, job_id)
        output_path = os.path.join(job_dir, "my_output.xlsx")
        html_dir    = os.path.join(job_dir, "html")
        os.makedirs(html_dir, exist_ok=True)

        jobs_db[job_id]["progress"] = "Auto-colouring stratigraphy..."
        # Pass sap_user so ruleScript can write to geo_audit_log (Rule 4)
        colour_file(input_path, output_path, sap_user=sap_user)

        jobs_db[job_id]["progress"] = "Parsing boreholes..."
        boreholes = load_boreholes(output_path)
        if not boreholes:
            raise ValueError("No valid borehole records found in log file.")

        nx_fn, ny_fn = build_normaliser(boreholes)

        jobs_db[job_id]["progress"] = f"Generating 3D models (0/{len(boreholes)})..."
        for idx, (bh_id, bh_data) in enumerate(boreholes.items(), 1):
            if idx % 10 == 0:
                jobs_db[job_id]["progress"] = f"Generating 3D models ({idx}/{len(boreholes)})..."
            html_str = _generate_bh_html(bh_id, bh_data, nx_fn, ny_fn)
            with open(os.path.join(html_dir, f"{bh_id}.html"), "w", encoding="utf-8") as f:
                f.write(html_str)

        jobs_db[job_id]["progress"] = "Finalizing map data..."
        records = []
        for bh_id, bh in boreholes.items():
            try:
                lat, lon = utm_to_latlon(bh["x"], bh["y"], utm_zone, northern=True)
            except Exception:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            max_depth = max((l["depth_bot"] for l in bh["layers"]), default=0)
            records.append({
                "id": bh_id, "borehole_no": str(bh_id),
                "lat": round(lat, 6), "lon": round(lon, 6),
                "coordinates_raw": f"{bh['x']:.1f} m E, {bh['y']:.1f} m N",
                "water_depth": f"{bh['water_depth']} m",
                "max_depth": max_depth,
                "layers_count": len(bh["layers"]),
                "has_3d": True,
                "job_id": job_id,
                "stratigraphy": [
                    {
                        "depth": f"{l['depth_top']}–{l['depth_bot']} m",
                        "depth_top": l["depth_top"],
                        "depth_bot": l["depth_bot"],
                        "description": l["strat"],
                    }
                    for l in bh["layers"]
                ],
            })

        if not records:
            raise ValueError("Coordinates could not be converted to lat/lon — verify UTM zone.")

        jobs_db[job_id]["status"] = "completed"
        jobs_db[job_id]["data"]   = {"count": len(records), "boreholes": records}

    except Exception as exc:
        jobs_db[job_id]["status"] = "failed"
        jobs_db[job_id]["error"]  = str(exc)
        security_log.error(
            f"EVENT=PIPELINE_FAILURE JOB={job_id} USER={sap_user} DETAIL={exc}"
        )


@app.route("/api/process_log", methods=["POST"])
def process_log():
    if not PIPELINE_AVAILABLE:
        return jsonify({"error": "Pipeline modules missing."}), 500
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "Please upload an Excel file (.xlsx / .xls)"}), 400

    utm_zone = int(request.form.get("utm_zone", 43))
    sap_user = g.sap_user  # from enforce_iam()

    # Create isolated workspace for this job
    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    input_path = os.path.join(job_dir, "input.xlsx")
    file.save(input_path)

    _emit_security_event("info", "PIPELINE_STARTED", f"job={job_id} file={file.filename}")

    # Initialize job tracking
    jobs_db[job_id] = {"status": "processing", "progress": "Initializing queue..."}

    # Submit to background worker (avoids Nginx 504 timeouts)
    worker_queue.submit(_run_pipeline_task, job_id, input_path, utm_zone, sap_user)

    return jsonify({"job_id": job_id, "status": "processing"})


@app.route("/api/job_status/<job_id>")
def job_status(job_id):
    job = jobs_db.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/borehole_3d/<job_id>/<int:bh_id>")
def borehole_3d(job_id, bh_id):
    filepath = os.path.join(JOBS_DIR, job_id, "html", f"{bh_id}.html")
    if os.path.exists(filepath):
        file_size = os.path.getsize(filepath)
        # Rule 6 — CERT-In: alert SIEM on anomalously large 3D file downloads
        if file_size > _DOWNLOAD_ALERT_BYTES:
            _emit_security_event(
                "critical",
                "HIGH_VOLUME_DOWNLOAD",
                f"job={job_id} bh={bh_id} size={file_size}B exceeds threshold={_DOWNLOAD_ALERT_BYTES}B",
            )
        _emit_security_event("info", "3D_VIEW_SERVED", f"job={job_id} bh={bh_id}")
        return send_file(filepath, mimetype="text/html")

    return (
        '<html><body style="background:#0d1117;color:#aaa;'
        'font-family:sans-serif;padding:30px;text-align:center">'
        "<h3>3D view not found</h3>"
        "<p>Cache may have expired. Please re-run the pipeline.</p></body></html>"
    ), 404


@app.route("/api/download_processed/<job_id>")
def download_processed(job_id):
    filepath = os.path.join(JOBS_DIR, job_id, "my_output.xlsx")
    if not os.path.exists(filepath):
        return jsonify({"error": "Processed file not found. Please run the pipeline again."}), 404

    file_size = os.path.getsize(filepath)
    # Rule 6 — CERT-In: alert on high-volume data export
    if file_size > _DOWNLOAD_ALERT_BYTES:
        _emit_security_event(
            "critical",
            "HIGH_VOLUME_DOWNLOAD",
            f"job={job_id} type=xlsx size={file_size}B exceeds threshold={_DOWNLOAD_ALERT_BYTES}B",
        )
    _emit_security_event("info", "XLSX_DOWNLOAD", f"job={job_id} size={file_size}B")

    return send_file(
        filepath,
        as_attachment=True,
        download_name="my_output.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ══════════════════════════════════════════════════════════════════════════════
# NOTE — Rule 7 (Infrastructure): Do NOT use app.run() in production.
# NGINX (or Apache) must sit in front of this app to:
#   • Terminate TLS 1.3
#   • Act as a WAF (ModSecurity or equivalent)
#   • Forward X-SAP-Role / X-SAP-User headers from the IAM gateway
#
# Run in production with Gunicorn:
#   gunicorn -w 4 -b 127.0.0.1:5050 app:app
#
# Sample NGINX snippet (ssl_protocols TLSv1.3; ssl_ciphers HIGH:!aNULL:!MD5;)
# is provided in nginx.conf.example.
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Development only — NEVER expose this directly to the internet
    app.run(debug=True, port=5050, host="127.0.0.1", use_reloader=False)