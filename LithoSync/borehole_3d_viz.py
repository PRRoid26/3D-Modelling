#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           3D Borehole Log Visualizer  —  borehole_3d_viz.py     ║
╠══════════════════════════════════════════════════════════════════╣
║  Reads a borehole-log Excel workbook and an optional soil-type   ║
║  index workbook, then produces a fully interactive 3-D HTML      ║
║  visualisation (no server needed — opens in any browser).        ║
╠══════════════════════════════════════════════════════════════════╣
║  USAGE                                                           ║
║    python borehole_3d_viz.py  <borehole.xlsx>  [index.xlsx]      ║
║                                                                   ║
║  POSITIONAL ARGUMENTS                                             ║
║    borehole.xlsx   Path to the borehole-log workbook.            ║
║    index.xlsx      (optional) Path to soil-type index workbook.  ║
║                    Falls back to built-in defaults when omitted. ║
║                                                                   ║
║  OPTIONS                                                          ║
║    -o / --output   Output HTML file path.                        ║
║                    Default: <borehole_stem>_3d.html              ║
║    -s / --sheet    Sheet name/index for borehole data.           ║
║                    Default: first sheet                          ║
║    --index-sheet   Sheet name/index for index data.             ║
║                    Default: first sheet                          ║
║    --hw            Half-width of each borehole column in the     ║
║                    normalised plot space.  Default: 9.5          ║
║    -v / --verbose  Print per-layer pattern-match summary.        ║
║                                                                   ║
║  BOREHOLE WORKBOOK EXPECTED COLUMNS (row 1 = header)             ║
║    A  Sr. No.          – borehole ID (integer; blank = same BH)  ║
║    B  Co-ordinates     – "XXXXX.X m E, YYYYYYY.Y m N"           ║
║    C  Water Depth      – "25.1 m"                                ║
║    D  Depth            – "0.0–20.1" or "97.4–130.1 (EOB)"       ║
║    E  Stratigraphy     – free-text description                   ║
║    F  Colour           – cell fill colour (read from fill)       ║
║    G  Texture          – cell fill patternType (read from fill)  ║
║                                                                   ║
║  INDEX WORKBOOK EXPECTED COLUMNS (row 1 = header "Index")        ║
║    A  Keyword          – e.g. "Clay", "Sand", "Silt" …          ║
║       Cell fill        – patternType encodes the overlay pattern ║
║                                                                   ║
║  DEPENDENCIES                                                     ║
║    pip install openpyxl                                          ║
║    (json, re, argparse, pathlib, sys are stdlib)                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse
import json
import re
import sys
from pathlib import Path

import openpyxl


# ══════════════════════════════════════════════════════════════════
# 1.  PARSE COMMAND-LINE ARGUMENTS
# ══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        prog="borehole_3d_viz.py",
        description="Generate an interactive 3-D borehole-log visualisation from Excel data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("borehole",       help="Path to the borehole-log Excel workbook (.xlsx)")
    p.add_argument("index",          help="Path to the soil-type index workbook (.xlsx)",
                   nargs="?", default=None)
    p.add_argument("-o", "--output", help="Output HTML file path (default: <borehole>_3d.html)",
                   default=None)
    p.add_argument("-s", "--sheet",  help="Sheet name or 0-based index for borehole data (default: 0)",
                   default=0)
    p.add_argument("--index-sheet",  help="Sheet name or 0-based index for index data (default: 0)",
                   default=0, dest="index_sheet")
    p.add_argument("--hw",           help="Half-width of borehole column in plot units (default: 9.5)",
                   type=float, default=9.5)
    p.add_argument("-v", "--verbose",action="store_true",
                   help="Print per-layer pattern-match summary to stdout")
    return p.parse_args()


# ══════════════════════════════════════════════════════════════════
# 2.  EXCEL READERS
# ══════════════════════════════════════════════════════════════════

def _sheet(wb: openpyxl.Workbook, ref):
    """Return a worksheet by name (str) or 0-based index (int)."""
    if isinstance(ref, int):
        return wb.worksheets[ref]
    return wb[ref]


