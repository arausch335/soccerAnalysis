# Soccer analysis

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
must be `[combined]`. Extension spacing defaults to 0.5–2 seconds. API pacing is
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
