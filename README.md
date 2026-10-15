# soccerAnalysis

Collect football match and player data from Sofascore, FotMob, and WhoScored,
build analysis-ready datasets, and explore the results locally.

## Start in PyCharm

Open the repository root. If you already have an environment on this computer,
select its existing Python interpreter; there is no need to recreate it.
Python 3.12 is recommended. Install the collection dependencies if needed:

```bash
python -m pip install -r requirements.txt
```

Edit **[`config/settings.yaml`](config/settings.yaml)** for both workflows.
Under `collection`, choose `action`, `competitions`, `seasons`, and
`spatial_capture`. Set `build_dataset: true` to package the selected dataset
outputs after collection. Press **Run** on `main.py` in PyCharm; no arguments
are needed.

`auto` starts missing collections and resumes unfinished ones. Checkpoints,
caches, and cooldowns stay under `paths.data`. Completed seasons are skipped
during collection but included when dataset building is enabled.

## One dataset-building step

Run **`dataset_builder.py`** to rebuild all saved seasons without scraping.
There is no separate CSV-building or dataset-manager step. Each build:

1. Re-decodes saved checkpoints and exports the CSVs, including WhoScored events.
2. Packages each season under `dataset/<competition>/<YYYY-YYYY>/` with
   `events.csv`, `player_match_stats.csv`, `team_match_stats.csv`, and `fixtures.csv`.
3. Refreshes combined `player_ids.csv`, `team_ids.csv`, and `match_ids.csv` at the
   dataset root. Unknown provider IDs stay blank.

The builder stages each packaged season before replacing its previous version;
a failed export keeps the previous packaged season. Raw responses and collection
checkpoints are retained. Combined ID tables use all saved collections, even
when you rebuild only selected seasons. `dataset/fixtures.csv`, if present from
the fixture builder, augments the FotMob-keyed team reference; season fixture
tables retain their Sofascore IDs.

The same YAML file has a `dataset` section:

```yaml
dataset:
  collections: []  # Standalone builder: all saved seasons
  tables:
    events: true
    player_match_stats: true
    team_match_stats: true
    fixtures: true
  references:
    player_ids: true
    team_ids: true
    match_ids: true
  maintenance:
    refresh_published_team_ids: false
    standardize_fixture_team_names: false
```

Set each dataset type to `true` or `false`, then press **Run** on
`dataset_builder.py`. Disabled tables already in `dataset/` stay unchanged.
For ID-only builds, set all four `tables` values to `false`.

`dataset.collections` selects seasons for a standalone build; an empty list
builds all saved seasons. Builds from `main.py` use `collection.competitions`
and `collection.seasons`. Both entry points share the `paths` section; relative
paths are resolved from the project root regardless of PyCharm's working directory.

Season fixtures use saved schedules. The separate historical fixture backfill
still has settings in `fixtures/builder.py`. The normal dataset build is offline.
The two optional maintenance settings require the historical
`dataset/fixtures.csv` to exist first; name standardization also requires
`dataset.references.team_ids: true`.

## Organization

```text
main.py                 Collection settings and the everyday workflow
dataset_builder.py      Export, season packaging, and combined ID tables
config/settings.yaml    Shared collection settings, dataset types, and paths
collection/             Collection commands, checkpoints, and CSV exporters
providers/              Provider clients, adapters, identity matching, and safety
fixtures/               Historical fixture-only collection and building
models/                 Match, player, team, and statistics models
storage/                Local persistence
analysis/               Offline analysis, event enrichment, roles, and viewers
tools/                  Explicit maintenance tools
tests/                  Offline regression tests
data/                   Local raw responses, caches, and checkpoints (ignored)
dataset/                Local analysis-ready tables (ignored)
outputs/                Local analysis reports and visualizations (ignored)
```

For fixture-only backfills, edit the settings in `fixtures/builder.py` and run
`python -m fixtures.builder`. It writes the separate historical
`dataset/fixtures.csv`; its default provider is FotMob. The standalone Sofascore
fixture collector is available as `python -m fixtures.collector --help`.