def load_boreholes(path: str, sheet_ref=0) -> dict:
    """
    Parse the borehole-log workbook.

    Returns
    -------
    dict  { bh_id (int) : {
                'x': float, 'y': float, 'water_depth': float,
                'layers': [ { depth_top, depth_bot, strat, color, texture } ]
            } }
    """
    wb = openpyxl.load_workbook(path)
    ws = _sheet(wb, sheet_ref)

    boreholes   = {}
    current_bh  = None
    eob_depth   = {}   # track actual end-of-borehole per BH id

    for row in ws.iter_rows(min_row=2):
        cells = {cell.column: cell for cell in row}

        # ── New borehole header row ──────────────────────────────
        sr_cell = cells.get(1)
        if sr_cell and sr_cell.value is not None:
            try:
                current_bh = int(sr_cell.value)
            except (ValueError, TypeError):
                continue

            coord_str = (cells.get(2).value or "").strip()
            x, y = _parse_coords(coord_str)

            wd_str = (cells.get(3).value or "0 m").strip()
            wd     = float(re.sub(r"[^\d.]", "", wd_str))

            boreholes[current_bh] = {
                "x": x, "y": y,
                "water_depth": wd,
                "layers": []
            }

        if current_bh is None:
            continue

        # ── Layer row ────────────────────────────────────────────
        d_cell = cells.get(4)
        if d_cell is None or d_cell.value is None:
            continue

        depth_raw = str(d_cell.value).strip()
        is_eob    = "(EOB)" in depth_raw.upper()
        depth_raw = depth_raw.replace("(EOB)", "").replace("(eob)", "").strip()

        if "–" not in depth_raw and "-" not in depth_raw:
            continue
        sep = "–" if "–" in depth_raw else "-"
        parts = depth_raw.split(sep, 1)
        try:
            d_top = float(parts[0].strip())
            d_bot = float(parts[1].strip())
        except ValueError:
            continue

        strat   = (cells[5].value if cells.get(5) else "") or ""

        # -- Colour: read solid fgColor directly from the Colour column fill
        hex_col = "#808080"
        if cells.get(6):
            try:
                argb    = cells[6].fill.fgColor.rgb   # stored as "00RRGGBB"
                hex_col = "#" + argb[2:]               # strip alpha -> "#RRGGBB"
            except Exception:
                # theme-based colour: fall back to the cell text value (#RRGGBB)
                cell_val = str(cells[6].value or "").strip().lstrip("#")
                if len(cell_val) == 6:
                    hex_col = "#" + cell_val

        # -- Texture: read patternType directly from the Texture column fill
        texture = "solid"
        if cells.get(7):
            pat = cells[7].fill.patternType
            if pat and pat not in ("none", "None", None):
                texture = pat

        boreholes[current_bh]["layers"].append({
            "depth_top": d_top,
            "depth_bot": d_bot,
            "strat":     strat,
            "color":     hex_col,
            "texture":   texture,
            "is_eob":    is_eob,
        })

    return boreholes


def _parse_coords(s: str):
    """Parse 'XXXXX.X m E, YYYYYYY.Y m N' → (x_float, y_float)."""
    m = re.findall(r"([\d.]+)\s*m\s*([EeNn])", s)
    coords = {}
    for val, direction in m:
        coords[direction.upper()] = float(val)
    return coords.get("E", 0.0), coords.get("N", 0.0)


def load_index(path: str, sheet_ref=0) -> list:
    """
    Parse the index workbook.

    Returns
    -------
    list of (keyword_upper, patternType, display_name)
    ordered so longer / more-specific keywords come first.
    """
    wb  = openpyxl.load_workbook(path)
    ws  = _sheet(wb, sheet_ref)
    raw = []

    for row in ws.iter_rows(min_row=2):
        cells = {cell.column: cell for cell in row}
        kw_cell = cells.get(1)
        if kw_cell is None or kw_cell.value is None:
            continue
        keyword  = str(kw_cell.value).strip()
        pat_type = kw_cell.fill.patternType or "solid"
        raw.append((keyword.upper().rstrip(), pat_type, keyword.strip()))

    # Sort longest keyword first → more-specific matches win
    raw.sort(key=lambda t: -len(t[0]))
    return raw


# ── Built-in fallback index (mirrors the provided Index.xlsx) ────
BUILTIN_INDEX = [
    ("SANDSTONE",   "lightTrellis", "Sandstone"),
    ("CALCARENITE", "lightUp",      "Calcarenite"),
    ("CLAY",        "lightGray",    "Clay"),
    ("SILT",        "gray0625",     "Silt"),
    ("SAND",        "darkGray",     "Sand"),
]


# ══════════════════════════════════════════════════════════════════
# 3.  KEYWORD → PATTERN MATCHING
# ══════════════════════════════════════════════════════════════════

def detect_pattern(strat_text: str, index: list):
    """
    Match stratigraphy text against the index keyword list.

    Matching rules
    ──────────────
    •  Case-insensitive whole-word match.
    •  "SILT" only matches when the word appears as the principal
       material (i.e. not merely as a modifier like "silty").
    •  Index list is pre-sorted longest-first, so "SANDSTONE"
       beats "SAND" automatically.

    Returns (patternType, display_name) or (None, None).
    """
    if not strat_text:
        return None, None

    upper = strat_text.upper()

    for keyword, pat, name in index:
        # Special guard: SILT must appear as a standalone noun
        if keyword == "SILT":
            if re.search(r"\bSILT\b", upper) and not re.search(r"\bSILTY\b", upper):
                return pat, name
            continue
        if re.search(r"\b" + re.escape(keyword) + r"\b", upper):
            return pat, name

    return None, None


