#!/usr/bin/env python3
"""Fetch today's OpenCelliD cell export into data/raw/<YYYY-MM-DD>/.

OpenCelliD (https://opencellid.org, run by Unwired Labs) shows its download
links only after an API access token is posted to its download page,
https://opencellid.org/downloads.php . This script posts the token from the
environment variable OPEN_CELL_ID_ACCESS_TOKEN to that page (one request,
which is a page view, not a download), and reads every link the page lists:

  type=full  cell_towers.csv.gz, the world export ("Database")
  type=mcc   one file per Mobile Country Code, e.g. 440.csv.gz
  type=diff  the daily differential files of the last days

Each link has the form
  https://opencellid.org/ocid/downloads?token=<token>&type=<type>&file=<file>
The token is never printed or saved: URLs are stored and shown with
token=REDACTED, and every message goes through redact().

By default only the world file is fetched (--type full). The exports hold
only cells observed in the last 18 months, and are regenerated every day by
02:00 GMT, so a cell that has not been seen for 18 months drops out of later
files. Each export is therefore kept as its own snapshot: the version
directory is the "Updated:" date the page states for the world file, and
nothing is overwritten.

Limits stated by OpenCelliD: "You can download each file twice per day"
(https://docs.opencellid.org/docs/getting-started/access-and-limits). Every
download attempt is logged in data/raw/DOWNLOADS.json before it starts, and a
file already attempted twice on the same UTC day is refused. HEAD requests are
not used, since it is not stated whether they count. A 429 or 503 or an error
document from the server stops the run; it is not retried. Interrupted
downloads restart from the beginning (a Range request could count as another
download).

data/raw/<version>/MANIFEST.json records the listing (every file the page
offers, with its stated size and countries, without the token) and for each
fetched file its URL (redacted), bytes, sha256, Last-Modified and fetch time.
A file in the manifest whose sha256 still matches is not fetched again.

    set -a; . /path/to/.env; set +a
    uv run python scripts/01_download.py --plan        # show what would be fetched
    uv run python scripts/01_download.py               # the world file
    uv run python scripts/01_download.py --type mcc --only 440 441
"""

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
LOG = RAW / "DOWNLOADS.json"
LISTING_URL = "https://opencellid.org/downloads.php"
DOWNLOAD_URL = "https://opencellid.org/ocid/downloads"
URL_FORM = DOWNLOAD_URL + "?token=REDACTED&type=<type>&file=<file>"
TOKEN_ENV = "OPEN_CELL_ID_ACCESS_TOKEN"
UA = "opencellid/1 (+https://github.com/yuiseki/opencellid)"
TYPES = ("full", "mcc", "diff")
MAX_PER_DAY = 2  # "You can download each file twice per day"
LICENCE = "CC BY-SA 4.0 (https://docs.opencellid.org/docs/attribution)"

_last_request = 0.0

_ROW = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", re.S)
_LINK = re.compile(r'<a href="([^"]+)"[^>]*>[^<]*</a>(?:\s*\((\d+) KB\))?')
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.csv\.gz")


def redact(text: str, token: str | None) -> str:
    if not token:
        return text
    for t in {token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)}:
        text = text.replace(t, "REDACTED")
    return text


def file_url(typ: str, file: str, token: str) -> str:
    return DOWNLOAD_URL + "?" + urllib.parse.urlencode({"token": token, "type": typ, "file": file})


def _parse_link(href: str) -> tuple[str, str]:
    u = urllib.parse.urlparse(htmllib.unescape(href))
    if f"{u.scheme}://{u.netloc}{u.path}" != DOWNLOAD_URL:
        raise SystemExit(f"a download link points elsewhere: {u.scheme}://{u.netloc}{u.path}")
    q = urllib.parse.parse_qs(u.query)
    typ, file = q.get("type", [""])[0], q.get("file", [""])[0]
    if typ not in TYPES or not _FILE.fullmatch(file):
        raise SystemExit(f"unexpected download link: type={typ!r} file={file!r}")
    return typ, file


