import gzip
import importlib.util
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("verify", ROOT / "scripts" / "02_verify.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

SAMPLE = ROOT / "tests" / "fixtures" / "cell_towers_sample.csv.gz"


def test_count_gzip_csv_rows():
    header, n = m.count_gzip_csv(SAMPLE)
    assert header[0] == "radio"
    assert n == 11


def test_a_truncated_gzip_fails(tmp_path):
    p = tmp_path / "t.csv.gz"
    p.write_bytes(SAMPLE.read_bytes()[:-12])
    with pytest.raises((EOFError, OSError)):
        m.count_gzip_csv(p)


def test_typed_follows_the_export_types():
    assert m.typed("radio", "LTE") == "LTE"
    assert m.typed("cell", "23641375123") == 23641375123
    assert m.typed("lon", "-122.8506") == -122.8506
    assert m.typed("unit", "") is None


def test_compare_rows_finds_every_cell_equal(tmp_path):
    out = tmp_path / "c.parquet"
    m.export.export(duckdb.connect(), SAMPLE, out)
    n, diff = m.compare_rows(duckdb.connect(), SAMPLE, out)
    assert (n, diff) == (11, [])


def test_compare_rows_reports_a_difference(tmp_path):
    out = tmp_path / "c.parquet"
    m.export.export(duckdb.connect(), SAMPLE, out)
    lines = gzip.decompress(SAMPLE.read_bytes()).decode().replace("9.0114", "9.0115")
    other = tmp_path / "o.csv.gz"
    other.write_bytes(gzip.compress(lines.encode()))
    n, diff = m.compare_rows(duckdb.connect(), other, out)
    assert n == 11
    assert diff and diff[0][1] == "lon"


def test_manifest_urls_must_be_redacted():
    ok = {"url": "https://opencellid.org/ocid/downloads?token=REDACTED&type=full&file=a.csv.gz"}
    assert m.unredacted({"a": ok}) == []
    bad = {"url": "https://opencellid.org/ocid/downloads?token=abc&type=full&file=a.csv.gz"}
    assert m.unredacted({"a": ok, "b": bad}) == ["b"]