# ══════════════════════════════════════════════════════════════════
# 4.  COORDINATE NORMALISATION HELPERS
# ══════════════════════════════════════════════════════════════════

def build_normaliser(boreholes: dict, target_spread=300.0):
    """Return (nx_fn, ny_fn) that map real coords → plot coords."""
    all_x = [bh["x"] for bh in boreholes.values()]
    all_y = [bh["y"] for bh in boreholes.values()]
    cx0   = sum(all_x) / len(all_x)
    cy0   = sum(all_y) / len(all_y)
    span  = max(max(all_x) - min(all_x), max(all_y) - min(all_y))
    scale = target_spread / span if span > 0 else 1.0

    def nx(v): return round((v - cx0) * scale, 3)
    def ny(v): return round((v - cy0) * scale, 3)
    return nx, ny


# ══════════════════════════════════════════════════════════════════
# 5.  3-D GEOMETRY
# ══════════════════════════════════════════════════════════════════

def box_mesh(cx, cy, z_top, z_bot, hw):
    """8-vertex box mesh for a single stratigraphic layer column."""
    x = [cx-hw, cx+hw, cx+hw, cx-hw, cx-hw, cx+hw, cx+hw, cx-hw]
    y = [cy-hw, cy-hw, cy+hw, cy+hw, cy-hw, cy-hw, cy+hw, cy+hw]
    z = [-z_top]*4 + [-z_bot]*4
    i = [0, 0, 4, 4, 0, 0, 2, 2, 3, 3, 1, 1]
    j = [1, 2, 7, 6, 4, 5, 6, 7, 7, 4, 5, 6]
    k = [2, 3, 6, 5, 5, 1, 7, 3, 4, 0, 6, 2]
    return x, y, z, i, j, k


def pattern_on_face(face, cx, cy, z_top, z_bot, hw, pat_type):
    """
    Build scatter-3d geometry for one pattern on one face of a box.

    face    : 'front' | 'back' | 'left' | 'right'
    pat_type: Excel patternType string
    Returns a plotly trace dict, or None.
    """
    depth_range = z_bot - z_top
    if depth_range <= 0:
        return None

    px, py, pz = [], [], []

    z_step  = max(2.0, depth_range / max(int(depth_range / 2.5), 3))
    xy_step = hw * 0.5
    n_pts   = max(3, int(2 * hw / xy_step))
    t_vals  = [k / (n_pts - 1) for k in range(n_pts)]
    z_vals  = [z_top + z_step / 2 + k * z_step
               for k in range(int(depth_range / z_step) + 1)
               if z_top + z_step / 2 + k * z_step < z_bot]

    def fp(t, z):
        """Face-parameter (t ∈ [0,1], depth z) → 3-D point."""
        t = max(0.0, min(1.0, t))
        if face == "front":
            return cx - hw + t*2*hw, cy + hw, -z
        elif face == "back":
            return cx - hw + t*2*hw, cy - hw, -z
        elif face == "left":
            return cx - hw, cy - hw + t*2*hw, -z
        else:  # right
            return cx + hw, cy - hw + t*2*hw, -z

    # ── CLAY – horizontal dashes ─────────────────────────────────
    if pat_type == "lightGray":
        for z in z_vals:
            for t_centre in [0.2, 0.8]:
                x1, y1, z1 = fp(t_centre - 0.14, z)
                x2, y2, z2 = fp(t_centre + 0.14, z)
                px += [x1, x2, None]; py += [y1, y2, None]; pz += [z1, z2, None]

    # ── SAND – dense dot grid ─────────────────────────────────────
    elif pat_type == "darkGray":
        for z in z_vals:
            for t in t_vals:
                xp, yp, zp = fp(t, z)
                px.append(xp); py.append(yp); pz.append(zp)

    # ── SILT – sparse fine dots ───────────────────────────────────
    elif pat_type == "gray0625":
        for z in z_vals[::2]:
            for t in t_vals[::2]:
                xp, yp, zp = fp(t, z)
                px.append(xp); py.append(yp); pz.append(zp)

    # ── CALCARENITE – diagonal lines / ───────────────────────────
    elif pat_type == "lightUp":
        n_diag  = max(3, int(2 * hw / 4))
        offsets = [k / (n_diag - 1) for k in range(n_diag)]
        z_rng   = z_bot - z_top
        for off in offsets:
            x0, y0, _ = fp(0.0,  z_top + off * z_rng)
            x1, y1, _ = fp(off,  z_top)
            px += [x0, x1, None]; py += [y0, y1, None]
            pz += [-(z_top + off*z_rng), -z_top, None]
            x0b, y0b, _ = fp(1.0,       z_top + off * z_rng)
            x1b, y1b, _ = fp(1.0 - off, z_top)
            px += [x0b, x1b, None]; py += [y0b, y1b, None]
            pz += [-(z_top + off*z_rng), -z_top, None]

    # ── SANDSTONE – cross-hatch # ─────────────────────────────────
    elif pat_type == "lightTrellis":
        n_diag  = max(4, int(2 * hw / 3.5))
        offsets = [k / (n_diag - 1) for k in range(n_diag)]
        z_rng   = z_bot - z_top
        for off in offsets:
            x0, y0, _   = fp(0.0,       z_top + off * z_rng)
            x1, y1, _   = fp(off,       z_top)
            x0b, y0b, _ = fp(1.0,       z_top + off * z_rng)
            x1b, y1b, _ = fp(1.0 - off, z_top)
            px += [x0, x1, None, x0b, x1b, None]
            py += [y0, y1, None, y0b, y1b, None]
            pz += [-(z_top+off*z_rng), -z_top, None,
                   -(z_top+off*z_rng), -z_top, None]
            if off in offsets[::2]:
                z_bar       = z_top + off * z_rng
                xa, ya, _   = fp(0.05, z_bar)
                xb, yb, _   = fp(0.95, z_bar)
                px += [xa, xb, None]; py += [ya, yb, None]
                pz += [-z_bar, -z_bar, None]

    if not px:
        return None

    is_line = None in px
    trace   = {"type": "scatter3d", "x": px, "y": py, "z": pz,
                "mode": "lines" if is_line else "markers",
                "showlegend": False, "hoverinfo": "skip"}
    if is_line:
        trace["line"]   = {"color": "rgba(0,0,0,0.88)", "width": 2}
    else:
        trace["marker"] = {"color": "rgba(0,0,0,0.88)", "size": 3,
                           "symbol": "circle"}
    return trace


