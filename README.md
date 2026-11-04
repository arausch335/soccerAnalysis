# Soccer analysis

## Python environment

From the repository root, create and activate the shared collection, packaging,
and socceraction analysis environment:

```sh
conda env create -f environment.yml
conda activate soccer-analysis
python -m pip check
python main.py --package
```

In PyCharm, select the Python interpreter from the `soccer-analysis` Conda
environment. This is separate from the project's `.venv` and the existing
`soccerAnalysis` Conda environment.

After dependency-file changes, update this environment from the repository root:

```sh
conda env update -f environment.yml
```

`constraints-socceraction.txt` keeps NumPy at 1.26.4, pandas below 3, and
PyArrow at 25.0.1 so Parquet packaging and socceraction can share one environment.
The environment installs both requirements files and the project in editable mode.
Keep these constraints when installing additional packages:

```sh
python -m pip install -c constraints-socceraction.txt PACKAGE_NAME
```

`requirements-tested.txt` records a different environment with NumPy 2 and
pandas 3; do not install it into this socceraction environment.

Historical fixtures from 2010–2011 onward: `python main.py --fixtures`.
This builds a Sofascore-led dataset with FotMob, WhoScored and Reep match/team
IDs, scores, season/competition context and matching evidence. See
[fixture dataset instructions](docs/FIXTURES.md) for scope, resume and refresh.

Package the data already saved on this computer for R:

```sh
python main.py --package
```

Dataset production is explicit: normal `main.py` collection, imports and the MSI
receiver never build or publish a dataset. Run `--package` on MSI to build and
publish its saved data for Main. Main downloads published datasets automatically.
The default is incremental: only new or changed saved seasons are processed.
To explicitly select either mode:

```sh
python main.py --package --incremental
python main.py --package --rebuild
python main.py --package --csv       # CSV concordances instead of Parquet
```

`--rebuild` ignores packaging and ID-projection caches and regenerates every
selected saved season plus the combined concordance. Both modes use local
saved inputs and preserve stable IDs and received fixture history. Concordance
tables default to Parquet. `--csv` selects CSV; `--parquet` explicitly selects
Parquet. Switching format during an incremental build republishes concordance
without rebuilding unchanged season CSVs. Set `dataset.concordance_format` in
`config/settings.yaml` to choose the default for explicit packaging.

This uses one saved-data builder for season CSVs and consolidated fixture/ID Parquet tables:

```text
dataset/
  raw_data/<league>/<season>/
    fixtures.csv
    player_match_stats.csv
    team_match_stats.csv
  concordance/
    fixtures/part-00000.parquet
    match_ids/part-00000.parquet
    team_ids/part-00000.parquet
    player_ids/part-00000.parquet
    metadata/
```

Flattened events are never exported. All linkage details, registry snapshots,
checksums, integration status and private compiler inputs stay under
`concordance/metadata`. Its `compiled/fixtures.csv` is the private, full-width
working input used to regenerate the public tables; it stays CSV for imports.
Existing exports are archived there after a successful
replacement. Provider responses and checkpoints remain under `data/`.

The command runs locally without scraping, starting workers, opening the
extension or downloading identity mappings. Paths and table choices come from
`config/settings.yaml`; `--data-root`, `--dataset-root`, and repeatable `--name`
provide overrides. For example:

```sh
python main.py --package --dataset-root /path/to/R-project/data
```

`python dataset_builder.py`, `soccer-dataset`, and the older
`python -m tools.export_concordances` command delegate to the same builder.
Fixture collection saves inputs for the next explicit package.
Each Parquet table is a folder with numbered chunks of at most 50,000 rows.
Packaging prints a coverage review, saves `dataset/coverage.json`, and makes it
available on the dashboard’s **Dataset** page (`/dataset`). It also includes its
R readers, usage guide and file-checksum manifest: copy only `dataset/`, then
`source("dataset/read_dataset.R")` and `ds <- read_dataset("dataset")`. See
[dataset production and coverage](docs/DATASET_PRODUCTION.md).

