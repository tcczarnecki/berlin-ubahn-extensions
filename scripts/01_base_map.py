"""
Step 1 - Base map
=================
Starts from a CLEAN QGIS project and builds the base layers locally:
  1. Removes all layers currently in the project
  2. Downloads the newest population-density blocks (Umweltatlas, block level)
     from Berlin's geoportal in chunks with retries -> output/einwohnerdichte.gpkg
  3. Downloads the U-Bahn lines from OpenStreetMap  -> output/ubahn_osm.gpkg
  4. Styles: population density, water / forest / parks, U-Bahn in line colours
  5. Saves the project

Run from the QGIS Python console editor. The first run downloads ~26,000
blocks and takes a few minutes; later runs reuse output/einwohnerdichte.gpkg.
"""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import processing
from qgis.core import (
    QgsClassificationQuantile, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsFeature, QgsFillSymbol, QgsGeometry,
    QgsGraduatedSymbolRenderer, QgsLineSymbol, QgsPointXY, QgsProject,
    QgsProperty, QgsRuleBasedRenderer, QgsSingleSymbolRenderer, QgsStyle,
    QgsSymbolLayer, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor
from qgis.utils import iface

PROJECT = QgsProject.instance()
CRS = QgsCoordinateReferenceSystem("EPSG:25833")
BG = QColor("#0b0b0f")

ROOT = QgsProject.instance().homePath()
if not ROOT:
    raise RuntimeError("Save your QGIS project in the repository folder first "
                       "(e.g. berlin_ubahn.qgz), then run this again.")
DATA = os.path.join(ROOT, "data")       # inputs: CSV tables, census ZIP
OUT = os.path.join(ROOT, "output")      # everything the scripts generate
os.makedirs(OUT, exist_ok=True)
folder = OUT
tmp = os.path.join(folder, "_download_tmp")
os.makedirs(tmp, exist_ok=True)
print("Project folder:", folder)


def fetch(url, data=None, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"User-Agent": "berlin-ubahn-extensions-analysis"})
            with urllib.request.urlopen(req, timeout=240) as r:
                return r.read()
        except Exception as e:
            print(f"    attempt {k + 1} failed ({e}) - retrying ...")
            time.sleep(4 * (k + 1))
    raise RuntimeError("Download failed several times: " + url[:120])


def free_path(path):
    """Delete an old file; if Windows has it locked, use a new file name."""
    stem, ext = os.path.splitext(path)
    candidate, n = path, 1
    while os.path.exists(candidate):
        try:
            os.remove(candidate)
        except PermissionError:
            n += 1
            candidate = f"{stem}_{n}{ext}"
    return candidate


def save_layer(layer, path):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, PROJECT.transformContext(), opts)
    if res[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError("Saving failed: " + str(res[1]))


# ------------------------------------------------------------ 1. clean up
PROJECT.removeAllMapLayers()
PROJECT.setCrs(CRS)
PROJECT.setBackgroundColor(BG)
iface.mapCanvas().setCanvasColor(BG)
print("OK  project cleared")


# --------------------------------------------- 2. population density blocks
def find_service():
    for year in range(2026, 2008, -1):
        base = f"https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_{year}"
        try:
            xml = fetch(base + "?SERVICE=WFS&REQUEST=GetCapabilities", tries=1)
        except RuntimeError:
            continue
        root = ET.fromstring(xml)
        for el in root.iter():
            if el.tag.endswith("FeatureType"):
                for child in el:
                    if child.tag.endswith("Name"):
                        return year, base, child.text
    raise RuntimeError("No population-density service found.")


year, base, typename = find_service()
density_path = os.path.join(folder, "einwohnerdichte.gpkg")
existing = QgsVectorLayer(density_path, "check", "ogr") if os.path.exists(density_path) else None
if existing is not None and existing.isValid() and existing.featureCount() > 20000:
    print(f"OK  reusing einwohnerdichte.gpkg ({existing.featureCount()} blocks, "
          "delete the file to download again)")
    start, pages = existing.featureCount(), None
else:
    print(f"... downloading population density {year} in chunks")
    start, pages = 0, []
del existing
PAGE = 4000
last_hash = None
while pages is not None and start < 300000:
    params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
              "TYPENAMES": typename, "COUNT": PAGE, "STARTINDEX": start,
              "SRSNAME": "urn:ogc:def:crs:EPSG::25833"}
    data = fetch(base + "?" + urllib.parse.urlencode(params))
    h = hashlib.md5(data).hexdigest()
    if h == last_hash:            # server ignores paging -> stop
        break
    last_hash = h
    path = os.path.join(tmp, f"page_{start:06d}.gml")
    with open(path, "wb") as f:
        f.write(data)
    lyr = QgsVectorLayer(path, "page", "ogr")
    n = lyr.featureCount() if lyr.isValid() else 0
    if n <= 0:
        if start == 0:
            raise RuntimeError("Server returned no blocks. Start of answer: "
                               + data[:300].decode("utf-8", "replace"))
        break
    pages.append(path)
    start += n
    print(f"    {start} blocks so far")

if pages is not None:
    density_path = free_path(density_path)
    processing.run("native:mergevectorlayers",
                   {"LAYERS": pages, "CRS": CRS, "OUTPUT": density_path})
    print(f"OK  {start} blocks saved to {os.path.basename(density_path)}")


# --------------------------------------------------------- 3. U-Bahn lines
print("... downloading U-Bahn lines from OpenStreetMap")
query = """
[out:json][timeout:90];
area["name"="Berlin"]["admin_level"="4"]->.berlin;
relation["route"="subway"](area.berlin);
out geom;
"""
osm = json.loads(fetch("https://overpass-api.de/api/interpreter",
                       data=urllib.parse.urlencode({"data": query}).encode()))