def pattern_traces_for_layer(cx, cy, z_top, z_bot, hw, pat_type):
    """Return pattern traces for all 4 vertical faces of one layer."""
    out = []
    for face in ("front", "back", "left", "right"):
        t = pattern_on_face(face, cx, cy, z_top, z_bot, hw, pat_type)
        if t:
            out.append(t)
    return out


# ══════════════════════════════════════════════════════════════════
# 6.  TRACE BUILDER
# ══════════════════════════════════════════════════════════════════

TEXTURE_OPACITY = {
    "lightGray":    0.82,
    "darkGray":     0.95,
    "lightUp":      0.72,
    "gray0625":     0.55,
    "lightTrellis": 0.65,
    "solid":        0.90,
}

TEXTURE_LABEL = {
    "lightGray":    "Fine / Light-grained",
    "darkGray":     "Dense / Coarse-grained",
    "lightUp":      "Diagonal / Weathered",
    "gray0625":     "Very Fine / Silty",
    "lightTrellis": "Cross-hatched / Cemented",
    "solid":        "Solid / Massive",
}


def build_traces(boreholes, index, nx, ny, hw):
    """Build the complete list of plotly trace dicts."""
    traces = []

    for bh_id, bh in boreholes.items():
        bx = nx(bh["x"]); by = ny(bh["y"]); wd = bh["water_depth"]

        # ── Water column ─────────────────────────────────────────
        if wd > 0:
            wx, wy, wz, wi, wj, wk = box_mesh(bx, by, -wd, 0, hw)
            traces.append({
                "type": "mesh3d", "x": wx, "y": wy, "z": wz,
                "i": wi, "j": wj, "k": wk,
                "color": "#5b9bd5", "opacity": 0.22, "flatshading": True,
                "lighting": {"ambient": 0.95, "diffuse": 0.3},
                "hovertemplate": (
                    f"<b>BH-{bh_id} — Water Column</b><br>"
                    f"Water Depth: {wd} m<br>"
                    f"Easting: {bh['x']} m E  |  Northing: {bh['y']} m N"
                    "<extra></extra>"
                ),
                "showlegend": False,
                "name": f"BH-{bh_id} water",
            })

        # ── Stratigraphic layers ──────────────────────────────────
        for layer in bh["layers"]:
            dt      = layer["depth_top"]; db = layer["depth_bot"]
            color   = layer["color"]
            texture = layer["texture"]   # patternType read directly from Excel
            strat   = layer["strat"]
            opacity = TEXTURE_OPACITY.get(texture, 0.9)
            # Derive a human-readable soil name from the texture pattern
            soil_name = TEXTURE_LABEL.get(texture, texture)

            x, y, z, ii, jj, kk = box_mesh(bx, by, dt, db, hw)
            traces.append({
                "type": "mesh3d", "x": x, "y": y, "z": z,
                "i": ii, "j": jj, "k": kk,
                "color": color, "opacity": opacity, "flatshading": True,
                "lighting": {"ambient": 0.72, "diffuse": 0.65,
                             "specular": 0.25, "roughness": 0.65},
                "lightposition": {"x": 300, "y": 400, "z": 600},
                "hovertemplate": (
                    f"<b>BH-{bh_id}</b><br>"
                    f"<b>Depth:</b> {dt} – {db} m  ({db-dt:.1f} m thick)<br>"
                    f"<b>Stratigraphy:</b> {strat}<br>"
                    f"<b>Soil type:</b> {soil_name}<br>"
                    f"<b>Texture pattern:</b> {texture}<br>"
                    f"<b>Fill colour:</b> {color}"
                    "<extra></extra>"
                ),
                "showlegend": False,
                "name": (strat or "")[:40],
            })

            # Apply texture overlay pattern directly from Excel fill
            if texture and texture != "solid":
                traces.extend(pattern_traces_for_layer(bx, by, dt, db, hw, texture))

        # ── Borehole spine ────────────────────────────────────────
        max_d = max((l["depth_bot"] for l in bh["layers"]), default=1)
        traces.append({
            "type": "scatter3d", "x": [bx, bx], "y": [by, by], "z": [0, -max_d],
            "mode": "lines", "line": {"color": "rgba(0,0,0,0.9)", "width": 4},
            "showlegend": False, "hoverinfo": "skip",
        })

        # ── BH label ─────────────────────────────────────────────
        traces.append({
            "type": "scatter3d", "x": [bx], "y": [by], "z": [6],
            "mode": "text+markers",
            "text": [f"<b>BH-{bh_id}</b>"],
            "textposition": "top center",
            "textfont": {"size": 16, "color": "white", "family": "Arial Black"},
            "marker": {"size": 7, "color": "#222",
                       "line": {"color": "white", "width": 2}},
            "showlegend": False,
            "hovertemplate": (
                f"<b>BH-{bh_id}</b><br>"
                f"Easting: {bh['x']} m E<br>"
                f"Northing: {bh['y']} m N<br>"
                f"Water depth: {wd} m<extra></extra>"
            ),
        })

    # ── Legend swatches — colours match my_output.xlsx exactly ──
    LEGEND = [
        ("#A8C5C5", "Clay — horizontal dashes"),
        ("#F0D890", "Sand — dense dot grid"),
        ("#C8B478", "Silt — sparse dots"),
        ("#E8D8B0", "Calcarenite — diagonal lines"),
        ("#D4975A", "Sandstone — cross-hatch"),
    ]
    for col, lbl in LEGEND:
        traces.append({
            "type": "scatter3d", "x": [None], "y": [None], "z": [None],
            "mode": "markers",
            "marker": {"size": 14, "color": col, "opacity": 0.85,
                       "symbol": "square", "line": {"color": "white", "width": 1}},
            "name": lbl, "showlegend": True, "legendgroup": "soiltype",
        })

    return traces