def parse_listing(page: str) -> dict:
    """Every file the download page offers, with the sizes and countries it states."""
    files: dict[tuple[str, str], dict] = {}

    def add(typ, file, kb, country=None):
        e = files.setdefault(
            (typ, file), {"type": typ, "file": file, "stated_kb": kb, "countries": []}
        )
        if country and country not in e["countries"]:
            e["countries"].append(country)

    countries, no_recent = {}, {}
    table = page[page.find('id="regions"') : page.find("</table>")]
    for name, code, cell in _ROW.findall(table):
        name, code = htmllib.unescape(name.strip()), code.strip()
        countries[code] = name
        for href, kb in _LINK.findall(cell):
            typ, file = _parse_link(href)
            add(typ, file, int(kb) if kb else None, code)
        mt = re.search(r"No recent data for MCCs:\s*([0-9, ]+)", cell)
        if mt:
            for mcc in re.findall(r"[0-9]+", mt.group(1)):
                no_recent.setdefault(mcc, []).append(code)

    world = page[page.find("</table>") :]
    for href, kb in _LINK.findall(world):
        if "/ocid/downloads" in href or "type=" in href:
            typ, file = _parse_link(href)
            add(typ, file, int(kb) if kb else None)
    if not any(t == "full" for t, _ in files):
        raise SystemExit("the download page lists no world file (type=full)")
    mt = re.search(r"Updated:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", world)
    if not mt:
        raise SystemExit("the download page states no 'Updated:' date for the world file")
    order = {t: i for i, t in enumerate(TYPES)}
    return {
        "updated": mt.group(1),
        # MCC files in numeric order, the daily files in the page's order (newest first)
        "files": sorted(
            files.values(),
            key=lambda e: (order[e["type"]], _natural(e["file"]) if e["type"] == "mcc" else []),
        ),
        "countries": countries,
        "no_recent_data": no_recent,
    }


def _natural(file: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", file)]


def dest_name(typ: str, file: str) -> str:
    if not _FILE.fullmatch(file):
        raise SystemExit(f"unsafe file name {file!r}")
    return file if typ == "full" else f"{typ}/{file}"


def select(listing: dict, types: list[str], only: list[str] | None, limit: int | None) -> list:
    picked = [f for f in listing["files"] if f["type"] in types]
    if only:
        names = {o if o.endswith(".csv.gz") else f"{o}.csv.gz" for o in only}
        missing = names - {f["file"] for f in picked}
        if missing:
            raise SystemExit(f"not on the download page: {sorted(missing)}")
        picked = [f for f in picked if f["file"] in names]
    return picked[:limit] if limit else picked


def estimate(todo: list, listing: dict) -> tuple[int, int]:
    """(requests, bytes). The page states no size for the world file; it is
    estimated as the sum of the per-MCC files, which cover the same cells."""
    mcc_sum = sum(f["stated_kb"] or 0 for f in listing["files"] if f["type"] == "mcc") * 1024
    size = sum(f["stated_kb"] * 1024 if f["stated_kb"] else mcc_sum for f in todo)
    return len(todo), size


def check_start(first: bytes, content_type: str) -> None:
    """Stop unless the body starts like a gzip file; quote the server's error if it sent one."""
    if first[:2] == b"\x1f\x8b":
        return
    msg = first[:500].decode("utf-8", "replace")
    try:
        doc = json.loads(first)
        msg = doc.get("message") or doc.get("error") or msg
    except (ValueError, AttributeError):
        pass
    raise SystemExit(f"the server sent {content_type or 'no type'} instead of a gzip file: {msg}")


def attempts_on(log: list, name: str, day: str) -> int:
    return sum(1 for e in log if e["file"] == name and e["started_at"].startswith(day))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def needs_fetch(files: dict, name: str, path: Path) -> bool:
    e = files.get(name)
    return not (e and path.exists() and sha256(path) == e["sha256"])


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open(req: urllib.request.Request):
    global _last_request
    wait = 1.0 - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    try:
        return urllib.request.urlopen(req, timeout=600)
    finally:
        _last_request = time.monotonic()


def get_listing(token: str) -> str:
    data = urllib.parse.urlencode({"token": token}).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            LISTING_URL, data=data, method="POST",
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
        )  # fmt: skip
        try:
            with _open(req) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == 2:
                raise SystemExit(f"{LISTING_URL}: HTTP {e.code}") from None
            time.sleep(60 * (attempt + 1))
    raise AssertionError