mem = QgsVectorLayer("LineString?crs=EPSG:25833&field=line:string&field=colour:string",
                     "U-Bahn", "memory")
xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), CRS, PROJECT)
seen, feats = set(), []
for rel in osm.get("elements", []):
    ref = rel.get("tags", {}).get("ref", "?")
    colour = rel.get("tags", {}).get("colour", "#ffffff")
    for m in rel.get("members", []):
        if m.get("type") != "way" or "geometry" not in m or (ref, m["ref"]) in seen:
            continue
        seen.add((ref, m["ref"]))
        pts = [xform.transform(QgsPointXY(p["lon"], p["lat"])) for p in m["geometry"]]
        if len(pts) >= 2:
            f = QgsFeature(mem.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY(pts))
            f.setAttributes([ref, colour])
            feats.append(f)
mem.dataProvider().addFeatures(feats)
ubahn_path = free_path(os.path.join(folder, "ubahn_osm.gpkg"))
save_layer(mem, ubahn_path)
print(f"OK  U-Bahn saved to {os.path.basename(ubahn_path)} ({len(feats)} segments, lines: "
      f"{', '.join(sorted({f['line'] for f in feats}))})")


# ------------------------------------------------------- 4. layers + styles
def fields_of(layer):
    return [f.name() for f in layer.fields()]


# 4a. water + green (bottom layer)
land = QgsVectorLayer(density_path, "Landschaft (Wasser/Grün)", "ogr")
type_fields = [f for f in fields_of(land) if f.lower() in ("typklar", "etypklar")]
CATEGORIES = [
    ("Wasser",         ["gewässer", "wasser", "see", "fluss", "kanal", "hafen"], "#4d7c8f"),
    ("Wald",           ["wald", "forst"],                                        "#1d4a2a"),
    ("Grünflächen",    ["park", "grün", "friedhof", "kleingarten", "wiese",
                        "sport", "baumschule", "gärtnerei", "camping"],          "#2f6b3a"),
    ("Landwirtschaft", ["landwirtschaft", "acker", "feld"],                      "#4a5a2a"),
]
if type_fields:
    text = ("lower(" + " || ' ' || ".join(f"coalesce(\"{f}\", '')" for f in type_fields)
            + ")")
    root_rule = QgsRuleBasedRenderer.Rule(None)
    earlier = []
    for label, words, colour in CATEGORIES:
        match = "(" + " OR ".join(f"{text} LIKE '%{w}%'" for w in words) + ")"
        expr = match + "".join(f" AND NOT {e}" for e in earlier)
        earlier.append(match)
        sym = QgsFillSymbol.createSimple({"color": colour, "outline_style": "no"})
        root_rule.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, label))
    land.setRenderer(QgsRuleBasedRenderer(root_rule))
    PROJECT.addMapLayer(land)

    def category_of(v):
        v = (v or "").lower()
        return next((lab for lab, words, _ in CATEGORIES
                     if any(w in v for w in words)), "-")

    print("Land-use types -> colour:")
    for fld in type_fields:
        vals = sorted(str(v) for v in land.uniqueValues(land.fields().indexOf(fld))
                      if v not in (None, ""))
        print(f"  field '{fld}':")
        for v in vals[:60]:
            print(f"     {category_of(v):<15} {v}")
else:
    print("!!  no land-use field found - water/green skipped")

# 4b. population density (middle layer)
density = QgsVectorLayer(density_path, f"Einwohnerdichte {year} (EW/ha)", "ogr")
dfield = next((f for f in fields_of(density) if f.lower().startswith("ew_ha")), None)
if dfield:
    r = QgsGraduatedSymbolRenderer(
        f'CASE WHEN to_real("{dfield}") > 0 THEN to_real("{dfield}") END')
    r.setSourceSymbol(QgsFillSymbol.createSimple({"outline_style": "no"}))
    r.setClassificationMethod(QgsClassificationQuantile())
    r.updateClasses(density, 7)
    r.updateColorRamp(QgsStyle().defaultStyle().colorRamp("Magma"))
    density.setRenderer(r)
    density.setOpacity(0.9)
else:
    print("!!  density field not found. Fields:", fields_of(density))
PROJECT.addMapLayer(density)

# 4c. U-Bahn (top layer)
ubahn = QgsVectorLayer(ubahn_path, "U-Bahn (OSM)", "ogr")
sym = QgsLineSymbol.createSimple({"width": "1.2", "capstyle": "round",
                                  "joinstyle": "round"})
sym.symbolLayer(0).setDataDefinedProperty(
    QgsSymbolLayer.PropertyStrokeColor,
    QgsProperty.fromExpression("coalesce(\"colour\", '#ffffff')"))
casing = QgsLineSymbol.createSimple({"width": "1.7", "color": "0,0,0,230",
                                     "capstyle": "round", "joinstyle": "round"})
sym.insertSymbolLayer(0, casing.symbolLayer(0).clone())   # black edge underneath
ubahn.setRenderer(QgsSingleSymbolRenderer(sym))
PROJECT.addMapLayer(ubahn)


# ---------------------------------------------------------------- 5. save
iface.mapCanvas().setExtent(density.extent())
iface.mapCanvas().refresh()
if not PROJECT.fileName():
    PROJECT.setFileName(os.path.join(ROOT, "berlin_ubahn.qgz"))
PROJECT.write()
print("Done! Project saved as", PROJECT.fileName())
print("(You can delete the _download_tmp folder.)")
