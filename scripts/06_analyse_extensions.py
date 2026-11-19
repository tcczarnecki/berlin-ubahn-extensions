"""
Step 6 - Analyse every proposed extension
=========================================
For each extension in data/extensions.csv this works out:

  - residents within WALK_RADIUS_M of its new stations (Berlin + Brandenburg)
  - residents NEWLY reached: the part of those walking zones that is not
    already within WALK_RADIUS_M of an existing U-Bahn or S-Bahn station
  - housing projects (data/housing.csv) inside the walking zones, with planned
    homes and estimated future residents
  - per new station: S-Bahn and other U-Bahn lines within TRANSFER_DIST_M

Outputs (in output/):
  extension_summary.csv     one row per extension
  extension_stations.csv    one row per new station
  extension_catchments.gpkg walking zones split into "newly reached" and
                            "already served", added to the project as a layer

Needs steps 1-4 run first; step 5 (Brandenburg) for extensions leaving Berlin.
"""

import csv
import json
import os
import re

from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsFeature, QgsFeatureRequest, QgsField,
    QgsFillSymbol, QgsGeometry, QgsPointXY, QgsProject, QgsRendererCategory,
    QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

WALK_RADIUS_M = 800          # walking distance to a station (straight line)
TRANSFER_DIST_M = 250        # platforms closer than this count as a connection
RESIDENTS_PER_HOME = 130000 / 62000   # Senate figure for the new urban quarters

PROJECT = QgsProject.instance()
ROOT = PROJECT.homePath()
if not ROOT:
    raise RuntimeError("Save your QGIS project in the repository folder first.")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)
CRS = QgsCoordinateReferenceSystem("EPSG:25833")
TO_UTM = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), CRS, PROJECT)


def find_layer(prefix):
    return next((l for l in PROJECT.mapLayers().values()
                 if l.name().startswith(prefix)), None)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


density = find_layer("Einwohnerdichte")
ext_lines = find_layer("Erweiterungen (Linien)")
ext_pts = find_layer("Erweiterungen (Stationen)")
housing = find_layer("Wohnungsbau")
umland = find_layer("Umland")
stops_file = os.path.join(OUT, "osm_routes_stops_v2.json")
missing = [n for n, v in (("01_base_map", density), ("02_extensions", ext_lines),
                          ("04_sbahn_interchanges", os.path.exists(stops_file) or None))
           if v is None]
if missing:
    raise RuntimeError("Run these first: " + ", ".join(missing))
if housing is None:
    print("note: no housing layer (step 3) - housing columns will be empty")
if umland is None:
    print("note: no Brandenburg layer (step 5) - residents outside Berlin are not counted")

fields = [f.name() for f in density.fields()]
count_field = next((f for f in fields if re.fullmatch(r"ew\d{4}", f.lower())), None)
dens_field = next((f for f in fields if f.lower().startswith("ew_ha")), None)
YEAR = (re.search(r"(\d{4})", density.name()) or [None, "?"])[1]


def residents_berlin(f, g):
    return num(f[count_field]) if count_field else num(f[dens_field]) * g.area() / 10000.0


def residents_umland(f, g):
    return num(f["residents"])


# --------------------------------------------- existing stations (from step 4)
with open(stops_file, encoding="utf-8") as fh:
    osm = json.load(fh)
nodes = {e["id"]: e for e in osm["elements"] if e["type"] == "node"}
stop_lines = {}                                   # node id -> {(kind, ref)}
for rel in (e for e in osm["elements"] if e["type"] == "relation"):
    tags = rel.get("tags", {})
    kind = "U" if tags.get("route") == "subway" else "S"
    ref = (tags.get("ref") or "?").replace(" ", "")
    for m in rel.get("members", []):
        if m["type"] == "node" and m.get("role", "").startswith("stop"):
            stop_lines.setdefault(m["ref"], set()).add((kind, ref))
existing = []                                     # (point, {(kind, ref)})
for nid, n in nodes.items():
    existing.append((TO_UTM.transform(QgsPointXY(n["lon"], n["lat"])),
                     stop_lines.get(nid, set())))
print(f"{len(existing)} existing U-/S-Bahn stop and platform points")


def sum_residents(layer, func, area_geom):
    """Area-weighted residents of `layer` inside `area_geom`."""
    if layer is None or area_geom.isEmpty():
        return 0.0
    eng = QgsGeometry.createGeometryEngine(area_geom.constGet())
    eng.prepareGeometry()
    total = 0.0
    for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(area_geom.boundingBox())):
        g = f.geometry()
        if g is None or g.isEmpty() or g.area() <= 0 or not eng.intersects(g.constGet()):
            continue
        r = func(f, g)
        if r > 0:
            total += r * area_geom.intersection(g).area() / g.area()
    return total


# ------------------------------------------------------------ per extension
routes = {}
for f in ext_pts.getFeatures():
    routes.setdefault(f["route"], []).append(f)
status_of = {f["route"]: f["status"] for f in ext_lines.getFeatures()}
line_of = {f["route"]: f["line"] for f in ext_lines.getFeatures()}
length_of = {f["route"]: f.geometry().length() for f in ext_lines.getFeatures()}

summary, station_rows, zone_feats = [], [], []
zones = QgsVectorLayer("MultiPolygon?crs=EPSG:25833", "Einzugsgebiete Erweiterungen", "memory")
zones.dataProvider().addAttributes([QgsField("route", QVariant.String),
                                    QgsField("kind", QVariant.String),
                                    QgsField("residents", QVariant.Int)])