def depth_gridlines(boreholes, nx, ny, hw):
    """Return shallow scatter traces for depth reference lines."""
    all_x  = [bh["x"] for bh in boreholes.values()]
    all_y  = [bh["y"] for bh in boreholes.values()]
    xn_min = min(nx(v) for v in all_x) - hw * 2.2
    xn_max = max(nx(v) for v in all_x) + hw * 2.2
    yn_mid = (min(ny(v) for v in all_y) + max(ny(v) for v in all_y)) / 2
    traces = []
    max_depth = max(
        l["depth_bot"]
        for bh in boreholes.values()
        for l in bh["layers"]
    )
    step = 25
    marks = list(range(0, -int(max_depth) - step, -step))
    for dm in marks:
        traces.append({
            "type": "scatter3d",
            "x": [xn_min, xn_max], "y": [yn_mid, yn_mid], "z": [dm, dm],
            "mode": "lines+text",
            "line": {"color": "rgba(180,180,180,0.22)", "width": 1},
            "text": [f"  {abs(dm)} m", ""],
            "textposition": "middle right",
            "textfont": {"size": 9, "color": "rgba(180,180,180,0.55)"},
            "showlegend": False, "hoverinfo": "skip",
        })
    return traces


# ══════════════════════════════════════════════════════════════════
# 7.  LAYOUT
# ══════════════════════════════════════════════════════════════════

