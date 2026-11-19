# Berlin U-Bahn extensions: who would they reach?

A small, reproducible QGIS/PyQGIS analysis of Berlin's proposed U-Bahn extensions.
For each extension it estimates

- how many residents live within walking distance of the new stations,
- how many of them would **newly** get a rapid-transit station nearby (people already
  near an existing U-Bahn or S-Bahn station are not counted),
- which large **housing projects** fall inside the new walking zones, and how many
  future residents they add,
- which new stations would **connect to the S-Bahn** or other U-Bahn lines.

It also builds a map of Berlin's population density, the U-Bahn and S-Bahn networks,
interchange stations, and the population of the surrounding Brandenburg area.

> Status: work in progress. Station positions for proposed lines and housing-project
> locations are approximate (see [Limitations](#limitations)).

## Repository layout

```
data/
  extensions.csv      proposed extensions, one row per station (editable)
  housing.csv         large housing projects (editable)
scripts/
  01_base_map.py              population blocks + U-Bahn, styled base map
  02_extensions.py            extension lines and stations from data/extensions.csv
  03_housing.py               housing projects from data/housing.csv
  04_sbahn_interchanges.py    S-Bahn network + interchange stations (OSM)
  05_brandenburg.py           population around Berlin, Zensus 2022 100 m grid (optional)
  06_analyse_extensions.py    the analysis: CSV reports + catchment layer
output/               generated files (not committed)
```

## Requirements

- [QGIS](https://qgis.org) 3.x (developed with a Qt6-based QGIS release on Windows).
  No extra plugins or Python packages are needed; everything uses PyQGIS.
- An internet connection for the first run (Berlin geoportal, OpenStreetMap Overpass API).

## How to run

1. Clone or download this repository.
2. Open QGIS, create a new project and **save it in the repository folder**
   (e.g. `berlin_ubahn.qgz`). The scripts find `data/` and `output/` relative to it.
3. Open *Plugins → Python Console → Show Editor*, then open and run the scripts in order:

| Script | Takes | Produces |
|---|---|---|
| `01_base_map.py` | a few minutes (first run) | `output/einwohnerdichte.gpkg`, `output/ubahn_osm.gpkg` |
| `02_extensions.py` | seconds | extension lines + stations |
| `03_housing.py` | seconds | housing-project layer |
| `04_sbahn_interchanges.py` | ~1 min (first run) | S-Bahn + interchange layers, OSM caches |
| `05_brandenburg.py` *(optional)* | a few minutes | population around Berlin |
| `06_analyse_extensions.py` | ~1 min | `output/extension_summary.csv`, `output/extension_stations.csv`, `output/extension_catchments.gpkg` |

For `05_brandenburg.py`, first download **"Bevölkerungszahlen in Gitterzellen"**
(ZIP, ~18 MB) from [zensus2022.de](https://www.zensus2022.de) and put the ZIP into `data/`.
It is needed for any extension that leaves Berlin, e.g. the U7 to BER via Schönefeld.

Downloads are cached in `output/`, so later runs work offline and are fast.

## Outputs

**`extension_summary.csv`** - one row per extension, sorted by newly reached residents:

| Column | Meaning |
|---|---|
| `residents_within_800m` | residents within 800 m of any new station |
| `residents_newly_reached` | of those, residents not already within 800 m of an existing U-/S-Bahn station |
| `newly_reached_berlin` / `_brandenburg` | split by state |
| `new_stations_with_s_bahn` | new stations with S-Bahn platforms within 250 m |
| `housing_projects_in_zone` | projects from `housing.csv` inside the walking zones (homes) |
| `planned_homes_in_zone`, `est_future_residents_in_zone` | homes and estimated residents (homes × 2.1) |
| `planned_homes_newly_reached` | homes in the newly reached part only |

**`extension_stations.csv`** - one row per new station: S-Bahn and U-Bahn lines within
250 m, and residents within 800 m of that station (zones of neighbouring stations overlap,
so these do not add up to the route total).

**`extension_catchments.gpkg`** - the walking zones, split into *newly reached* and
*already served*, with resident counts.

CSV files use `;` as separator and UTF-8 with BOM, so they open directly in Excel.

## Method

- **Walking zone:** a circle of 800 m (straight-line) around each station, a common
  planning rule of thumb for rapid transit. Change `WALK_RADIUS_M` in script 06.
- **Newly reached:** walking zones of the new stations *minus* 800 m circles around all
  existing U-Bahn and S-Bahn stop/platform positions from OpenStreetMap.
- **Residents in a zone:** block-level resident counts (Berlin) and 100 m census cells
  (Brandenburg), weighted by the share of each block's area inside the zone - i.e.
  assuming residents are spread evenly within a block.
- **Future residents from housing:** planned homes × 2.1, the ratio implied by the Senate's
  figure of about 130,000 residents in 62,000 homes in the new urban quarters.
- **Interchanges:** two U-Bahn lines count as an interchange only where they go different
  ways (crossing or branching), derived from the station order in OSM route relations;
  stations on shared track do not count. A U-/S-Bahn interchange needs platforms within 250 m.

Example output of script 04 (OpenStreetMap, November 2026):

| Line | Interchanges with other U-Bahn lines | Interchanges with the S-Bahn |
|---|---|---|
| U1 | 7 | 1 |
| U2 | 7 | 6 |
| U3 | 8 | 2 |
| U4 | 2 | 1 |
| U5 | 2 | 6 |
| U6 | 5 | 3 |
| U7 | 7 | 5 |
| U8 | 4 | 5 |
| U9 | 6 | 4 |

## Data sources

| Data | Source | Licence |
|---|---|---|
| Population density per block (Umweltatlas, Einwohnerdichte) | Amt für Statistik Berlin-Brandenburg via [Geoportal Berlin](https://daten.berlin.de) | as listed on daten.berlin.de (earlier editions: CC BY 3.0 DE) |
| Population, 100 m grid | [Zensus 2022](https://www.zensus2022.de), Statistische Ämter des Bundes und der Länder | see zensus2022.de |
| U-Bahn, S-Bahn, stations | © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors | ODbL |
| Proposed extensions | Senate of Berlin, BVG feasibility studies, Abgeordnetenhaus written questions (e.g. Drs. 19/22736) | compiled by hand |
| Housing projects | Senate of Berlin (Neue Stadtquartiere), district offices, press | compiled by hand |

Please credit the sources above when you publish maps or numbers from this project.

## Limitations

- **Station positions for proposed lines are approximate.** Official plans often only name
  stations; coordinates in `extensions.csv` were placed by hand and should be checked.
- **Housing coordinates are approximate** unless `checked = yes` in `housing.csv`, and the
  list is not complete (e.g. projects in Schönefeld are still missing).
- **Straight lines and circles:** extensions are drawn station to station, and walking zones
  are circles, not walking-network isochrones. Barriers like rivers or rail lines are ignored.
- **Different reference dates:** Berlin blocks (latest Umweltatlas edition) and Brandenburg
  census cells (May 2022). Very small census counts are withheld for privacy.
- **OSM-based interchanges** depend on how stations are tagged; long transfers (> 250 m)
  are not counted.

## Ideas / roadmap

- Walking isochrones on the street network instead of circles
- Add the BVG vision network (U0 ring etc.) to `extensions.csv` with `status = vision`
- Complete the housing list, including Brandenburg municipalities next to Berlin
- Travel-time savings to the city centre per extension

## Licence

Code: MIT, see [LICENSE](LICENSE). Data in `data/` was compiled from the public sources
listed above; the underlying data stays under the sources' own licences.
