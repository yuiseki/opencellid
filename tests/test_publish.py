import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "publish", Path(__file__).parents[1] / "src" / "publish.py"
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

URL = "https://opencellid.org/ocid/downloads?token=REDACTED&type=full&file=cell_towers.csv.gz"


def _data(tmp_path, card=("2026-10-06",), raw=b"x", manifest_raw=b"x", url=URL):
    d = tmp_path / "data"
    v = "2026-10-06"
    (d / "parquet" / v).mkdir(parents=True)
    (d / "parquet" / v / "cell_towers.parquet").write_bytes(b"PAR1")
    (d / "raw" / v).mkdir(parents=True)
    (d / "raw" / v / "cell_towers.csv.gz").write_bytes(raw)
    files = {"cell_towers.csv.gz": {"sha256": hashlib.sha256(manifest_raw).hexdigest(), "url": url}}
    (d / "raw" / v / "MANIFEST.json").write_text(json.dumps({"files": files}))
    (d / "raw" / "DOWNLOADS.json").write_text("[]")
    (d / "README.md").write_text(
        "---\nconfigs:\n" + "".join(m.subset_entry(c) for c in card) + "---\n"
    )
    for f in ("LICENSE", "provenance.yaml"):
        (d / f).write_text("x")
    return d


def test_collect_lists_card_parquet_and_raw(tmp_path):
    assert m.collect(_data(tmp_path), None) == [
        "README.md", "LICENSE", "provenance.yaml",
        "parquet/2026-10-06/cell_towers.parquet",
        "raw/2026-10-06/cell_towers.csv.gz", "raw/2026-10-06/MANIFEST.json",
    ]  # fmt: skip


def test_a_snapshot_the_card_does_not_declare_stops_it(tmp_path):
    with pytest.raises(SystemExit, match="2026-10-06"):
        m.collect(_data(tmp_path, card=("2026-10-05",)), None)


def test_a_raw_file_unlike_its_manifest_stops_it(tmp_path):
    with pytest.raises(SystemExit, match="raw/2026-10-06/cell_towers.csv.gz"):
        m.collect(_data(tmp_path, raw=b"changed"), None)


def test_a_manifest_url_with_a_token_stops_it(tmp_path):
    d = _data(tmp_path, url=URL.replace("REDACTED", "abc"))
    with pytest.raises(SystemExit, match="not redacted"):
        m.collect(d, None)


def test_a_text_file_holding_the_token_stops_it(tmp_path):
    d = _data(tmp_path)
    (d / "provenance.yaml").write_text("oops s3cret")
    with pytest.raises(SystemExit, match="provenance.yaml"):
        m.collect(d, "s3cret")
