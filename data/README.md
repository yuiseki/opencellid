---
license: cc-by-sa-4.0
language:
- en
pretty_name: OpenCelliD cell tower snapshots
task_categories:
- tabular-classification
tags:
- geospatial
- geoparquet
- cell-towers
- mobile-network
- opencellid
size_categories:
- 1M<n<10M
configs:
- config_name: '2026-10-06'
  data_files: parquet/2026-10-06/cell_towers.parquet
  default: true
---

# opencellid

Snapshots of the world export of OpenCelliD (https://opencellid.org), the
open database of mobile network cells run by Unwired Labs: one row per cell
(GSM, UMTS, LTE, NR, CDMA) with the cell's identifiers and its position as
OpenCelliD estimates it from community measurements. Each snapshot is the
file `cell_towers.csv.gz` exactly as served, and the same rows as
GeoParquet with typed columns and a point geometry.

Cell tower data from OpenCellID (https://opencellid.org),
licensed under CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/).
Changes: the CSV converted to GeoParquet with typed columns and an added
point geometry built from lon and lat; no value changed. See Licence.

## Why snapshots

OpenCelliD regenerates its exports every day by 02:00 GMT, and an export
holds only the cells observed in the last 18 months. A cell that nobody has
measured for 18 months disappears from later exports (OpenCelliD's API
still answers for it, but the files do not). A later file is therefore not
a superset of an earlier one, and no export is replaced here: each fetch is
kept as its own snapshot, named after the export date the download page
states, and new snapshots are added beside the old ones. They accumulate.

## Snapshots

One subset per snapshot.

| subset | rows | made from |
|---|---:|---|
| `2026-10-06` | 5,502,843 | `raw/2026-10-06/cell_towers.csv.gz` (119,529,026 bytes) |

`2026-10-06` is the export the download page dated "Updated: 2026-10-06"
(Last-Modified Tue, 06 Oct 2026 00:16:33 GMT), fetched at
2026-10-06T06:12:19Z. Its `updated` values run from 2025-04-06 to
2026-10-05, its `created` values from 2008-04-05.

`raw/<date>/MANIFEST.json` gives the file's URL (with the access token
replaced by `REDACTED`), size, sha256, Last-Modified and fetch time, and the
whole listing of the download page on that day: the world file, one file
per Mobile Country Code (179 on 2026-10-06, with their stated sizes and the
countries the page lists them under), the daily differential files, and the
MCCs the page lists with "No recent data". The per-country and daily files
are listed there but not included.

## Columns

