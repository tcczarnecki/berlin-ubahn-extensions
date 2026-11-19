"""
Step 3 - Housing projects
=========================
Reads data/housing.csv and builds the layer "Wohnungsbau" - one circle per
project, sized by planned homes, coloured by status, labelled with its name.
Prints estimated future residents per project (homes x RESIDENTS_PER_HOME).

Coordinates in housing.csv are approximate unless checked = yes. To fix one:
read the correct position from the QGIS status bar (EPSG:4326), edit the row,
set checked = yes and rerun.
"""

import csv
import os

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY, QgsProject,
    QgsProperty, QgsSymbolLayer, QgsTextBufferSettings, QgsTextFormat,
    QgsVectorFileWriter, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor, QFont

# Senate: 62,000 homes in the new quarters for ~130,000 residents
RESIDENTS_PER_HOME = 130000 / 62000

PROJECT = QgsProject.instance()
ROOT = QgsProject.instance().homePath()
if not ROOT:
    raise RuntimeError("Save your QGIS project in the repository folder first "
                       "(e.g. berlin_ubahn.qgz), then run this again.")
DATA = os.path.join(ROOT, "data")       # inputs: CSV tables, census ZIP
OUT = os.path.join(ROOT, "output")      # everything the scripts generate
os.makedirs(OUT, exist_ok=True)
folder = OUT
CSV = os.path.join(DATA, "housing.csv")
if not os.path.exists(CSV):
    raise RuntimeError("data/housing.csv not found in " + ROOT)

xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                               QgsCoordinateReferenceSystem("EPSG:25833"), PROJECT)

with open(CSV, encoding="utf-8-sig", newline="") as fh:
    dialect = csv.Sniffer().sniff(fh.read(2048), delimiters=",;")
    fh.seek(0)
    rows = list(csv.DictReader(fh, dialect=dialect))

def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return default

mem = QgsVectorLayer("Point?crs=EPSG:25833", "Wohnungsbau", "memory")
mem.dataProvider().addAttributes([
    QgsField("name", QVariant.String), QgsField("district", QVariant.String),
    QgsField("homes", QVariant.Int), QgsField("residents", QVariant.Int),
    QgsField("status", QVariant.String), QgsField("completion", QVariant.String),
    QgsField("checked", QVariant.String)])
mem.updateFields()

feats, total_homes, unchecked = [], 0, 0
for r in rows:
    if not r.get("lat") or not r.get("lon"):
        print(f"!!  {r['name']}: no coordinates - skipped")
        continue
    homes = int(num(r["homes"]))
    f = QgsFeature(mem.fields())
    f.setGeometry(QgsGeometry.fromPointXY(
        xform.transform(QgsPointXY(num(r["lon"]), num(r["lat"])))))
    f.setAttributes([r["name"], r["district"], homes,
                     int(homes * RESIDENTS_PER_HOME), r["status"].strip().lower(),
                     r.get("completion", ""), r.get("checked", "no")])
    feats.append(f)
    total_homes += homes
    unchecked += r.get("checked", "no").strip().lower() != "yes"
    print(f"    {r['name']:<52} {homes:>6} homes  ~{homes * RESIDENTS_PER_HOME:>7,.0f} residents")
mem.dataProvider().addFeatures(feats)
print(f"OK  {len(feats)} projects, {total_homes:,} homes, "
      f"~{total_homes * RESIDENTS_PER_HOME:,.0f} future residents")
if unchecked:
    print(f"    {unchecked} positions still marked unchecked - see note at the top")

# ---------------------------------------------------------------- save
for old in [l for l in PROJECT.mapLayers().values() if l.name() == "Wohnungsbau"]:
    PROJECT.removeMapLayer(old.id())
path = os.path.join(folder, "wohnungsbau.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, PROJECT.transformContext(), opts)
layer = QgsVectorLayer(path, "Wohnungsbau", "ogr")

# ---------------------------------------------------------------- style
sym = QgsMarkerSymbol.createSimple({"name": "circle", "outline_color": "#000000",
                                    "outline_width": "0.4"})
sl = sym.symbolLayer(0)
# area grows with homes: 160 homes ~ 2.5 mm, 13,000 homes ~ 13 mm
sl.setDataDefinedProperty(QgsSymbolLayer.PropertySize,
                          QgsProperty.fromExpression('1.2 + sqrt("homes") * 0.1'))
sl.setDataDefinedProperty(QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(
    "CASE WHEN \"status\" = 'under construction' THEN '255,209,102,210' "
    "WHEN \"status\" = 'completed' THEN '160,160,170,180' "
    "ELSE '255,140,66,190' END"))
layer.renderer().setSymbol(sym)

fmt = QgsTextFormat()
fmt.setFont(QFont("Arial"))
fmt.setSize(8)
fmt.setColor(QColor("#ffffff"))
buf = QgsTextBufferSettings()
buf.setEnabled(True)
buf.setColor(QColor("#000000"))
buf.setSize(0.8)
fmt.setBuffer(buf)
lab = QgsPalLayerSettings()
lab.fieldName = "\"name\" || '\\n' || format_number(\"homes\", 0) || ' homes'"
lab.isExpression = True
lab.setFormat(fmt)
layer.setLabeling(QgsVectorLayerSimpleLabeling(lab))
layer.setLabelsEnabled(True)

PROJECT.addMapLayer(layer)
PROJECT.write()
print("Done - layer 'Wohnungsbau' added and project saved.")
