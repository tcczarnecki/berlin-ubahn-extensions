"""
Step 4 - S-Bahn network and interchange stations
================================================
Downloads from OpenStreetMap (cached in output/):
  - S-Bahn lines  -> layer "S-Bahn" (one neutral colour)
  - interchanges  -> layer "Umsteigebahnhöfe"

An interchange is a station where
  - two U-Bahn lines meet AND go different ways (crossing or branching);
    stations where two lines only share track don't count, or
  - a U-Bahn line meets the S-Bahn (platforms within TRANSFER_DIST_M).

Prints the number of interchanges per U-Bahn line.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsLineSymbol, QgsMarkerSymbol, QgsPointXY, QgsProject,
    QgsSingleSymbolRenderer, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

SBAHN_COLOUR = "#e8e8f0"
SBAHN_OPACITY = 0.6
TRANSFER_DIST_M = 250

PROJECT = QgsProject.instance()
ROOT = QgsProject.instance().homePath()
if not ROOT:
    raise RuntimeError("Save your QGIS project in the repository folder first "
                       "(e.g. berlin_ubahn.qgz), then run this again.")
DATA = os.path.join(ROOT, "data")       # inputs: CSV tables, census ZIP
OUT = os.path.join(ROOT, "output")      # everything the scripts generate
os.makedirs(OUT, exist_ok=True)
folder = OUT
CRS = QgsCoordinateReferenceSystem("EPSG:25833")
xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), CRS, PROJECT)

AREA = 'area["name"="Berlin"]["admin_level"="4"]->.b;'
S_ROUTES = 'relation["route"~"^(light_rail|train)$"]["ref"~"^S ?[0-9]+$"](area.b)'
U_ROUTES = 'relation["route"="subway"](area.b)'


def overpass(query, cache_name, must_have=1):
    cache = os.path.join(folder, cache_name)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        if len(data.get("elements", [])) >= must_have:
            print(f"    using saved {cache_name}")
            return data
    for k in range(5):
        try:
            req = urllib.request.Request(
                "https://overpass-api.de/api/interpreter",
                data=urllib.parse.urlencode({"data": query}).encode(),
                headers={"User-Agent": "berlin-ubahn-extensions-analysis"})
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.loads(r.read())
            if len(data.get("elements", [])) < must_have:
                raise RuntimeError("empty answer")
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return data
        except Exception as e:
            print(f"    attempt {k + 1} failed ({e}) - retrying ...")
            time.sleep(6 * (k + 1))
    raise RuntimeError("OpenStreetMap download failed - try again in a few minutes "
                       "and send Claude the messages above.")


def save(layer, filename):
    for old in [l for l in PROJECT.mapLayers().values() if l.name() == layer.name()]:
        PROJECT.removeMapLayer(old.id())
    path = os.path.join(folder, filename)
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            n += 1
            path = f"{stem}_{n}{ext}"
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, PROJECT.transformContext(), opts)
    return QgsVectorLayer(path, layer.name(), "ogr")


def clean(name):
    name = re.sub(r"^(S\+U|U|S)\s+", "", name or "")
    return re.sub(r"\s*\(Berlin\)$", "", name).strip()


def norm_ref(ref):
    return (ref or "?").replace(" ", "")


# ================================================================ S-Bahn lines
print("S-Bahn lines ...")
sb = overpass(f"[out:json][timeout:180];{AREA}{S_ROUTES};out geom;",
              "osm_sbahn_lines_v2.json")
mem = QgsVectorLayer("LineString?crs=EPSG:25833&field=line:string", "S-Bahn", "memory")
seen, feats, s_lines = set(), [], set()
for rel in sb["elements"]:
    s_lines.add(norm_ref(rel.get("tags", {}).get("ref")))
    for m in rel.get("members", []):
        if m.get("type") != "way" or "geometry" not in m or m["ref"] in seen:
            continue
        seen.add(m["ref"])
        pts = [xform.transform(QgsPointXY(p["lon"], p["lat"])) for p in m["geometry"]]
        if len(pts) > 1:
            f = QgsFeature(mem.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY(pts))
            f.setAttributes([norm_ref(rel.get("tags", {}).get("ref"))])
            feats.append(f)
mem.dataProvider().addFeatures(feats)
sbahn = save(mem, "sbahn_osm.gpkg")
sbahn.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
    {"width": "0.9", "color": SBAHN_COLOUR, "capstyle": "round", "joinstyle": "round"})))
sbahn.setOpacity(SBAHN_OPACITY)
print(f"OK  S-Bahn: {len(feats)} track pieces, lines "
      f"{', '.join(sorted(s_lines, key=lambda r: (len(r), r)))}")

# ======================================= routes in order + their stop nodes
print("Stations ...")
st = overpass(f"[out:json][timeout:180];{AREA}({U_ROUTES};{S_ROUTES};)->.all;"
              ".all out body;node(r.all);out;", "osm_routes_stops_v2.json")
nodes = {el["id"]: el for el in st["elements"] if el["type"] == "node"}

u_neighbours = {}       # line -> station -> set of neighbouring stations
u_pos = {}              # station -> list of points (U platforms)
u_lines_at = {}         # station -> set of U lines
s_stops = []            # (point, S line)
for rel in (el for el in st["elements"] if el["type"] == "relation"):
    tags = rel.get("tags", {})
    is_u = tags.get("route") == "subway"
    ref = norm_ref(tags.get("ref"))
    seq = []
    for m in rel.get("members", []):
        if m["type"] != "node" or not m.get("role", "").startswith("stop"):
            continue
        n = nodes.get(m["ref"])
        if not n:
            continue
        p = xform.transform(QgsPointXY(n["lon"], n["lat"]))
        name = clean(n.get("tags", {}).get("name"))
        if is_u:
            if name and (not seq or seq[-1] != name):
                seq.append(name)
            u_pos.setdefault(name, []).append(p)
            u_lines_at.setdefault(name, set()).add(ref)
        else:
            s_stops.append((p, ref))
    if is_u:
        nb = u_neighbours.setdefault(ref, {})
        for i, s in enumerate(seq):
            nb.setdefault(s, set()).update(
                x for x in (seq[i - 1] if i else None,
                            seq[i + 1] if i + 1 < len(seq) else None) if x)

# ================================================ find the real interchanges
lay = QgsVectorLayer("Point?crs=EPSG:25833", "Umsteigebahnhöfe", "memory")
lay.dataProvider().addAttributes([QgsField("name", QVariant.String),
                                  QgsField("u_lines", QVariant.String),
                                  QgsField("s_lines", QVariant.String),
                                  QgsField("n_u", QVariant.Int),
                                  QgsField("to_sbahn", QVariant.Int)])
lay.updateFields()
feats, per_line = [], {}
for name, lines in u_lines_at.items():
    pts = u_pos[name]
    centre = QgsPointXY(sum(p.x() for p in pts) / len(pts), sum(p.y() for p in pts) / len(pts))
    # U-U: lines whose neighbours here differ from at least one other line's
    crossing = set()
    lst = sorted(lines)
    for i, a in enumerate(lst):
        for b in lst[i + 1:]:
            na = u_neighbours.get(a, {}).get(name, set())
            nb = u_neighbours.get(b, {}).get(name, set())
            if na != nb:
                crossing |= {a, b}
    # U-S: S-Bahn platforms close by
    s_here = {ref for p, ref in s_stops if p.distance(centre) < TRANSFER_DIST_M}
    if not crossing and not s_here:
        continue
    f = QgsFeature(lay.fields())
    f.setGeometry(QgsGeometry.fromPointXY(centre))
    f.setAttributes([name, " ".join(lst), " ".join(sorted(s_here)),
                     len(lines), 1 if s_here else 0])
    feats.append(f)
    for line in lines:
        d = per_line.setdefault(line, {"U": 0, "S": 0})
        d["U"] += line in crossing
        d["S"] += bool(s_here)
lay.dataProvider().addFeatures(feats)
inter = save(lay, "umsteigebahnhoefe.gpkg")
inter.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
    {"name": "circle", "size": "2.6", "color": "#ffffff", "outline_color": "#000000",
     "outline_width": "0.7"})))
inter.setOpacity(1.0)

print(f"OK  {len(feats)} interchange stations")
print("    line | to other U-lines | to S-Bahn")
for line in sorted(per_line, key=lambda l: (len(l), l)):
    d = per_line[line]
    print(f"    {line:<4} | {d['U']:>16} | {d['S']:>9}")
print("    check: Alexanderplatz ->",
      next((f"{f['u_lines']} + S {f['s_lines']}" for f in feats if f["name"] == "Alexanderplatz"),
           "not found"))

root = PROJECT.layerTreeRoot()
for layer in (inter, sbahn):
    PROJECT.addMapLayer(layer, False)
ub_idx = next((i for i, n in enumerate(root.children()) if n.name().startswith("U-Bahn")), 0)
root.insertLayer(ub_idx + 1, sbahn)
root.insertLayer(0, inter)
PROJECT.write()
print("Done - S-Bahn and interchanges added, project saved.")
