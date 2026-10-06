#!/usr/bin/env python3
"""Turn each snapshot's world export into one GeoParquet file.

    data/raw/<YYYY-MM-DD>/cell_towers.csv.gz -> data/parquet/<YYYY-MM-DD>/cell_towers.parquet

The CSV must have exactly the 14 columns of the database format, in its order
(https://docs.opencellid.org/docs/downloads/database-format); any other
header stops the run. DuckDB reads the gzip directly with every column as
text, and each column is cast to the type its name gives (COLUMNS below):
radio is text, cell and the two Unix timestamps BIGINT (NR cell ids exceed 32
bits), lon and lat DOUBLE, the others INTEGER. Before the cast each value is
matched against a regular expression; a value that does not fit, or a lon
outside -180..180 or lat outside -90..90, stops the run with the column and
the value. An empty cell would be NULL.

All 14 columns are kept as they are (including the deprecated changeable and
averageSignal), and a geometry column is added: a
point built from the lon and lat text of the same row (x = lon, y = lat),
written as WKB with GeoParquet 1.0 metadata by DuckDB's built-in GEOMETRY
type. The metadata has no "crs" key, which in GeoParquet means OGC:CRS84,
that is WGS 84 longitude/latitude (EPSG:4326 with x = longitude).

Rows are ordered by mcc, net and area (country, network, location area),
then by the remaining columns (ORDER_BY), so a reader that filters on a
country or a network skips the row groups whose statistics rule it out.
The full key makes the order the same on every run.

    uv run python scripts/03_export_parquet.py
"""

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "parquet"
SOURCE = "cell_towers.csv.gz"
TABLE = "cell_towers"

# the database format, in order: name -> type
COLUMNS = {
    "radio": "VARCHAR",
    "mcc": "INTEGER",
    "net": "INTEGER",
    "area": "INTEGER",
    "cell": "BIGINT",
    "unit": "INTEGER",
    "lon": "DOUBLE",
    "lat": "DOUBLE",
    "range": "INTEGER",
    "samples": "INTEGER",
    "changeable": "INTEGER",
    "created": "BIGINT",
    "updated": "BIGINT",
    "averageSignal": "INTEGER",
}
PATTERNS = {
    "VARCHAR": ("[A-Z][A-Z0-9]*", "a radio name"),
    "INTEGER": ("-?[0-9]+", "an integer"),
    "BIGINT": ("-?[0-9]+", "an integer"),
    "DOUBLE": (r"-?[0-9]+(\.[0-9]+)?", "a number"),
}
RANGES = {"lon": (-180, 180), "lat": (-90, 90)}
COPY = "format parquet, compression zstd, write_bloom_filter false"


# country, network and area first, then every other column so the order is total
ORDER_BY = ["mcc", "net", "area", "cell", "radio", "unit", "lon", "lat", "range", "samples",
            "created", "updated", "changeable", "averageSignal"]  # fmt: skip


def column_type(name: str) -> str:
    return COLUMNS[name]


def read_header(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return f.readline().rstrip("\r\n").split(",")


def check_header(path: Path) -> None:
    got = read_header(path)
    if got != list(COLUMNS):
        raise SystemExit(f"{path}: header {got} is not the documented {list(COLUMNS)}")


def _expr(name: str) -> str:
    c, t = f'"{name}"', column_type(name)
    pattern, what = PATTERNS[t]
    cast = c if t == "VARCHAR" else f"cast({c} as {t})"
    if name in RANGES:
        lo, hi = RANGES[name]
        cast = (
            f"case when {cast} between {lo} and {hi} then {cast} "
            f"else error('{name} out of range: ' || {c}) end"
        )
    return (
        f"case when {c} is null then null "
        f"when regexp_full_match({c}, '{pattern}') then {cast} "
        f"else error('{name} is not {what}: ' || {c}) end"
    )


def order_key_sql() -> str:
    """The ORDER BY list. A DOUBLE column used directly as a sort key comes back
    with -0.0 as 0.0 (DuckDB 1.5.6), and the CSV has "-0" coordinates, so lon
    and lat are sorted through their text and keep their sign."""
    return ", ".join(f'"{n}"::varchar' if COLUMNS[n] == "DOUBLE" else f'"{n}"'
                     for n in ORDER_BY)  # fmt: skip


def select_sql(path: Path) -> str:
    src = (
        f"read_csv('{path}', header = true, all_varchar = true, strict_mode = true, "
        f"columns = {{{', '.join(f"'{n}': 'VARCHAR'" for n in COLUMNS)}}})"
    )
    cols = [f'{_expr(n)} as "{n}"' for n in COLUMNS]
    geom = "('POINT(' || lon || ' ' || lat || ')')::GEOMETRY as geometry"
    order = order_key_sql()
    return f"select {', '.join(cols)}, {geom} from {src} order by {order}"


def export(con, src: Path, out: Path) -> int:
    check_header(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    con.execute(f"copy ({select_sql(src)}) to '{tmp}' ({COPY})")
    tmp.replace(out)
    return con.sql(f"select count(*) from '{out}'").fetchone()[0]


def geo_metadata(con, path: Path) -> dict:
    kv = con.sql(f"select value from parquet_kv_metadata('{path}') where key = 'geo'").fetchone()
    return json.loads(kv[0])


def main() -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("set memory_limit = '8GB'")
    manifests = sorted(RAW.glob("*/MANIFEST.json"))
    if not manifests:
        raise SystemExit("no data/raw/<YYYY-MM-DD>/MANIFEST.json")
    for mpath in manifests:
        version = mpath.parent.name
        if SOURCE not in json.loads(mpath.read_text())["files"]:
            print(f"{version}: no {SOURCE} in the manifest, skipped")
            continue
        out = OUT / version / f"{TABLE}.parquet"
        n = export(con, mpath.parent / SOURCE, out)
        print(f"{version}: {out.name} {n:,} rows, {out.stat().st_size:,} bytes", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