zones.updateFields()

for route, feats in routes.items():
    new = [f for f in feats if int(f["new"]) == 1]
    if not new:
        continue
    pts = [f.geometry().asPoint() for f in new]
    zone = QgsGeometry.unaryUnion([QgsGeometry.fromPointXY(p).buffer(WALK_RADIUS_M, 48)
                                   for p in pts])
    near = zone.buffer(WALK_RADIUS_M + 50, 8)
    cover_parts = [QgsGeometry.fromPointXY(p).buffer(WALK_RADIUS_M, 48)
                   for p, _ in existing if near.contains(QgsGeometry.fromPointXY(p))]
    cover = QgsGeometry.unaryUnion(cover_parts) if cover_parts else QgsGeometry()
    newly = zone.difference(cover) if not cover.isEmpty() else QgsGeometry(zone)
    already = zone.intersection(cover) if not cover.isEmpty() else QgsGeometry()

    ber_all = sum_residents(density, residents_berlin, zone)
    bb_all = sum_residents(umland, residents_umland, zone)
    ber_new = sum_residents(density, residents_berlin, newly)
    bb_new = sum_residents(umland, residents_umland, newly)

    projects, homes, homes_new = [], 0, 0
    if housing is not None:
        for h in housing.getFeatures():
            g = h.geometry()
            if zone.contains(g):
                hm = int(num(h["homes"]))
                projects.append(f"{h['name']} ({hm})")
                homes += hm
                if newly.contains(g):
                    homes_new += hm

    for f, p in zip(new, pts):
        s_lines = sorted({ref for q, ls in existing if p.distance(q) < TRANSFER_DIST_M
                          for k, ref in ls if k == "S"}, key=lambda r: (len(r), r))
        u_lines = sorted({ref for q, ls in existing if p.distance(q) < TRANSFER_DIST_M
                          for k, ref in ls if k == "U" and ref != line_of[route]})
        own = QgsGeometry.fromPointXY(p).buffer(WALK_RADIUS_M, 48)
        station_rows.append({
            "route": route, "station": f["station"],
            "s_bahn_connections": " ".join(s_lines), "u_bahn_connections": " ".join(u_lines),
            f"residents_within_{WALK_RADIUS_M}m": round(
                sum_residents(density, residents_berlin, own)
                + sum_residents(umland, residents_umland, own)),
        })

    row = {
        "route": route, "line": line_of[route], "status": status_of[route],
        "new_stations": len(new), "length_km_straight": round(length_of[route] / 1000, 1),
        f"residents_within_{WALK_RADIUS_M}m": round(ber_all + bb_all),
        "residents_newly_reached": round(ber_new + bb_new),
        "newly_reached_berlin": round(ber_new), "newly_reached_brandenburg": round(bb_new),
        "new_stations_with_s_bahn": sum(1 for r in station_rows
                                        if r["route"] == route and r["s_bahn_connections"]),
        "housing_projects_in_zone": "; ".join(projects),
        "planned_homes_in_zone": homes,
        "est_future_residents_in_zone": round(homes * RESIDENTS_PER_HOME),
        "planned_homes_newly_reached": homes_new,
    }
    summary.append(row)
    for kind, geom, res in (("newly reached", newly, ber_new + bb_new),
                            ("already served", already, ber_all + bb_all - ber_new - bb_new)):
        if geom.isEmpty():
            continue
        g = QgsGeometry(geom)
        g.convertToMultiType()
        zf = QgsFeature(zones.fields())
        zf.setGeometry(g)
        zf.setAttributes([route, kind, int(round(res))])
        zone_feats.append(zf)
    print(f"OK  {route:<24} newly reached ~{ber_new + bb_new:>8,.0f} "
          f"(of {ber_all + bb_all:,.0f} in zone)  homes in zone: {homes:,}")

# ------------------------------------------------------------------ write
def write_csv(name, rows):
    if not rows:
        return
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:   # opens fine in Excel
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print("    wrote", os.path.relpath(path, ROOT))


summary.sort(key=lambda r: -r["residents_newly_reached"])
write_csv("extension_summary.csv", summary)
write_csv("extension_stations.csv", station_rows)

zones.dataProvider().addFeatures(zone_feats)
for old in [l for l in PROJECT.mapLayers().values() if l.name() == zones.name()]:
    PROJECT.removeMapLayer(old.id())
gpkg = os.path.join(OUT, "extension_catchments.gpkg")
if os.path.exists(gpkg):
    try:
        os.remove(gpkg)
    except PermissionError:
        gpkg = os.path.join(OUT, "extension_catchments_2.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
QgsVectorFileWriter.writeAsVectorFormatV3(zones, gpkg, PROJECT.transformContext(), opts)
layer = QgsVectorLayer(gpkg, zones.name(), "ogr")
layer.setRenderer(QgsCategorizedSymbolRenderer("kind", [
    QgsRendererCategory("newly reached", QgsFillSymbol.createSimple(
        {"color": "255,209,102,70", "outline_color": "255,209,102,230",
         "outline_width": "0.5"}), "newly reached"),
    QgsRendererCategory("already served", QgsFillSymbol.createSimple(
        {"color": "120,120,130,90", "outline_color": "160,160,170,160",
         "outline_width": "0.3"}), "already served"),
]))
PROJECT.addMapLayer(layer)
PROJECT.write()
print(f"Done. Population: AfS Berlin-Brandenburg {YEAR}"
      + (", Zensus 2022" if umland is not None else "") + ".")
