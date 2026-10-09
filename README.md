# Sofascore scrapers

Both the fixture collector and the player match scraper are included. Clone this
private repository on another computer and recreate the Python environment; see
[transfer instructions](docs/PRIVATE_GITHUB.md).

## Player statistics and dataset tools

Use 64-bit Python 3.12 on Windows or macOS (Intel or Apple Silicon). From
the repository root, follow the commands for your operating system in the
[Mac and Windows setup guide](docs/MAC_WINDOWS.md). For macOS:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r player_match_information/requirements.txt
cd player_match_information
```

On Windows, create the environment with `py -3.12 -m venv .venv`. You can
install dependencies and run scripts directly with `.venv\Scripts\python.exe`;
PowerShell activation is optional.

The supported entry points are:

| Script | Purpose |
| --- | --- |
| `main.py` | Start/resume the configured seasons using Sofascore, FotMob and WhoScored |
| `collect.py` | Start, resume, status and offline export for one saved season |
| `build_csvs.py` | Rebuild player CSVs, optional packaged datasets and ID concordances |
| `dataset_manager.py` | Package `player_match_stats.csv` and `fixtures.csv` by league/season |
| `build_fixtures.py` | Build the combined fixture CSV from 2010/11 onward |

The main scraper retains its provider collection, matching, caching and request
pacing. FotMob enrichment remains part of player statistics. The separate
FotMob-only backup collection path has been removed. Visualization, event-layer,
role analysis, and derived spatial/pass/dribble processing have been removed.

`player_match_stats.csv` retains conventional match statistics. The `fast` mode
collects and exports `average_x` and `average_y` from the match's average-position
endpoint, with no heatmap, action-map or shot-map requests. The existing `all`
mode additionally caches those endpoints and exports `touch_count` plus
`heatmap_xy`, a JSON array of original `{x,y}` touch coordinates. Missing values
remain blank.

`build_fixtures.py` starts with Sofascore and switches the whole fixture output
to FotMob after a Sofascore HTTP 403. Its metadata records the actual provider
so IDs remain correctly identified. The player project's Sofascore client uses
`www.sofascore.com/api/v1` and reuses successful caches from its former API host.
The [ID reference](player_match_information/docs/ID_REFERENCE.md) documents the
ScraperFC reference and how competition, season, match and player IDs are resolved.

Edit the Python settings in `main.py`, `build_csvs.py`, and `build_fixtures.py`
before running them. In particular, `build_csvs.py` currently has
`REFERENCE_CSVS_ONLY=True`; set it to `False` to rebuild per-season player CSVs.
The [player project README](player_match_information/README.md) explains settings,
output schemas, safe continuation, and concordance validation.

## Standalone Sofascore fixture collector

The original standard-library fixture command also remains available. From the
repository root, without installing player dependencies:

```sh
python -m sofascore_scrapers.fixtures --output data/fixtures
```

It covers the top two divisions in England, Spain, Germany, Italy and France,
plus the Champions League, Europa League and Conference League. Its cutoff is
January 1, 2010 UTC; Conference League starts in 2021/22. It writes CSV, JSONL,
SQLite checkpoints and a coverage report under the selected output directory.
Requests are serial with 15–25 second gaps, and HTTP 403 stops collection.

```sh
python -m sofascore_scrapers.fixtures --competition 17 --max-requests 3 --output data/trial
python -m sofascore_scrapers.fixtures --output data/fixtures --export-only
```

A one-request cloud check on October 9, 2026 also returned HTTP 403 (`challenge`)
for a fixture on `www.sofascore.com/api/v1`; see the
[compatibility and access check](docs/MAC_WINDOWS.md#verification-and-live-access).
Availability from your computer and historical coverage have not been verified.
Unit tests use saved or mocked payloads and make no provider requests.

## Offline checks

From the repository root:

```sh
python -m unittest discover -s tests -q
cd player_match_information
python -m unittest discover -s tests -q
```

Scraped data, checkpoints, safety state and virtual environments are excluded
from Git. Use the player's `transfer.py` to move its saved collection state
separately when continuing on another computer.
