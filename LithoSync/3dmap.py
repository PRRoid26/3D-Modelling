import pandas as pd
import numpy as np
import re
import plotly.graph_objects as go

file = "boreholeLog.xlsx"
df = pd.read_excel(file)

# --------------------------
# PARSE FUNCTIONS
# --------------------------

def parse_coordinates(coord):
    e = float(re.search(r'([\d\.]+)\s*m\s*E', str(coord)).group(1))
    n = float(re.search(r'([\d\.]+)\s*m\s*N', str(coord)).group(1))
    return e, n


def parse_depth(depth):
    nums = re.findall(r'[\d\.]+', str(depth))
    return float(nums[0]), float(nums[1])


def extract_colour(text):

    colours = [
        "dark greenish grey",
        "greenish grey",
        "dark grey",
        "grey",
        "brown",
        "black"
    ]

    for c in colours:
        if c in str(text).lower():
            return c

    return "grey"


colour_map = {
    "dark greenish grey": "#3f4f4f",
    "greenish grey": "#6f8f8f",
    "dark grey": "#444444",
    "grey": "#888888",
    "brown": "#8b5a2b",
    "black": "#111111"
}

# --------------------------
# PARSE BOREHOLES
# --------------------------

boreholes = []
current = None

for _, row in df.iterrows():

    if not pd.isna(row["Sr. No."]):

        if current:
            boreholes.append(current)

        e, n = parse_coordinates(row["Co-ordinates"])

        current = {
            "x": e,
            "y": n,
            "layers": []
        }

    if not pd.isna(row["Depth"]):

        top, bottom = parse_depth(row["Depth"])
        colour = extract_colour(row["Stratigraphy"])

        current["layers"].append({
            "top": top,
            "bottom": bottom,
            "colour": colour
        })

if current:
    boreholes.append(current)

# --------------------------
# NORMALIZE COORDINATES
# --------------------------

xs = [b["x"] for b in boreholes]
ys = [b["y"] for b in boreholes]

x0 = min(xs)
y0 = min(ys)

for b in boreholes:
    b["x"] -= x0
    b["y"] -= y0

# --------------------------
# CYLINDER FUNCTION
# --------------------------

def create_cylinder(x, y, z0, z1, r, color):

    theta = np.linspace(0, 2*np.pi, 50)

    xs = x + r*np.cos(theta)
    ys = y + r*np.sin(theta)

    X = np.vstack([xs, xs])
    Y = np.vstack([ys, ys])
    Z = np.vstack([np.ones_like(xs)*z0, np.ones_like(xs)*z1])

    return go.Surface(
        x=X,
        y=Y,
        z=Z,
        colorscale=[[0,color],[1,color]],
        showscale=False,
        opacity=1
    )

# --------------------------
# BUILD FIGURE
# --------------------------

fig = go.Figure()

radius = 120   # MUCH THICKER

for bh in boreholes:

    x = bh["x"]
    y = bh["y"]

    for layer in bh["layers"]:

        z0 = -layer["top"]
        z1 = -layer["bottom"]

        fig.add_trace(
            create_cylinder(
                x,
                y,
                z0,
                z1,
                radius,
                colour_map[layer["colour"]]
            )
        )

        # layer boundary line
        fig.add_trace(go.Scatter3d(
            x=[x],
            y=[y],
            z=[z1],
            mode='markers',
            marker=dict(size=5,color="black"),
            showlegend=False
        ))

# --------------------------
# LAYOUT
# --------------------------

fig.update_layout(

    title="3D Borehole Stratigraphy",

    scene=dict(

        xaxis_title="Relative Easting",
        yaxis_title="Relative Northing",
        zaxis_title="Depth (m)",

        zaxis=dict(autorange="reversed"),

        aspectratio=dict(x=1,y=1,z=2)
    ),

    height=900
)

fig.show()