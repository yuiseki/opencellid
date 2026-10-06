# opencellid

A reproducible collection of the world export of OpenCelliD
(https://opencellid.org), the open database of mobile network cells run by
Unwired Labs: one row per cell (GSM, UMTS, LTE, NR, CDMA) with its
identifiers and its estimated position. Each fetch is kept as a dated
snapshot, as served, and converted to GeoParquet.

- Download page: <https://opencellid.org/downloads.php>
- Documentation: <https://docs.opencellid.org/docs/downloads/overview>,
  format <https://docs.opencellid.org/docs/downloads/database-format>
- Dataset on the Hub: <https://huggingface.co/datasets/yuiseki/opencellid>

## Licence

OpenCelliD data is licensed under CC BY-SA 4.0, including the downloads
(<https://docs.opencellid.org/docs/attribution>). Credit it as "Cell tower
data from OpenCellID (https://opencellid.org), licensed under CC BY-SA 4.0
(https://creativecommons.org/licenses/by-sa/4.0/)." and note your changes.
The code here is MIT (`LICENSE`).

## What is fetched, and why snapshots

The download page shows its links only after an API access token is posted
to it. It lists the world file `cell_towers.csv.gz`, one file per Mobile
Country Code, and the daily differential files of the last week. The links
have the form

```
https://opencellid.org/ocid/downloads?token=<token>&type=<full|mcc|diff>&file=<file>
```

By default only the world file is fetched. OpenCelliD regenerates the
exports every day by 02:00 GMT, and they hold only the cells observed in the
last 18 months, so cells drop out of later files. No export is overwritten:
each is filed under `data/raw/<YYYY-MM-DD>/`, the "Updated:" date the page
states, and the snapshots accumulate. The per-MCC and daily files are
recorded in the manifest's listing and can be fetched with `--type mcc` or
`--type diff`, but are not part of the default run (the world file holds the
same cells).

OpenCelliD allows each file to be downloaded twice per day. The downloader
logs every attempt in `data/raw/DOWNLOADS.json` before it starts and refuses
a third attempt on the same UTC day. It sends no HEAD requests, does not
retry a failed download, and stops on an error document from the server.
An interrupted download starts again from the beginning on the next run.

## How to run

The token is read from the environment variable `OPEN_CELL_ID_ACCESS_TOKEN`
only. It is never printed or written: URLs are stored as `token=REDACTED`.

```sh
set -a; . /path/to/.env; set +a                  # provides OPEN_CELL_ID_ACCESS_TOKEN
uv run python scripts/01_download.py --plan      # read the listing, print what would be fetched
uv run python scripts/01_download.py             # today's world file -> data/raw/<date>/
uv run python scripts/03_export_parquet.py       # -> data/parquet/<date>/cell_towers.parquet
uv run python scripts/02_verify.py               # raw against MANIFEST.json, Parquet against the CSV
uv run python -m pytest                          # unit tests
uv run python src/publish.py                     # dry run: the files that would go to the Hub
uv run python src/publish.py --push              # upload to huggingface.co/datasets/yuiseki/opencellid
```

A smaller slice: `--type mcc --only 440 441` (Japan's per-country files),
`--type diff --limit 1`. `--plan` prints the number of requests and the
stated size before anything is fetched.

The full collection is this run once a day, some time after 02:00 GMT:
one POST to the download page and one GET of about 120 MB, about a minute.
Run it again after an interruption: a file whose manifest entry matches its
sha256 is skipped, and the manifest is written after each file.

`03_export_parquet.py` checks that the header is the 14 documented columns,
types every column by name with a regular expression guard before each
cast, range-checks lon and lat, and adds a WKB point geometry with
GeoParquet metadata. `02_verify.py` re-hashes the raw files, reads each gzip
to its end, counts rows with the csv module, and compares every cell and
geometry of the Parquet with the CSV.

## Layout

```
scripts/01_download.py         post the token to the download page, read the listing, fetch, write MANIFEST.json
scripts/03_export_parquet.py   cell_towers.csv.gz -> GeoParquet
scripts/02_verify.py           raw files against MANIFEST.json, Parquet against the CSV
src/publish.py                 upload to the Hugging Face Hub (dry run by default)
tests/                         unit tests; tests/fixtures/ holds a trimmed listing page (token
                               replaced by REDACTED) and eleven real rows of the world export
data/README.md                 the dataset card (tracked)
data/LICENSE                   licence, credit and what the Parquet changes (tracked)
data/provenance.yaml           source, files, counts, checks, modifications (tracked)
data/raw/<date>/               cell_towers.csv.gz as served, plus MANIFEST.json (not in git)
data/raw/DOWNLOADS.json        local log of download attempts (not in git, not uploaded)
data/parquet/<date>/           cell_towers.parquet (not in git)
```

`MANIFEST.json` records, for each fetched file, its URL (redacted), bytes,
sha256, Last-Modified, ETag and fetch time; and for the snapshot, the URL
form, the stated update schedule and limit, the licence, and the full
listing of the download page: every file with its stated size and the
countries it is listed under, and the MCCs the page marks "No recent data".
