import gzip
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("export", ROOT / "scripts" / "03_export_parquet.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

# eleven real rows of the 2026-10-06 world export: every radio, Japan, unit -1, an NR cell id
# beyond 32 bits, western longitudes
SAMPLE = ROOT / "tests" / "fixtures" / "cell_towers_sample.csv.gz"


def _plain():
    return duckdb.connect(
        config={"autoload_known_extensions": False, "autoinstall_known_extensions": False}
    )


def _rewrite(tmp_path, fn) -> Path:
    lines = gzip.decompress(SAMPLE.read_bytes()).decode().splitlines()
    p = tmp_path / "cell_towers.csv.gz"
    p.write_bytes(gzip.compress(("\n".join(fn(lines)) + "\n").encode()))
    return p


def test_types_come_from_the_documented_names():
    assert list(m.COLUMNS) == [
        "radio", "mcc", "net", "area", "cell", "unit", "lon", "lat", "range", "samples",
        "changeable", "created", "updated", "averageSignal",
    ]  # fmt: skip
    assert m.column_type("radio") == "VARCHAR"
    assert m.column_type("cell") == "BIGINT"
    assert m.column_type("mcc") == "INTEGER"
    assert m.column_type("lat") == "DOUBLE"
    with pytest.raises(KeyError):
        m.column_type("rssi")


def test_header_is_checked(tmp_path):
    assert m.read_header(SAMPLE) == list(m.COLUMNS)
    bad = _rewrite(tmp_path, lambda ls: [ls[0].replace("averageSignal", "avg")] + ls[1:])
    with pytest.raises(SystemExit, match="header"):
        m.check_header(bad)


def test_export_writes_typed_geoparquet(tmp_path):
    out = tmp_path / "cell_towers.parquet"
    assert m.export(duckdb.connect(), SAMPLE, out) == 11
    con = _plain()
    rel = con.sql(f"select * from '{out}'")
    assert rel.columns == [*m.COLUMNS, "geometry"]
    types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
    assert {c: types[c] for c in m.COLUMNS} == {c: m.column_type(c) for c in m.COLUMNS}
    assert types["geometry"].startswith("GEOMETRY")
    rows = rel.fetchall()
    nr = next(r for r in rows if r[4] == 23641375123)
    assert nr[:8] == ("NR", 440, 54, 2144781, 23641375123, -1, 132.4243, 34.3772)
    # the point is built from the CSV text: x = lon, y = lat
    want = con.sql("select 'POINT(132.4243 34.3772)'::geometry").fetchone()[0]
    assert nr[-1] == want


def test_export_carries_geoparquet_metadata(tmp_path):
    out = tmp_path / "cell_towers.parquet"
    m.export(duckdb.connect(), SAMPLE, out)
    geo = m.geo_metadata(duckdb.connect(), out)
    assert geo["primary_column"] == "geometry"
    col = geo["columns"]["geometry"]
    assert col["encoding"] == "WKB"
    assert col["geometry_types"] == ["Point"]
    assert "crs" not in col  # GeoParquet default: OGC:CRS84, lon/lat on WGS 84
    assert col["bbox"] == [-122.8506, 34.0737, 139.6196, 53.098]
    json.dumps(geo)


def test_a_value_that_does_not_fit_its_type_stops_it(tmp_path):
    bad = _rewrite(tmp_path, lambda ls: [ls[0], ls[1].replace("9.0114", "9.0x14")] + ls[2:])
    with pytest.raises(duckdb.Error, match="lon is not a number: 9.0x14"):
        m.export(duckdb.connect(), bad, tmp_path / "o.parquet")


def test_a_latitude_out_of_range_stops_it(tmp_path):
    bad = _rewrite(tmp_path, lambda ls: [ls[0], ls[1].replace("53.098", "93.098")] + ls[2:])
    with pytest.raises(duckdb.Error, match="lat out of range: 93.098"):
        m.export(duckdb.connect(), bad, tmp_path / "o.parquet")


def test_a_radio_that_is_not_a_word_stops_it(tmp_path):
    bad = _rewrite(tmp_path, lambda ls: [ls[0], "5" + ls[1][3:]] + ls[2:])
    with pytest.raises(duckdb.Error, match="radio is not"):
        m.export(duckdb.connect(), bad, tmp_path / "o.parquet")


def test_rows_are_ordered_by_country_network_and_area(tmp_path):
    out = tmp_path / "cell_towers.parquet"
    m.export(duckdb.connect(), SAMPLE, out)
    keys = [tuple(r) for r in _plain().sql(f"select {', '.join(m.ORDER_BY)} from '{out}'").fetchall()]
    assert keys == sorted(keys)
    assert m.ORDER_BY[:3] == ["mcc", "net", "area"]


def test_a_negative_zero_coordinate_survives_the_sort(tmp_path):
    # DuckDB returns -0.0 as 0.0 from a DOUBLE column it sorts on directly
    src = _rewrite(tmp_path, lambda ls: [ls[0], ls[1].replace(ls[1].split(",")[6], "-0", 1)])
    out = tmp_path / "cell_towers.parquet"
    m.export(duckdb.connect(), src, out)
    lon, = _plain().sql(f"select lon from '{out}'").fetchone()
    assert lon == 0 and str(lon) == "-0.0"