def build_layout(boreholes, title_suffix=""):
    max_depth = max(
        l["depth_bot"]
        for bh in boreholes.values()
        for l in bh["layers"]
    )
    step  = 25
    tvals = list(range(0, -int(max_depth) - step, -step))
    ttxt  = [str(abs(v)) for v in tvals]

    return {
        "title": {
            "text": (
                f"<b>3D Borehole Log Visualization</b>{title_suffix}"
                "<br><sup>Stratigraphic layers with indexed soil-type patterns  "
                "|  Hover for details  |  Drag to rotate</sup>"
            ),
            "x": 0.5, "y": 0.97, "xanchor": "center",
            "font": {"size": 21, "color": "white", "family": "Arial"},
        },
        "paper_bgcolor": "#0d1117",
        "scene": {
            "xaxis": {
                "title": {"text": "Easting (normalised)", "font": {"color": "#ccc", "size": 12}},
                "tickfont": {"color": "#999", "size": 9},
                "gridcolor": "rgba(120,120,120,0.18)",
                "zerolinecolor": "rgba(120,120,120,0.35)",
                "backgroundcolor": "rgba(12,22,36,0.85)",
            },
            "yaxis": {
                "title": {"text": "Northing (normalised)", "font": {"color": "#ccc", "size": 12}},
                "tickfont": {"color": "#999", "size": 9},
                "gridcolor": "rgba(120,120,120,0.18)",
                "zerolinecolor": "rgba(120,120,120,0.35)",
                "backgroundcolor": "rgba(12,22,36,0.85)",
            },
            "zaxis": {
                "title": {"text": "Depth (m)", "font": {"color": "#ccc", "size": 12}},
                "tickfont": {"color": "#999", "size": 9},
                "gridcolor": "rgba(120,120,120,0.18)",
                "zerolinecolor": "#5b9bd5",
                "backgroundcolor": "rgba(8,15,28,0.92)",
                "tickvals": tvals, "ticktext": ttxt,
            },
            "bgcolor": "#0d1117",
            "aspectmode": "manual",
            "aspectratio": {"x": 1.2, "y": 2.2, "z": 1.6},
            "camera": {
                "eye": {"x": 1.7, "y": 1.9, "z": 1.0},
                "up":  {"x": 0,   "y": 0,   "z": 1},
                "center": {"x": 0, "y": 0,  "z": -0.15},
            },
            "annotations": [{
                "showarrow": False, "x": 0, "y": 0, "z": 8,
                "text": "Seafloor (z = 0)",
                "font": {"size": 11, "color": "#5b9bd5"},
            }],
        },
        "legend": {
            "title": {"text": "<b>Soil Type Index</b>",
                      "font": {"color": "white", "size": 13}},
            "font": {"color": "#ddd", "size": 12},
            "bgcolor": "rgba(15,25,45,0.90)",
            "bordercolor": "rgba(80,110,150,0.6)",
            "borderwidth": 1,
            "x": 0.01, "y": 0.96,
            "xanchor": "left", "yanchor": "top",
        },
        "margin": {"l": 0, "r": 0, "t": 80, "b": 0},
        "width": 1400, "height": 880,
        "hoverlabel": {
            "bgcolor": "rgba(15,25,50,0.95)",
            "bordercolor": "rgba(90,140,200,0.8)",
            "font": {"color": "white", "size": 13, "family": "Arial"},
        },
        "modebar": {"bgcolor": "rgba(0,0,0,0)", "color": "#aaa",
                    "activecolor": "white"},
    }


# ══════════════════════════════════════════════════════════════════
# 8.  HTML GENERATOR
# ══════════════════════════════════════════════════════════════════

