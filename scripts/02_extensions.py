"""
Step 2 - Proposed U-Bahn extensions
===================================
Reads data/extensions.csv and builds two layers:
  - "Erweiterungen (Linien)"    one line per extension, drawn station to station
  - "Erweiterungen (Stationen)" planned stations (white = new, grey = existing terminus)

Line style shows the status:
  planning -> solid      (in planning, e.g. U3, U8)
  study    -> dashed     (feasibility study done, e.g. U7 BER, U2)
  vision   -> dotted     (BVG vision network)
  stopped  -> grey dash  (officially dropped, e.g. U7 Heerstraße Nord)

Editing: one row per station, in order (seq 1 = existing terminus, new = 0).
Lines are straight between stations - a schematic, not the tunnel alignment.
"""

import csv
import os

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsLineSymbol,
    QgsMarkerSymbol, QgsPointXY, QgsProject, QgsProperty, QgsRendererCategory,
    QgsSimpleLineSymbolLayer, QgsSymbolLayer, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtGui import QColor

# Qt5 / Qt6 compatible names (your QGIS uses Qt6)
ROUND_CAP = getattr(Qt, "PenCapStyle", Qt).RoundCap
ROUND_JOIN = getattr(Qt, "PenJoinStyle", Qt).RoundJoin

PROJECT = QgsProject.instance()
ROOT = QgsProject.instance().homePath()
if not ROOT:
    raise RuntimeError("Save your QGIS project in the repository folder first "
                       "(e.g. berlin_ubahn.qgz), then run this again.")
DATA = os.path.join(ROOT, "data")       # inputs: CSV tables, census ZIP
OUT = os.path.join(ROOT, "output")      # everything the scripts generate
os.makedirs(OUT, exist_ok=True)
folder = OUT
CSV = os.path.join(DATA, "extensions.csv")
if not os.path.exists(CSV):
    raise RuntimeError("data/extensions.csv not found in " + ROOT)

CRS = QgsCoordinateReferenceSystem("EPSG:25833")
xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), CRS, PROJECT)

# line colours from the U-Bahn layer (fallback: BVG colours)
COLOURS = {"U1": "#7DAD4C", "U2": "#DA421E", "U3": "#16683D", "U4": "#F0D722",
           "U5": "#7E5330", "U6": "#8C6DAB", "U7": "#528DBA", "U8": "#224F86",
           "U9": "#F3791D"}
ub = next((l for l in PROJECT.mapLayers().values() if l.name().startswith("U-Bahn")), None)
if ub is not None:
    for f in ub.getFeatures():
        if f["colour"]:
            COLOURS[f["line"]] = f["colour"]

# ------------------------------------------------------------ read table
rows = []
with open(CSV, encoding="utf-8-sig", newline="") as fh:
    sample = fh.read(2048)
    fh.seek(0)
    dialect = csv.Sniffer().sniff(sample, delimiters=",;")   # Excel may save with ;
    for r in csv.DictReader(fh, dialect=dialect):
        rows.append(r)
routes = {}
for r in rows:
    routes.setdefault(r["route"], []).append(r)

def num(v):
    return float(str(v).replace(",", "."))

# ------------------------------------------------------------ build layers
lines = QgsVectorLayer("LineString?crs=EPSG:25833", "Erweiterungen (Linien)", "memory")
pts = QgsVectorLayer("Point?crs=EPSG:25833", "Erweiterungen (Stationen)", "memory")
lines.dataProvider().addAttributes([QgsField(n, QVariant.String) for n in
                                    ("route", "line", "status", "colour")])
pts.dataProvider().addAttributes([QgsField(n, QVariant.String) for n in
                                  ("route", "line", "status", "station")]
                                 + [QgsField("new", QVariant.Int)])
lines.updateFields()
pts.updateFields()

line_feats, pt_feats = [], []
for route, stops in routes.items():
    stops.sort(key=lambda r: int(r["seq"]))
    xy = [xform.transform(QgsPointXY(num(s["lon"]), num(s["lat"]))) for s in stops]
    line, status = stops[0]["line"], stops[0]["status"].strip().lower()
    f = QgsFeature(lines.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY(xy))
    f.setAttributes([route, line, status, COLOURS.get(line, "#ffffff")])
    line_feats.append(f)
    for s, p in zip(stops, xy):
        g = QgsFeature(pts.fields())
        g.setGeometry(QgsGeometry.fromPointXY(p))
        g.setAttributes([route, line, status, s["station"], int(s["new"])])
        pt_feats.append(g)
    print(f"OK  {route}: {len(stops) - 1} new stations ({status})")
lines.dataProvider().addFeatures(line_feats)
pts.dataProvider().addFeatures(pt_feats)

# ------------------------------------------------------------ save locally
def save(layer, name):
    for old in [l for l in PROJECT.mapLayers().values() if l.name() == layer.name()]:
        PROJECT.removeMapLayer(old.id())
    path = os.path.join(folder, name)
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):              # old file: delete, or new name if locked
        try:
            os.remove(path)
        except PermissionError:
            n += 1
            path = f"{stem}_{n}{ext}"
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, PROJECT.transformContext(), opts)
    print(f"    saved {os.path.basename(path)}")
    return QgsVectorLayer(path, layer.name(), "ogr")

lines = save(lines, "erweiterungen_linien.gpkg")
pts = save(pts, "erweiterungen_stationen.gpkg")

# ------------------------------------------------------------ styling
# every symbol part is built as its own object (no temporary objects -
# those can crash QGIS when Python throws them away too early)
def line_part(width, colour=None, colour_expr=None, dash=None):
    part = QgsSimpleLineSymbolLayer()
    part.setWidth(width)
    part.setPenCapStyle(ROUND_CAP)
    part.setPenJoinStyle(ROUND_JOIN)
    if colour:
        part.setColor(QColor(colour))
    if colour_expr:
        part.setDataDefinedProperty(QgsSymbolLayer.PropertyStrokeColor,
                                    QgsProperty.fromExpression(colour_expr))
    if dash:
        part.setUseCustomDashPattern(True)
        part.setCustomDashVector([float(x) for x in dash.split(";")])
    return part

DASH = {"planning": None, "study": "4;2", "vision": "0.6;1.6", "stopped": "3;2"}
cats = []
for status, dash in DASH.items():
    colour_expr = "'#777780'" if status == "stopped" else '"colour"'
    edge = line_part(2.0, colour="#000000", dash=dash)       # black edge
    core = line_part(1.4, colour_expr=colour_expr, dash=dash)
    sym = QgsLineSymbol([edge, core])
    cats.append(QgsRendererCategory(status, sym, status))
lines.setRenderer(QgsCategorizedSymbolRenderer("status", cats))
print("    lines styled")

new_sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "2.4", "color": "#ffffff",
                                        "outline_color": "#000000", "outline_width": "0.5"})
old_sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "1.6", "color": "#bbbbbb",
                                        "outline_color": "#000000", "outline_width": "0.4"})
pts.setRenderer(QgsCategorizedSymbolRenderer("new", [
    QgsRendererCategory(1, new_sym, "new station"),
    QgsRendererCategory(0, old_sym, "existing terminus")]))
print("    stations styled")

PROJECT.addMapLayer(lines)
PROJECT.addMapLayer(pts)
PROJECT.write()
print("Done - layers saved and project saved.")
