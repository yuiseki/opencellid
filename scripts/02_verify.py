#!/usr/bin/env python3
"""Check every snapshot under data/raw/ against its MANIFEST.json, and the Parquet against the CSV.

For each data/raw/<YYYY-MM-DD>/MANIFEST.json:
  - every file listed exists, with the manifest's size and sha256
  - every URL in the manifest carries token=REDACTED, not a token
  - every .csv.gz decompresses to the end; its rows are counted with the csv
    module (header excluded) and its header is the documented one

Then, for the world export cell_towers.csv.gz, against
data/parquet/<YYYY-MM-DD>/cell_towers.parquet:
  - the Parquet has as many rows as the CSV read by the csv module
  - every cell equals the CSV's, row by row in order (text as text, integers
    and doubles as Python reads them), compared one by one
  - every geometry is the point (lon, lat) of its row
  - the GeoParquet metadata names geometry as the primary column, WKB, Point,
    with no crs (OGC:CRS84) and a bbox inside -180..180, -90..90
  - a DuckDB with extension autoloading turned off opens it
  - no row group carries a bloom filter

Prints one ok/FAIL line per check and exits non-zero on any failure.

    uv run python scripts/02_verify.py
"""

import csv
import gzip
import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PQ = ROOT / "data" / "parquet"
REDACTED_PREFIX = "https://opencellid.org/ocid/downloads?token=REDACTED&"

_spec = importlib.util.spec_from_file_location("export", ROOT / "scripts" / "03_export_parquet.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def _reader(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", errors="strict", newline="") as f:
        yield csv.reader(f, strict=True)


def count_gzip_csv(path: Path) -> tuple[list[str], int]:
    """(header, data rows), reading the gzip to its end."""
    with _reader(path) as r:
        header = next(r)
        return header, sum(1 for _ in r)


def typed(name: str, raw: str):
    """What the Parquet should hold for one CSV cell."""
    if raw == "":
        return None
    t = export.column_type(name)
    if t in ("INTEGER", "BIGINT"):
        return int(raw)
    if t == "DOUBLE":
        return float(raw)
    return raw


def compare_rows(con, src: Path, pq: Path, limit: int = 5) -> tuple[int, list]:
    """(rows compared, first differences as (row, column, csv, parquet))."""
    names = list(export.COLUMNS)
    cols = ", ".join(f'"{n}"' for n in names)
    cur = con.execute(f"select {cols} from '{pq}'")
    diff, n = [], 0
    with _reader(src) as r:
        next(r)
        batch = cur.fetchmany(100_000)
        i = 0
        for row in r:
            if i == len(batch):
                batch, i = cur.fetchmany(100_000), 0
                if not batch:
                    diff.append((n, "<row>", ",".join(row), None))
                    break
            got = batch[i]
            i += 1
            n += 1
            if len(diff) < limit:
                for c, raw, g in zip(names, row, got, strict=True):
                    if typed(c, raw) != g:
                        diff.append((n, c, raw, g))
        if i < len(batch) or cur.fetchmany(1):
            diff.append((n, "<row>", None, "the Parquet has more rows"))
    return n, diff


def unredacted(files: dict) -> list[str]:
    return [k for k, e in files.items() if not e.get("url", "").startswith(REDACTED_PREFIX)]


def verify_parquet(d: Path, csv_rows: int) -> int:
    import duckdb

    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + f"{d.name}/{what}", flush=True)
        failures += not ok

    pq = PQ / d.name / f"{export.TABLE}.parquet"
    if not pq.exists():
        check(False, f"{pq.name}: missing; run scripts/03_export_parquet.py")
        return failures
    con = duckdb.connect()
    con.execute("set memory_limit = '8GB'")
    n = con.sql(f"select count(*) from '{pq}'").fetchone()[0]
    check(n == csv_rows, f"{pq.name}: {n:,} rows, the CSV has {csv_rows:,}")
    compared, diff = compare_rows(con, d / export.SOURCE, pq)
    check(
        not diff and compared == csv_rows,
        f"{pq.name}: all {compared * len(export.COLUMNS):,} cells equal the CSV ({diff or 'none differ'})",
    )
    bad = con.sql(
        f"select count(*) from '{pq}' where geometry is distinct from "
        "('POINT(' || cast(lon as varchar) || ' ' || cast(lat as varchar) || ')')::GEOMETRY"
    ).fetchone()[0]
    check(bad == 0, f"{pq.name}: every geometry is POINT(lon lat) of its row ({bad:,} differ)")
    geo = export.geo_metadata(con, pq)
    col = geo.get("columns", {}).get("geometry", {})
    bbox = col.get("bbox", [])
    check(
        geo.get("primary_column") == "geometry"
        and col.get("encoding") == "WKB"
        and col.get("geometry_types") == ["Point"]
        and "crs" not in col
        and len(bbox) == 4
        and -180 <= bbox[0] <= bbox[2] <= 180
        and -90 <= bbox[1] <= bbox[3] <= 90,
        f"{pq.name}: GeoParquet {geo.get('version')} metadata, Point, OGC:CRS84, bbox {bbox}",
    )
    plain = duckdb.connect(
        config={"autoload_known_extensions": False, "autoinstall_known_extensions": False}
    )
    jp = plain.sql(f"select count(*) from '{pq}' where mcc in (440, 441)").fetchone()[0]
    check(jp > 0, f"{pq.name}: a DuckDB without extensions opens it ({jp:,} rows for mcc 440/441)")
    bloom = con.sql(
        "select count(*) filter (where bloom_filter_offset is not null) "
        f"from parquet_metadata('{pq}')"
    ).fetchone()[0]
    check(bloom == 0, f"{pq.name}: no bloom filters")
    return failures


def verify(d: Path) -> int:
    failures = 0
    files = json.loads((d / "MANIFEST.json").read_text())["files"]
    leaked = unredacted(files)
    if leaked:
        print(f"FAIL {d.name}: URLs not redacted in the manifest: {leaked}")
        failures += 1
    else:
        print(f"ok   {d.name}: every manifest URL has token=REDACTED")
    world_rows = None
    for name, e in files.items():
        p = d / name
        tag = f"{d.name}/{name}"
        if not p.exists():
            print(f"FAIL {tag}: missing")
            failures += 1
            continue
        if p.stat().st_size != e["bytes"] or sha256(p) != e["sha256"]:
            print(f"FAIL {tag}: size or sha256 differs from the manifest")
            failures += 1
            continue
        print(f"ok   {tag}: sha256")
        try:
            header, n = count_gzip_csv(p)
        except (OSError, EOFError, UnicodeDecodeError, csv.Error) as ex:
            print(f"FAIL {tag}: {ex}")
            failures += 1
            continue
        if header != list(export.COLUMNS):
            print(f"FAIL {tag}: header {header}")
            failures += 1
            continue
        print(f"ok   {tag}: gzip read to the end, documented header, {n:,} rows (csv module)")
        if name == export.SOURCE:
            world_rows = n
    if world_rows is not None and not failures:
        failures += verify_parquet(d, world_rows)
    return failures


def main() -> int:
    dirs = sorted(p.parent for p in RAW.glob("*/MANIFEST.json"))
    if not dirs:
        print("FAIL no MANIFEST.json under data/raw/")
        return 1
    failures = sum(verify(d) for d in dirs)
    print("all ok" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