_PATTERN_LEGEND_HTML = """
  <div class="pat-row">
    <svg width="46" height="26" style="border-radius:4px;border:1px solid rgba(255,255,255,0.15)">
      <rect width="46" height="26" fill="#A8C5C5"/>
      <line x1="4" y1="8" x2="22" y2="8" stroke="black" stroke-width="1.8"/>
      <line x1="26" y1="8" x2="42" y2="8" stroke="black" stroke-width="1.8"/>
      <line x1="4" y1="18" x2="22" y2="18" stroke="black" stroke-width="1.8"/>
      <line x1="26" y1="18" x2="42" y2="18" stroke="black" stroke-width="1.8"/>
    </svg>
    <span class="pat-label"><b style="color:white">Clay</b> — horizontal dashes</span>
  </div>
  <div class="pat-row">
    <svg width="46" height="26" style="border-radius:4px;border:1px solid rgba(255,255,255,0.15)">
      <rect width="46" height="26" fill="#F0D890"/>
      <circle cx="8"  cy="7"  r="2.2" fill="black"/>
      <circle cx="18" cy="7"  r="2.2" fill="black"/>
      <circle cx="28" cy="7"  r="2.2" fill="black"/>
      <circle cx="38" cy="7"  r="2.2" fill="black"/>
      <circle cx="8"  cy="15" r="2.2" fill="black"/>
      <circle cx="18" cy="15" r="2.2" fill="black"/>
      <circle cx="28" cy="15" r="2.2" fill="black"/>
      <circle cx="38" cy="15" r="2.2" fill="black"/>
      <circle cx="8"  cy="22" r="2.2" fill="black"/>
      <circle cx="18" cy="22" r="2.2" fill="black"/>
      <circle cx="28" cy="22" r="2.2" fill="black"/>
      <circle cx="38" cy="22" r="2.2" fill="black"/>
    </svg>
    <span class="pat-label"><b style="color:white">Sand</b> — dense dot grid</span>
  </div>
  <div class="pat-row">
    <svg width="46" height="26" style="border-radius:4px;border:1px solid rgba(255,255,255,0.15)">
      <rect width="46" height="26" fill="#C8B478"/>
      <circle cx="12" cy="10" r="1.5" fill="black"/>
      <circle cx="34" cy="10" r="1.5" fill="black"/>
      <circle cx="12" cy="20" r="1.5" fill="black"/>
      <circle cx="34" cy="20" r="1.5" fill="black"/>
    </svg>
    <span class="pat-label"><b style="color:white">Silt</b> — sparse dots</span>
  </div>
  <div class="pat-row">
    <svg width="46" height="26" style="border-radius:4px;border:1px solid rgba(255,255,255,0.15)">
      <rect width="46" height="26" fill="#E8D8B0"/>
      <line x1="0" y1="26" x2="26" y2="0" stroke="black" stroke-width="1.8"/>
      <line x1="20" y1="26" x2="46" y2="0" stroke="black" stroke-width="1.8"/>
    </svg>
    <span class="pat-label"><b style="color:white">Calcarenite</b> — diagonal /</span>
  </div>
  <div class="pat-row">
    <svg width="46" height="26" style="border-radius:4px;border:1px solid rgba(255,255,255,0.15)">
      <rect width="46" height="26" fill="#D4975A"/>
      <line x1="0" y1="26" x2="26" y2="0" stroke="black" stroke-width="1.6"/>
      <line x1="20" y1="26" x2="46" y2="0" stroke="black" stroke-width="1.6"/>
      <line x1="26" y1="26" x2="0"  y2="0" stroke="black" stroke-width="1.6"/>
      <line x1="46" y1="26" x2="20" y2="0" stroke="black" stroke-width="1.6"/>
    </svg>
    <span class="pat-label"><b style="color:white">Sandstone</b> — cross-hatch #</span>
  </div>
"""


def bh_info_spans(boreholes):
    spans = []
    for bh_id, bh in sorted(boreholes.items()):
        max_d = max((l["depth_bot"] for l in bh["layers"]), default=0)
        spans.append(
            f'<span><b>BH-{bh_id}</b> '
            f'{bh["x"]:.0f} m E &middot; {bh["y"]:.0f} m N &middot; '
            f'WD {bh["water_depth"]} m &middot; EOB {max_d} m</span>'
        )
    return "\n  ".join(spans)


