import gzip
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("download", ROOT / "scripts" / "01_download.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

# the listing page as served after the token is posted, with the token replaced
# by REDACTED and trimmed to four countries and two daily files
HTML = (ROOT / "tests" / "fixtures" / "downloads.html").read_text(encoding="utf-8")


@pytest.fixture
def listing():
    return m.parse_listing(HTML)


def test_listing_states_the_export_date(listing):
    assert listing["updated"] == "2026-10-06"


def test_listing_has_one_world_file(listing):
    full = [f for f in listing["files"] if f["type"] == "full"]
    assert full == [
        {"type": "full", "file": "cell_towers.csv.gz", "stated_kb": None, "countries": []}
    ]


def test_listing_merges_mcc_files_shared_by_countries(listing):
    mcc = {f["file"]: f for f in listing["files"] if f["type"] == "mcc"}
    assert sorted(mcc) == [
        "202.csv.gz", "276.csv.gz", "286.csv.gz", "440.csv.gz", "441.csv.gz",
        "651.csv.gz", "655.csv.gz",
    ]  # fmt: skip
    assert mcc["440.csv.gz"]["stated_kb"] == 14705
    assert mcc["440.csv.gz"]["countries"] == ["JP"]
    assert mcc["202.csv.gz"]["countries"] == ["GR"]


def test_listing_keeps_daily_files_with_sizes(listing):
    diff = [(f["file"], f["stated_kb"]) for f in listing["files"] if f["type"] == "diff"]
    assert diff == [
        ("OCID-diff-cell-export-2026-10-06-T000000.csv.gz", 1758),
        ("OCID-diff-cell-export-2026-10-05-T000000.csv.gz", 1961),
    ]


def test_listing_keeps_countries_without_recent_data(listing):
    assert listing["no_recent_data"] == {"374": ["TT"]}
    assert listing["countries"]["JP"] == "Japan"


def test_a_link_to_another_place_is_refused():
    bad = HTML.replace("https://opencellid.org/ocid/downloads?", "https://example.org/x?", 1)
    with pytest.raises(SystemExit, match="example.org"):
        m.parse_listing(bad)


def test_a_page_without_the_world_file_is_refused():
    bad = HTML.replace("type=full", "type=diff")
    with pytest.raises(SystemExit, match="world file"):
        m.parse_listing(bad)


def test_file_url_has_the_documented_form_and_redacts():
    url = m.file_url("full", "cell_towers.csv.gz", "s3cr/t+")
    assert url == (
        "https://opencellid.org/ocid/downloads?token=s3cr%2Ft%2B&type=full&file=cell_towers.csv.gz"
    )
    assert m.redact(url, "s3cr/t+") == (
        "https://opencellid.org/ocid/downloads?token=REDACTED&type=full&file=cell_towers.csv.gz"
    )


def test_redact_covers_raw_and_quoted_token():
    assert m.redact("a s3cr/t+ b s3cr%2Ft%2B", "s3cr/t+") == "a REDACTED b REDACTED"


def test_dest_names():
    assert m.dest_name("full", "cell_towers.csv.gz") == "cell_towers.csv.gz"
    assert m.dest_name("mcc", "440.csv.gz") == "mcc/440.csv.gz"
    assert m.dest_name("diff", "OCID-diff-x.csv.gz") == "diff/OCID-diff-x.csv.gz"
    with pytest.raises(SystemExit):
        m.dest_name("mcc", "../440.csv.gz")


def test_select_defaults_to_the_world_file(listing):
    assert [f["file"] for f in m.select(listing, ["full"], None, None)] == ["cell_towers.csv.gz"]


def test_select_only_and_limit(listing):
    got = m.select(listing, ["mcc"], ["440", "441.csv.gz"], None)
    assert [f["file"] for f in got] == ["440.csv.gz", "441.csv.gz"]
    assert len(m.select(listing, ["mcc", "diff"], None, 3)) == 3


def test_select_unknown_only_stops(listing):
    with pytest.raises(SystemExit, match="999"):
        m.select(listing, ["mcc"], ["999"], None)


def test_estimate_uses_stated_sizes_and_the_mcc_sum_for_the_world_file(listing):
    mcc_total = sum(f["stated_kb"] for f in listing["files"] if f["type"] == "mcc") * 1024
    req, size = m.estimate(m.select(listing, ["full"], None, None), listing)
    assert (req, size) == (1, mcc_total)
    req, size = m.estimate(m.select(listing, ["mcc"], ["440"], None), listing)
    assert (req, size) == (1, 14705 * 1024)


def test_check_start_accepts_gzip():
    m.check_start(gzip.compress(b"radio,mcc\n")[:16], "application/octet-stream")


def test_check_start_stops_on_an_error_document():
    body = json.dumps({"status": "error", "message": "RATE_LIMITED"}).encode()
    with pytest.raises(SystemExit, match="RATE_LIMITED"):
        m.check_start(body, "application/json")
    with pytest.raises(SystemExit, match="INVALID_DATA"):
        m.check_start(b'{"status":"error","message":"INVALID_DATA"}', "text/html")


def test_attempts_today_counts_per_file_and_utc_day():
    log = [
        {"file": "cell_towers.csv.gz", "started_at": "2026-10-06T01:00:00Z"},
        {"file": "cell_towers.csv.gz", "started_at": "2026-10-05T23:00:00Z"},
        {"file": "mcc/440.csv.gz", "started_at": "2026-10-06T02:00:00Z"},
    ]
    assert m.attempts_on(log, "cell_towers.csv.gz", "2026-10-06") == 1
    assert m.attempts_on(log, "mcc/440.csv.gz", "2026-10-06") == 1
    assert m.attempts_on(log, "mcc/441.csv.gz", "2026-10-06") == 0


def test_a_file_matching_its_manifest_is_not_fetched_again(tmp_path):
    p = tmp_path / "cell_towers.csv.gz"
    p.write_bytes(b"abc")
    files = {"cell_towers.csv.gz": {"sha256": m.sha256(p)}}
    assert not m.needs_fetch(files, "cell_towers.csv.gz", p)
    p.write_bytes(b"abd")
    assert m.needs_fetch(files, "cell_towers.csv.gz", p)
    assert m.needs_fetch({}, "x", tmp_path / "x")
