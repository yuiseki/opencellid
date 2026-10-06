#!/usr/bin/env python3
"""Push the card, the licence, the provenance, the GeoParquet and the raw files to the Hub.

Every raw file goes up unchanged after its sha256 is checked against
data/raw/<YYYY-MM-DD>/MANIFEST.json, and the card must declare every Parquet
file under data/parquet/ as a subset named after its snapshot date. A new
snapshot is added beside the others; none is replaced.

The manifests must hold only redacted URLs (token=REDACTED). If
OPEN_CELL_ID_ACCESS_TOKEN is set, the text files to upload are also searched
for its value, and the run stops if it is found. data/raw/DOWNLOADS.json
(the local log of download attempts) is not uploaded.

    uv run python src/publish.py             # dry run: list the files and their total size
    uv run python src/publish.py --push
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPO = "yuiseki/opencellid"
TABLE = "cell_towers"
REDACTED_PREFIX = "https://opencellid.org/ocid/downloads?token=REDACTED&"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def subset_entry(version: str) -> str:
    return f"- config_name: '{version}'\n  data_files: parquet/{version}/{TABLE}.parquet\n"


def collect(data: Path, token: str | None) -> list[str]:
    """The files to upload, relative to data/; stops on anything the card or a manifest refutes."""
    card = (data / "README.md").read_text(encoding="utf-8")
    files = ["README.md", "LICENSE", "provenance.yaml"]
    parquet = sorted((data / "parquet").glob(f"*/{TABLE}.parquet"))
    if not parquet:
        raise SystemExit("no Parquet under data/parquet/; run scripts/03_export_parquet.py")
    for p in parquet:
        if subset_entry(p.parent.name) not in card:
            raise SystemExit(f"the card does not declare the subset {p.parent.name}")
        files.append(p.relative_to(data).as_posix())
    for mpath in sorted((data / "raw").glob("*/MANIFEST.json")):
        d = mpath.parent
        for name, e in json.loads(mpath.read_text())["files"].items():
            if not e.get("url", "").startswith(REDACTED_PREFIX):
                raise SystemExit(f"raw/{d.name}/MANIFEST.json: URL of {name} is not redacted")
            if sha256(d / name) != e["sha256"]:
                raise SystemExit(f"raw/{d.name}/{name} is not the file in its manifest")
            files.append(f"raw/{d.name}/{name}")
        files.append(f"raw/{d.name}/MANIFEST.json")
    if token:
        for f in files:
            if not f.endswith((".gz", ".parquet")) and token in (data / f).read_text("utf-8"):
                raise SystemExit(f"{f} holds the access token")
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()

    files = collect(DATA, os.environ.get("OPEN_CELL_ID_ACCESS_TOKEN", "").strip() or None)
    total = 0
    for f in files:
        size = (DATA / f).stat().st_size
        total += size
        print(f"  {size:>12,}  {f}")
    print(f"{a.repo}\n  {len(files)} files, {total:,} bytes ({total / 1e6:.1f} MB)")
    if not a.push:
        print("\ndry run. pass --push to upload")
        return 0

    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(a.repo, repo_type="dataset", exist_ok=True, private=False)
    api.upload_folder(
        folder_path=str(DATA), repo_id=a.repo, repo_type="dataset", allow_patterns=files
    )
    print(f"\npushed to https://huggingface.co/datasets/{a.repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