def fetch(typ: str, file: str, dest: Path, token: str) -> dict:
    url = file_url(typ, file, token)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        r = _open(req)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{file}: HTTP {e.code}; not retried, to stay within the limit") from None
    with r:
        length = r.headers.get("Content-Length")
        first = r.read(1 << 16)
        check_start(first, r.headers.get("Content-Type", ""))
        got, step = 0, 1 << 30
        with open(tmp, "wb") as f:
            while first:
                f.write(first)
                got += len(first)
                if got >= step:
                    print(f"  {file}: {got:,} bytes", flush=True)
                    step += 1 << 30
                first = r.read(1 << 20)
        headers = {k: r.headers.get(k, "") for k in ("Last-Modified", "Content-Type", "ETag")}
    if length is not None and got != int(length):
        tmp.unlink()
        raise SystemExit(f"{file}: got {got:,} bytes, the server stated {length}")
    tmp.replace(dest)
    return {
        "url": redact(url, token),
        "type": typ,
        "bytes": got,
        "sha256": sha256(dest),
        "last_modified": headers["Last-Modified"],
        "content_type": headers["Content-Type"],
        "etag": headers["ETag"],
        "fetched_at": _now(),
    }


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


def run(a: argparse.Namespace, token: str) -> int:
    listing = parse_listing(get_listing(token))
    version = listing["updated"]
    d = RAW / version
    mpath = d / "MANIFEST.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {"files": {}}
    files = manifest["files"]
    n = {t: sum(f["type"] == t for f in listing["files"]) for t in TYPES}
    print(
        f"listing: export of {version}; {n['full']} world, {n['mcc']} MCC, {n['diff']} daily files"
    )

    todo = [
        f
        for f in select(listing, a.type or ["full"], a.only, a.limit)
        if needs_fetch(files, dest_name(f["type"], f["file"]), d / dest_name(f["type"], f["file"]))
    ]
    req, size = estimate(todo, listing)
    print(f"to fetch: {req} files, about {size / 1e6:,.0f} MB (sizes as the page states them)")
    for f in todo:
        print(f"  {f['type']:4} {f['file']}")
    if a.plan:
        return 0

    d.mkdir(parents=True, exist_ok=True)
    manifest.update(
        {
            "version": version,
            "listing_page": LISTING_URL,
            "url_form": URL_FORM,
            "stated_update": "exports generated daily by 02:00 GMT; cells observed in the last 18 months",
            "stated_limit": "each file can be downloaded twice per day",
            "licence": LICENCE,
            "listing": {
                "listed_at": _now(),
                "files": listing["files"],
                "countries": listing["countries"],
                "no_recent_data": listing["no_recent_data"],
            },
        }
    )
    _write_json(mpath, manifest)

    log = json.loads(LOG.read_text()) if LOG.exists() else []
    for f in todo:
        name = dest_name(f["type"], f["file"])
        today = _now()[:10]
        if attempts_on(log, name, today) >= MAX_PER_DAY:
            raise SystemExit(f"{name}: already attempted {MAX_PER_DAY} times on {today} (UTC)")
        log.append({"file": name, "version": version, "started_at": _now()})
        _write_json(LOG, log)
        entry = fetch(f["type"], f["file"], d / name, token)
        files[name] = entry
        manifest["files"] = dict(sorted(files.items()))
        _write_json(mpath, manifest)
        log[-1]["result"] = f"{entry['bytes']} bytes"
        _write_json(LOG, log)
        print(f"{version}/{name}: {entry['bytes']:,} bytes, {entry['last_modified']}", flush=True)
    print(f"{version}: {len(todo)} fetched")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--type", action="append", choices=TYPES, help="default: full (repeatable)")
    ap.add_argument("--only", nargs="+", help="file names or MCCs, e.g. 440 441")
    ap.add_argument("--limit", type=int, help="fetch at most N files")
    ap.add_argument("--plan", action="store_true", help="read the listing, print the plan, stop")
    a = ap.parse_args()
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"set {TOKEN_ENV} in the environment", file=sys.stderr)
        return 2
    try:
        return run(a, token)
    except SystemExit as e:
        print(redact(str(e.code), token), file=sys.stderr)
        return 1
    except Exception as e:  # no traceback: it could carry a URL
        print(redact(f"{type(e).__name__}: {e}", token), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