See [dataset layout and R import](docs/DATASET_LAYOUT.md). Packaging does not
certify MSI integration; [audit and rebuild concordances](docs/CONCORDANCES.md)
when an integration-confirmed publication is required.

Collection uses one workflow: **combined**. Each provider starts with its API;
access or response failures switch that provider to **Soccer Collector**, the
Chrome extension. Sofascore, FotMob and WhoScored share one selected tab and one
local connection. No Playwright browser is launched. Actual provider rate limits
are honored across both access methods; missing resources stay missing rather
than repeatedly opening the extension.

## Run a worker

Install the project in the worker's Python environment:

```sh
python -m pip install -e .
python -m tools.extension_setup
python main.py --worker msi
```

Load the prepared `build/soccer-collector-extension` folder at
`chrome://extensions`. After an update, prepare it again and click **Reload**.
Version 0.6.0 adds FotMob access permission; Chrome must accept that permission.
The connection code prints immediately at collection startup. Open one of the
three providers in Chrome and connect the extension. It waits while APIs work,
then switches the chosen tab to the provider that needs it.

Main normally runs `python main.py --monitor` to receive progress and logs.
Workers run without a dashboard; `--with-dashboard` enables one if needed.
Set assignments and roles in `config/workers.yaml`, and shared pacing, paths,
season defaults and dataset choices in `config/settings.yaml`. Collection types
must be `[combined]`. Extension spacing defaults to 0.25–0.5 seconds. API pacing is
configured separately.

Manual collection and saved-data tools:

```sh
python main.py --competition "Premier League" --season 2021/22
python main.py status --competition "Premier League" --season 2021/22
python main.py export --competition "Premier League" --season 2021/22
python main.py --inventory
```

## Storage

League folders stay directly under `data/`. Everything operational lives in
`data/_system/`; see [the review and proposed structure](docs/WORKFLOW_REVIEW.md).
Stop the old Main/worker process before starting the new version. Startup moves
known local state into `_system` and preserves the worker ID, reporting token,
queued logs, checkpoints and raw data. Conflicting destinations stop migration
for inspection. A running old process prevents migration.

MSI is the central raw archive (`100.116.229.51:8767`). Headless uploads saved
matches in the background; MSI incorporates completed seasons and publishes
datasets and concordance to Main. Main remains the monitor and downloads no raw
files. See [worker setup and transfer behavior](docs/WORKERS.md#msi-raw-archive-and-dataset-delivery).

Season `raw/<provider>/` files are authoritative. A single compressed request
cache supports interrupted requests, with a 256 MiB target across providers.
A successful raw save releases its redundant temporary response. Historical
transport caches remain readable during transition.

Preview cache cleanup, then apply while the collector/monitor is stopped:

```sh
python -m tools.compact_cache
python -m tools.compact_cache --apply
```

Cleanup removes old match-response copies only when their contents match a
saved season raw response. Unique responses are compressed into a recoverable
area and stay available to the collector. It does not delete league raw data,
checkpoints, exported tables or analysis Parquet files.

## Code

- `collection/`: combined seasons, saved progress, worker scheduling, reporting.
- `providers/`: provider API parsing, loaders, identity and team concordance.
- `providers/transports/`: API-to-extension fallback and the shared bridge.
- `chrome_extension/`: selected-tab collection and manual verification.
- `storage/`: atomic files, bounded response cache, layout migration.
- `dashboard/`: coverage, live jobs and worker logs.
- `tools/`: extension preparation, upgrades and data maintenance.
- `dataset_builder.py`, `analysis/`, `models/`: saved-data exports and analysis.

Reviewed team-name variants belong in `providers/concordance/teams.json`; matching
still checks fixture time and home/away sides. The concordance includes
WhoScored's `Norwich` and Sofascore's `Norwich City`.