The 14 columns of the database format
(https://docs.opencellid.org/docs/downloads/database-format), in its order,
then the geometry. The CSV's header must be exactly these 14 names or the
build stops. Each value was matched against a regular expression before
the cast (`[A-Z][A-Z0-9]*` for radio, `-?[0-9]+` for integers,
`-?[0-9]+(\.[0-9]+)?` for lon and lat), lon had to be within -180..180 and
lat within -90..90; a value that did not fit would have stopped the build.
An empty cell would be NULL (there is none).

| column | type | meaning (OpenCelliD's documentation) |
|---|---|---|
| `radio` | VARCHAR | Radio technology: GSM, UMTS, LTE, NR or CDMA |
| `mcc` | INTEGER | Mobile Country Code |
| `net` | INTEGER | Mobile Network Code (MNC), or System Identifier (SID) for CDMA |
| `area` | INTEGER | Location Area Code (LAC), Tracking Area Code (TAC) for LTE, or Network Identifier (NID) for CDMA |
| `cell` | BIGINT | Cell ID; for UMTS the combined RNC and cell identifier, for CDMA the Base Station Identifier (BID). NR cell ids exceed 32 bits |
| `unit` | INTEGER | Primary Scrambling Code (PSC) for UMTS or Physical Cell ID (PCI) for LTE |
| `lon` | DOUBLE | Estimated cell longitude in degrees |
| `lat` | DOUBLE | Estimated cell latitude in degrees |
| `range` | INTEGER | Estimated cell range in metres |
| `samples` | INTEGER | Number of measurements assigned to the cell |
| `changeable` | INTEGER | Deprecated. Always 1 |
| `created` | BIGINT | When the cell was first seen, Unix time in seconds (UTC) |
| `updated` | BIGINT | When the cell was last seen, Unix time in seconds (UTC) |
| `averageSignal` | INTEGER | Deprecated. Always 0 |
| `geometry` | GEOMETRY (WKB) | `POINT(lon lat)` built from the row's lon and lat text |

The geometry column is WKB with GeoParquet 1.0.0 metadata (primary column
`geometry`, geometry type Point, a bbox). The metadata has no `crs` key,
which in GeoParquet means OGC:CRS84: WGS 84 longitude/latitude, the same
datum as EPSG:4326 with x as longitude. Rows are ordered by `mcc`, `net` and `area` (country, network, location
area), then by the other columns, so a reader that filters on a country or a
network skips most row groups: Japan (`mcc` 440 and 441) is in 7 of the 45.
The file opens in DuckDB without the spatial extension, and in GeoPandas,
GDAL and other GeoParquet readers.

## Counts in 2026-10-06

By radio: LTE 3,652,892; GSM 994,931; UMTS 595,010; NR 260,005; CDMA 5.

199 MCCs. The largest: 440 Japan 740,404; 310 United States 556,893;
208 France 391,612; 262 Germany 264,083; 460 China 213,011; 334 Mexico
205,018; 505 Australia 151,627; 311 United States 146,815; 234 United
Kingdom 146,217; 639 Kenya 139,983.

Japan: MCC 440 has 740,404 cells (LTE 699,556, UMTS 21,501, NR 19,347),
MCC 441 has 120 (all LTE).

```sql
-- DuckDB, straight off the Hub: cells per radio in Japan
SELECT radio, count(*) AS cells
FROM 'hf://datasets/yuiseki/opencellid/parquet/2026-10-06/cell_towers.parquet'
WHERE mcc IN (440, 441)
GROUP BY radio ORDER BY cells DESC;

-- cells around Tokyo Station, with timestamps
SELECT radio, net, area, cell, lon, lat, range, samples,
       make_timestamp(updated * 1000000) AS last_seen
FROM 'hf://datasets/yuiseki/opencellid/parquet/2026-10-06/cell_towers.parquet'
WHERE lon BETWEEN 139.76 AND 139.77 AND lat BETWEEN 35.676 AND 35.686;
```

## Things to know

- Positions are estimates from measurements sent by OpenCelliD's
  contributors, not the locations of antennas. Several cell ids can belong
  to one site. `range` and `samples` say how much to trust a row; 3,109,353
  rows (57%) have `range` exactly 1000 and 916,581 have a single sample.
- Coverage depends on who contributed where. The export is not an inventory
  of all cells, and a country's count reflects contributors as much as
  networks.
- `unit` is never empty, although the documentation says it is empty for
  GSM and CDMA. 0 and -1 stand for unknown: 826,829 GSM rows and 2,618,144
  LTE rows have -1.
- No two rows share (radio, mcc, net, area, cell) in 2026-10-06.
- 20 MCCs in the world export have no per-country file on the download
  page, among them 374 (Trinidad and Tobago, 13 rows), which the page lists
  under "No recent data".
- Counts and coverage from different snapshots are not directly
  comparable: each covers its own 18-month window.

## Licence

OpenCelliD's attribution page (https://docs.opencellid.org/docs/attribution,
read 2026-10-06): "OpenCellID data is licensed under CC BY-SA 4.0,
including API results, dataset downloads and daily changes." Its credit
for print or plain text, quoted exactly:

    Cell tower data from OpenCellID (https://opencellid.org),
    licensed under CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/).

The page asks to "credit OpenCelliD, link to the source and license, and
indicate any changes", to make the credit visible where people meet the
data, and, for adapted material, to follow the ShareAlike terms. Anything
you build from this dataset and share is to be shared under CC BY-SA 4.0,
with that credit and a note of your changes. Exceptions to attribution are
granted only in writing by Unwired Labs. This dataset is not made or
endorsed by OpenCelliD or Unwired Labs. See `LICENSE` and
`provenance.yaml`.

## How it was made

The fetch, checks and conversion are in https://github.com/yuiseki/opencellid.
Before upload, every raw file was checked against its manifest (size,
sha256, read to the end of the gzip), the CSV rows were counted with
Python's csv module, every row of the Parquet was compared with the CSV's
rows as typed values (as a multiset, since the Parquet is sorted: the count
and a sum of per-row hashes must match), the sort order was checked, and
every geometry was checked against its row's lon and lat.