## Check provider access

Run **`api_test.py`** in PyCharm to check Sofascore, WhoScored, and FotMob.
Set the providers and their match IDs under `api_test` in `config/settings.yaml`.
Each provider has its own ID namespace; the default examples are independent
matches, and all paths come from the shared configuration.

```bash
python api_test.py
python api_test.py --providers whoscored fotmob
python api_test.py --providers fotmob --match-id 4506485
```

The tester attempts one live match request per selected provider and prints
`GOOD`, `NOT GOOD` (with the error), or `NOT TESTED` when a cooldown or active
collector prevents a request. It bypasses cached responses, makes no retries,
and continues checking the other providers after one fails. Shared pacing and
cooldowns still apply, including the configured WhoScored delays. WhoScored's
check reads structured event data from its match page; FotMob and Sofascore
use their match-details JSON endpoints.

A successful check confirms access to that match's data, not every endpoint.
Exit codes are 0 when all selected checks pass, 2 for failed or skipped checks,
and 130 for interruption. The Sofascore-only `python main.py probe --match-id
12436870` command remains available. `api_test.py` replaces `sofascore_test.py`.

## WhoScored events without Sofascore

Edit **`config/settings.yaml`**, then run **`whoscored_events.py`** in PyCharm.
It uses `collection.competitions`, `collection.seasons`, and `paths.data` from
the same configuration as the main collector. This standalone collector discovers finished fixtures through WhoScored
and downloads their full event streams. It requires no Sofascore or FotMob
requests, so a Sofascore 403 does not block this workflow.

```bash
python whoscored_events.py --competition Bundesliga --season 2024/25
python whoscored_events.py --competition Bundesliga --season 2024/25 --limit 5
```

Results are saved under `data/whoscored_events/<competition>/<YYYY-YYYY>/`.
Original match payloads are retained under `raw/whoscored/`, wrapped with the
provider, cache key, and fetch timestamp. Provider field names and nested event
objects are preserved; this workflow creates no CSV exports.

Repeat the command to resume from saved responses. `schedule.json`, `job.json`,
and `failures.json` retain discovery and progress information. Use
`--refresh-schedule` (or `whoscored.refresh_schedule: true` in the config) to discover newly
finished fixtures in an ongoing season. `--limit` selects the first N finished
fixtures; omit it on a subsequent run to collect the rest. The collector uses the
shared request pacing, HTTP cache, and WhoScored cooldown ledger. A WhoScored
refusal pauses collection and retains saved progress.

WhoScored requests wait a random **15–30 seconds** between request starts by
default. Set `whoscored.request_delay_min_seconds` and
`whoscored.request_delay_max_seconds` in the YAML to adjust this range. Both must
be positive, and the maximum must be at least the minimum. These settings apply
to WhoScored requests in both collectors. Pacing persists across restarts;
cached responses do not send requests or add delays. HTTP 401, 403, and 429
responses retain the existing cooldown of at least 24 hours and respect a
longer `Retry-After`. Delays reduce request pressure but cannot guarantee access.

Analysis commands replace the former `run_*.py` wrappers:

```bash
python -m analysis.eda --help
python -m analysis.event_layer --help
python -m analysis.role_pipeline --help
python -m analysis.match_visualization --help
```

Install optional analysis dependencies with `python -m pip install -r
requirements-role-analysis.txt`. See [collection details](docs/COLLECTION.md)
for provider fields, checkpoint behavior, and export schemas.

## Existing local projects

Keep your existing `.venv`, `.idea`, `data`, `dataset`, and `outputs` at the
repository root. This layout has no `player_match_information/` wrapper. Update
PyCharm run configurations to the new script paths and use the repository root
as the working directory. Archive old source files before replacing them; do
not move or recreate your environment. Do not run the old and new collectors
against the same data at the same time.

## Verification

```bash
python -m unittest discover -s tests -t . -v
```

Tests use temporary directories and mocked provider responses. They do not run
live scrapes. Provider availability and live pagination still require separate
verification. HTTP refusals and cooldowns remain persistent across runs.
