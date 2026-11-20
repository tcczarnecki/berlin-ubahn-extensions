"""
Step 5 - Population around Berlin (Zensus 2022, optional)
=========================================================
Adds the population of the surrounding area from the 2022 census 100 m grid.
A 100 m cell is exactly 1 hectare, so residents per cell = residents/ha,
the same unit (and colours) as the Berlin layer.

Before running: download "Bevölkerungszahlen in Gitterzellen" (ZIP, ~18 MB)
from zensus2022.de and put the ZIP (no need to unpack) into data/.

What it does:
  - keeps populated 100 m cells within MARGIN_KM of Berlin
  - leaves out cells inside Berlin (the detailed block data is used there)
  - adds "Umland Einwohner (Zensus 2022)" and a "Stadtgrenze" outline
Needed for extensions that leave Berlin (e.g. U7 to BER via Schönefeld).
"""

import csv
import glob
import io
import os
import zipfile

import processing
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsFeatureRequest, QgsField, QgsFillSymbol, QgsGeometry, QgsPointXY,
    QgsProject, QgsRectangle, QgsSingleSymbolRenderer, QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

MARGIN_KM = 25          # how far beyond the city boundary to include

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
LAEA = QgsCoordinateReferenceSystem("EPSG:3035")      # the census grid's system
to_laea = QgsCoordinateTransform(CRS, LAEA, PROJECT)
to_utm = QgsCoordinateTransform(LAEA, CRS, PROJECT)

density_src = next((l for l in PROJECT.mapLayers().values()
                    if l.name().startswith("Einwohnerdichte")), None)
if density_src is None:
    raise RuntimeError("Run 01_setup_all.py first.")


def save(layer, filename, name):
    for old in [l for l in PROJECT.mapLayers().values() if l.name() == name]:
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
    return QgsVectorLayer(path, name, "ogr")


# ---------------------------------------------------- 1. Berlin outline (cached)
mask_path = os.path.join(folder, "berlin_umriss.gpkg")
_old_mask = QgsVectorLayer(mask_path, "check", "ogr") if os.path.exists(mask_path) else None
if _old_mask is not None and (not _old_mask.isValid() or _old_mask.featureCount() == 0):
    print("    old Berlin outline file is broken - rebuilding it")
    del _old_mask
    os.remove(mask_path)
    _old_mask = None
if _old_mask is None:
    print("Building the Berlin outline (once, takes a few minutes) ...")
    fixed = processing.run("native:fixgeometries", {
        "INPUT": density_src, "OUTPUT": "memory:"})["OUTPUT"]
    print("    geometries checked, merging blocks ...")
    dissolved = processing.run("native:dissolve", {
        "INPUT": fixed, "FIELD": [], "OUTPUT": "memory:"})["OUTPUT"]
    print("    merged, closing street gaps ...")
    processing.run("native:buffer", {          # closes the gaps where streets are
        "INPUT": dissolved, "DISTANCE": 30, "SEGMENTS": 4, "DISSOLVE": True,
        "OUTPUT": mask_path})
mask_layer = QgsVectorLayer(mask_path, "mask", "ogr")
berlin = QgsGeometry.unaryUnion([f.geometry() for f in mask_layer.getFeatures()])
engine = QgsGeometry.createGeometryEngine(berlin.constGet())
engine.prepareGeometry()
print("OK  Berlin outline")

# ---------------------------------------------------- 2. read the census grid
zips = [z for z in glob.glob(os.path.join(DATA, "*.zip"))
        if "bev" in os.path.basename(z).lower() or "gitter" in os.path.basename(z).lower()]
if not zips:
    raise RuntimeError("Census ZIP not found in " + DATA + " - see the top of this file.")
zf = zipfile.ZipFile(zips[0])
member = next((m for m in zf.namelist() if "100m" in m.lower() and m.lower().endswith(".csv")),
              None)
if member is None:
    raise RuntimeError("No 100 m CSV inside the ZIP. Contents: " + ", ".join(zf.namelist()))
print("Reading", member, "...")

ext = density_src.extent()
ext.grow(MARGIN_KM * 1000)
corners = [to_laea.transform(QgsPointXY(x, y)) for x in (ext.xMinimum(), ext.xMaximum())
           for y in (ext.yMinimum(), ext.yMaximum())]
xmin, xmax = min(p.x() for p in corners), max(p.x() for p in corners)
ymin, ymax = min(p.y() for p in corners), max(p.y() for p in corners)

layer = QgsVectorLayer("Polygon?crs=EPSG:25833&field=dens:double&field=residents:double",
                       "Umland Einwohner (Zensus 2022)", "memory")
feats, total, skipped_berlin = [], 0, 0
with zf.open(member) as raw:
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
    reader = csv.reader(text, delimiter=";")
    header = [h.strip().lower() for h in next(reader)]
    ix = next(i for i, h in enumerate(header) if h.startswith("x_mp"))
    iy = next(i for i, h in enumerate(header) if h.startswith("y_mp"))
    ie = next(i for i, h in enumerate(header) if "einwohner" in h)
    for row_no, row in enumerate(reader):
        if row_no % 500000 == 0 and row_no:
            print(f"    {row_no:,} grid rows read, {len(feats):,} cells kept")
        try:
            x, y = float(row[ix]), float(row[iy])
        except (ValueError, IndexError):
            continue
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            continue
        try:
            people = float(row[ie].replace(",", "."))
        except ValueError:
            continue                  # "–" = kept secret / no data
        if people <= 0:
            continue
        centre = to_utm.transform(QgsPointXY(x, y))
        centre_geom = QgsGeometry.fromPointXY(centre)     # keep it alive while QGIS uses it
        if engine.contains(centre_geom.constGet()):
            skipped_berlin += 1
            continue
        ring = [to_utm.transform(QgsPointXY(x + dx, y + dy))
                for dx, dy in ((-50, -50), (50, -50), (50, 50), (-50, 50), (-50, -50))]
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        f.setAttributes([people, people])          # 1 cell = 1 ha
        feats.append(f)
        total += people
layer.dataProvider().addFeatures(feats)
print(f"OK  {len(feats):,} populated cells outside Berlin, ~{total:,.0f} residents "
      f"within {MARGIN_KM} km ({skipped_berlin:,} Berlin cells left out)")

# ---------------------------------------------------- 3. save + style like Berlin
umland = save(layer, "umland_zensus100m.gpkg", "Umland Einwohner (Zensus 2022)")
renderer = density_src.renderer().clone()
if hasattr(renderer, "setClassAttribute"):
    renderer.setClassAttribute("dens")
umland.setRenderer(renderer)
umland.setOpacity(density_src.opacity())

border = QgsVectorLayer(mask_path, "Stadtgrenze", "ogr")
border.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
    "color": "0,0,0,0", "outline_color": "255,255,255,110", "outline_width": "0.35"})))

root = PROJECT.layerTreeRoot()
PROJECT.addMapLayer(umland, False)
PROJECT.addMapLayer(border, False)
for old in [l for l in PROJECT.mapLayers().values()
            if l.name() == "Stadtgrenze" and l.id() != border.id()]:
    PROJECT.removeMapLayer(old.id())
idx = next((i for i, n in enumerate(root.children()) if n.name().startswith("Einwohnerdichte")),
           len(root.children()) - 1)
root.insertLayer(idx + 1, umland)       # just under the Berlin population layer
root.insertLayer(idx, border)           # boundary line above it
PROJECT.write()
print("Done - Brandenburg layer and city boundary added, project saved.")