def build_html(traces, layout, boreholes):
    traces_json = json.dumps(traces)
    layout_json = json.dumps(layout)
    config_json = json.dumps({
        "displayModeBar": True,
        "scrollZoom": True,
        "modeBarButtonsToAdd": ["resetCameraDefault3d"],
        "displaylogo": False,
    })
    info_spans  = bh_info_spans(boreholes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>3D Borehole Log Visualizer</title>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{background:#0d1117;font-family:Arial,sans-serif;
          display:flex;flex-direction:column;align-items:center}}
    #controls{{width:100%;max-width:1400px;padding:14px 20px 6px;
               display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
    .btn{{background:rgba(40,60,90,.85);color:#cde;
          border:1px solid rgba(80,120,180,.5);padding:7px 16px;
          border-radius:6px;cursor:pointer;font-size:13px;transition:all .2s}}
    .btn:hover{{background:rgba(60,90,130,.9);color:#fff;border-color:#5b9bd5}}
    .hint{{color:#556;font-size:12px;margin-left:6px}}
    #pat-legend{{position:fixed;right:18px;top:90px;
                 background:rgba(10,18,32,.95);
                 border:1px solid rgba(80,110,150,.55);
                 border-radius:10px;padding:14px 16px;
                 z-index:100;min-width:220px}}
    #pat-legend h3{{color:#fff;font-size:13px;margin-bottom:10px;letter-spacing:.04em}}
    .pat-row{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
    .pat-label{{color:#ccc;font-size:12px}}
    #plot{{width:100%;max-width:1400px}}
    #info-bar{{width:100%;max-width:1400px;padding:8px 20px;
               color:#556;font-size:11px;
               border-top:1px solid rgba(50,70,100,.35);
               display:flex;gap:20px;flex-wrap:wrap}}
    #info-bar b{{color:#778}}
  </style>
</head>
<body>

<div id="controls">
  <button class="btn" onclick="resetCam()">⟳ Reset Camera</button>
  <button class="btn" onclick="toggleWater()">💧 Toggle Water</button>
  <button class="btn" onclick="topView()">⬆ Plan View</button>
  <button class="btn" onclick="sideView()">◀ Section View</button>
  <span class="hint">Drag → rotate &nbsp;·&nbsp; Scroll → zoom &nbsp;·&nbsp; Right-drag → pan &nbsp;·&nbsp; Hover → layer info</span>
</div>

<div id="pat-legend">
  <h3>📋 Soil Type Index</h3>
  {_PATTERN_LEGEND_HTML}
</div>

<div id="plot"></div>

<div id="info-bar">
  {info_spans}
</div>

<script>
  const traces = {traces_json};
  const layout = {layout_json};
  const config = {config_json};
  Plotly.newPlot('plot', traces, layout, config);

  function resetCam() {{
    Plotly.relayout('plot', {{'scene.camera': {{
      eye: {{x:1.7,y:1.9,z:1.0}},
      up:  {{x:0,y:0,z:1}},
      center: {{x:0,y:0,z:-0.15}}
    }}}});
  }}

  let waterOn = true;
  function toggleWater() {{
    const idx = traces.reduce((a,t,i)=>
      t.name&&t.name.includes('water')?[...a,i]:a, []);
    waterOn = !waterOn;
    idx.forEach(i=>Plotly.restyle('plot',
      {{visible:waterOn?true:'legendonly'}},[i]));
  }}

  function topView() {{
    Plotly.relayout('plot', {{'scene.camera': {{
      eye: {{x:0,y:0,z:3.5}},
      up:  {{x:0,y:1,z:0}},
      center: {{x:0,y:0,z:-0.3}}
    }}}});
  }}

  function sideView() {{
    Plotly.relayout('plot', {{'scene.camera': {{
      eye: {{x:3.0,y:0,z:0.3}},
      up:  {{x:0,y:0,z:1}},
      center: {{x:0,y:0,z:-0.3}}
    }}}});
  }}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════
# 9.  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    borehole_path = args.borehole
    index_path    = args.index

    # ── Validate inputs ───────────────────────────────────────────
    if not Path(borehole_path).is_file():
        sys.exit(f"[ERROR] Borehole file not found: {borehole_path}")

    if index_path and not Path(index_path).is_file():
        sys.exit(f"[ERROR] Index file not found: {index_path}")

    # ── Determine output path ─────────────────────────────────────
    if args.output:
        out_path = args.output
    else:
        stem     = Path(borehole_path).stem
        out_path = str(Path(borehole_path).parent / f"{stem}_3d.html")

    # ── Load data ─────────────────────────────────────────────────
    print(f"[1/4] Loading borehole data  →  {borehole_path}")
    boreholes = load_boreholes(borehole_path, args.sheet)
    print(f"      Found {len(boreholes)} boreholes, "
          f"{sum(len(b['layers']) for b in boreholes.values())} layers total.")

    if index_path:
        print(f"[2/4] Loading index  →  {index_path}")
        index = load_index(index_path, args.index_sheet)
        print(f"      Loaded {len(index)} index entries.")
    else:
        print("[2/4] No index file supplied — using built-in defaults.")
        index = BUILTIN_INDEX

    # ── Normalise coordinates ─────────────────────────────────────
    print("[3/4] Building 3-D traces …")
    nx_fn, ny_fn = build_normaliser(boreholes)
    traces = build_traces(boreholes, index, nx_fn, ny_fn, args.hw)
    traces += depth_gridlines(boreholes, nx_fn, ny_fn, args.hw)

    # ── Optional verbose summary ──────────────────────────────────
    if args.verbose:
        print("\n  Pattern matches per layer:")
        for bh_id, bh in boreholes.items():
            for l in bh["layers"]:
                pat, name = detect_pattern(l["strat"], index)
                print(f"    BH-{bh_id}  {l['depth_top']:6.1f}–{l['depth_bot']:6.1f} m"
                      f"  [{name or '—':14}]  {l['strat'][:60]}")
        print()

    # ── Layout ────────────────────────────────────────────────────
    title_suffix = f" — {Path(borehole_path).stem}"
    layout = build_layout(boreholes, title_suffix)

    # ── Write HTML ────────────────────────────────────────────────
    print(f"[4/4] Writing HTML  →  {out_path}")
    html = build_html(traces, layout, boreholes)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = len(html) / 1024
    print(f"\n✅  Done!  Output: {out_path}  ({size_kb:.1f} KB)")
    print("    Open the HTML file in any web browser to view the 3-D plot.")


if __name__ == "__main__":
    main()
