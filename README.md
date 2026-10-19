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

Edit **[`config/workers.yaml`](config/workers.yaml)** on Main to assign work.
Shared provider access, delays and dataset preferences live in
**[`config/settings.yaml`](config/settings.yaml)**.

Run **`main.py`** without arguments on each computer. Main defaults to a monitor
with the browser dashboard; MSI and Headless default to scraping their
assignments without a dashboard. The current plan gives MSI 2021/22 and Headless
2022/23 across the seven configured competitions. Main has no scraping jobs.

Workers publish saved progress hourly after the current match, and at job
start/finish/pause. Main checks Git every two minutes. Code/config changes apply
between saved season jobs with a controlled restart. The coverage grid groups
player stats, events, spatial maps and other data into aligned season columns,
with worker colors for completed and planned portions. Current jobs include
elapsed time, scraped/skipped counts and ETA.

```bash
python main.py                         # Main monitor or this computer's worker role
python main.py --worker-id msi          # First launch on the original MSI data folder
python main.py --worker-id headless     # First launch on Headless
python main.py --with-dashboard         # Optional worker dashboard
python main.py --dashboard              # Read-only view; no Git sync
```

**[Worker setup, identity, dashboard and safe updates](docs/WORKERS.md).**
Local identity stays in ignored `data/identity.json`; raw data, credentials and
browser sessions stay local. Saved checkpoints are reused. Explicit CLI scrape
selections retain the previous behavior; use `--retry` to revisit attempted
assignments through the shared supervisor.

## Choose collection types and see timing

All match collectors can run from **`main.py`**. In worker mode, set
`collection_types` on an assignment in `workers.yaml`. For manual commands and
standalone launchers, this selection in `settings.yaml` provides the defaults:

```yaml
collection:
  types: [combined, whoscored_events, fotmob_players]
  action: auto
  show_timing: true
  sofascore_access: extension
```

This is an excerpt; keep the other settings in the supplied file. Types:

- `combined`: Sofascore match/player data enriched with FotMob and WhoScored.
- `sofascore`: Sofascore only. Choose this **or** `combined`; they share checkpoints.
- `whoscored_events`: standalone WhoScored event responses.
- `fotmob_players`: standalone FotMob player stats with `fmb_` columns and IDs.

For example, use `types: [sofascore]` and `sofascore_access: browser` for a visible
Sofascore run. `sofascore_access` replaces the old name “transport”: it means
HTTP requests, a visible browser, automatic HTTP-to-browser fallback, or the
Chrome extension. The
HTTP, browser, and WhoScored preferences now all live inside `collection`.
Older settings files with top-level `transport`/`whoscored` still load.

After each game, the Run console shows its duration, the estimated remaining
season time, and the estimated remaining time across all selected types,
competitions, and seasons. It starts learning after the first scraped game,
marks estimates provisional until three games are sampled, and uses the most
recent 12 games of the same type/access mode. Cache-only skips and failed
attempts do not train the estimate. A browser fallback learns its own speed.
Future schedules not yet discovered use approximate fixture counts and are
marked **provisional**; known cached fixtures are subtracted. Paused seasons are
reported separately and excluded from further ETA calculations. Extension
verification/connection waits are excluded from speed samples; partial matches do not train the estimate, and recent successful speeds use a median
to limit the effect of one stuck match. ETAs cover
scraping plus estimated season setup, excluding final CSV/dataset builds; the
final elapsed run time includes those builds. Set `show_timing: false` to hide
these additional lines.

```text
[Timing] La Liga 2023-2024 | match 123 complete: 1m 30s | season remaining ~8h 12m | collection remaining ~2d 3h 20m | 12 recent scraped games
```

CLI overrides are also available:

```bash
python main.py --collection-types whoscored_events fotmob_players --competition "La Liga" --season 2023/24 --limit 3
python main.py --collection-types sofascore --sofascore-transport browser
```

`collection.action` applies to each selected type. `auto` starts or resumes;
`start` selects new jobs, and `resume` selects saved jobs. Standalone `status`
reads local cache counts. Standalone `export` rebuilds WhoScored and FotMob CSVs offline; combined/Sofascore `export` builds the packaged
dataset. Standalone output remains in its existing source-specific folders.
Explicit CLI commands or competition/season selections target one season;
running without arguments uses every configured competition and season.

## Standalone scrapers and coverage review

The older shortcuts still work: `whoscored_events.py` for WhoScored events, `fotmob_player_stats.py` for
FotMob-only player stats, and `review_data.py` for separate WhoScored/Sofascore/FotMob
match coverage for every configured season. Both scrapers print the competition,
season, match ID, and team names. FotMob output uses `fmb_` columns and IDs and
keeps unavailable normal-schema columns as NA. All share `config/settings.yaml`.

[Output paths, schema, direct WhoScored stage IDs, and coverage definitions](docs/PROVIDER_COLLECTION.md).

## Collect from your normal Chrome tab

The JavaScript **Soccer Collector** extension can connect your chosen Sofascore
Chrome tab to the Python collector. Run `python -m tools.extension_setup`, then
use **Load unpacked** in Chrome to select the prepared folder it prints. Try one match:

```bash
python main.py --collection-types sofascore --sofascore-transport extension --competition "Premier League" --season 2024/25 --limit 1
```

Python prints a connection code. Open the extension on your Sofascore tab,
paste that code, and click **Connect this Sofascore tab**. Python keeps the
existing checkpoints, schema, dataset building and timing; JavaScript handles
the page. After a 403, the extension makes up to three full page reloads,
after waits of 1 minute, then 5 minutes, then 30 minutes. Each bypasses the
browser cache and confirms a new document before checking access. Verification
finishes naturally; restored access resumes collection, including on the homepage.
After the third unsuccessful attempt it waits for you to click Resume. The popup
shows the attempt/countdown and version; the diagnostic log records each result. Persistent verification stays paused until you use
**Resume** or **Disconnect**, without expiring the collection.
For regular PyCharm runs set `collection.sofascore_access: extension` in YAML.
Completed API checkpoints remain valid when switching transports; saved API
responses are reused before opening the browser. `python main.py --inventory`
refreshes Git-shareable worker records for that older data.

[Installation, first-match check, permissions and limitations](docs/CHROME_EXTENSION.md).

To run every configured competition/season with one extension connection:

```bash
python main.py --collection-types sofascore --build-dataset
```

Omit competition/season/name overrides to use the YAML lists. `python main.py`
also uses the configured collection types (the supplied default is `combined`).

## Combine data from multiple computers

Paste another computer's complete `data` folder into `ensemble_data/data/`, or
use `ensemble_data/<computer>/data/` for several transfers. `main.py` imports
these before scraping: provider raw responses, checkpoints, schedules, coverage
and provenance enter the regular data root. Conflicting originals and machine
state are preserved under `data/ensemble_imports/`; browser profiles/cookies and
safety settings are archived rather than activated on the Mac. Successful
imports are removed from the staging folder only after checksum verification.
Invalid or interrupted transfers stay for review/retry. Stop copying before
starting an import. To import without scraping:

```bash
python main.py --import-only
```

Give each computer a unique identity and inventory its existing data without
scraping. On the main Mac:

```bash
python main.py --inventory --worker-id main
python review_data.py
```

This saves `data/identity.json` and builds
`ensemble_data/workers/main.json` from validated local artifacts. On other
computers use their configured ID (`msi` in the initial plan). Add new IDs to
`config/workers.yaml` before using the shared supervisor. You can also edit the
`worker_id` in `data/identity.json`; keep the other generated fields. That file
travels inside transferred data folders, while imports retain the receiving
computer's identity. Collection origins stay with the imported records.

The worker JSON files are allowed in Git; raw data, identity and logs remain
ignored. Commit/push only your worker's file, then pull other workers' updates
before starting collection:

```bash
git add ensemble_data/workers/main.json
git commit -m "Update main inventory"
git pull --rebase origin main
git push origin main
```

`main.py` reads all shared worker files and updates only its own inventory
after every saved match. Re-run `--inventory` to refresh the snapshot after
transfers or filesystem changes. Inventories describe data held on a worker;
their record `origins` preserve the original collectors. The shared worker
supervisor automatically fetches and publishes snapshots hourly after a saved match;
manual CLI commands do not run Git. The older ignored `ensemble_data/provenance.json`
remains a compatible aggregate map you can manually copy between computers.
Local files are rechecked at startup. Provider namespaces,
competition/season and completed provider/spatial/browser coverage prevent a
WhoScored event ID or a basic HTTP match from standing in for other requested
data. Remote-only work is labeled `elsewhere`; it does not inflate local data
coverage or create fake checkpoints. `review_data.py` reports these counts
separately. Transfer the full folders to the Mac before building its complete
dataset. The shared plan rejects overlapping data-type assignments. Remote status is a
published snapshot; raw data transfers remain separate.

[Transfer-folder instructions](ensemble_data/README.md). `--ensemble-root`
selects another staging folder; `--data-root` selects the receiving data root.
[Worker IDs and Git inventory workflow](ensemble_data/workers/README.md).
Use `--ignore-provenance` to collect missing local data despite remote completion
records, while retaining the usual local cache/checkpoint resume behavior.

## HTTP and visible-browser collection

Sofascore supports `http`, visible `browser`, and `auto` fallback in
`config/settings.yaml`. HTTP sessions use coherent saved browser profiles and
cookies, with optional proxy routing. Run `sofascore_browser.py` for a dedicated
visible-browser run with all spatial categories requested. It handles common
advertising overlays and saves match-by-match coverage and diagnostics.

Install browser support with `python -m pip install ".[browser]"` and use installed
Chrome. [Setup, settings, output coverage, and ad handling](docs/BROWSER_COLLECTION.md).

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
main.py                 Entry point for every match collection type
dataset_builder.py      Export, season packaging, and combined ID tables
config/settings.yaml    Shared provider settings, manual defaults and paths
config/workers.yaml     Shared worker assignments, dashboard and Git updates
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

Results share `data/<competition>/<YYYY-YYYY>/` with the other providers.
Original match payloads are retained under `raw/whoscored/`, wrapped with the
provider, cache key, and fetch timestamp. Provider field names and nested event
objects are preserved. Raw-only collection remains the default. To build CSVs
from saved data, including every event, without network requests:

```bash
python whoscored_events.py export --competition "Champions League" --season 2019/20 --compile-events
```

This writes `whoscored_events.csv`, `whoscored_player_match_stats.csv`,
`whoscored_team_match_stats.csv`, and `whoscored_fixtures.csv` in the season folder. Native IDs have `wsc_` column names.
Player metrics come from named WhoScored event flags; absent categories stay
missing, and nested player/team statistics remain in JSON columns. Add
`--build-dataset` to copy these tables to
`dataset/whoscored/<competition>/<YYYY-YYYY>/`. Set `dataset.tables.events: true`
to compile events by default; an explicit `--compile-events` overrides it.
Export rebuilds every locally saved match even after a limited collection.

Both launchers use the ensemble import, worker inventory and remote-completion
logic. Copy the complete data folder between computers and share worker JSON
inventories as described above. Browser profiles stay machine-specific.
WhoScored event CSV transfers preserve one row per event, and raw response
conflicts retain one exact response with the incoming original archived.

Combined collection and offline dataset builds now reuse standalone WhoScored
schedules and match payloads. Date, team and player identity checks still apply;
WhoScored IDs are never treated as Sofascore IDs. Existing Sofascore checkpoints
can gain WhoScored enrichment during an offline export. Unmatched or ambiguous
identities remain reported rather than being forced into the dataset.

Repeat the command to resume from saved responses. `whoscored_schedule.json`,
`whoscored_job.json`, and `whoscored_failures.json` retain provider discovery and
progress without overwriting the baseline schedule or job.
Existing `data/whoscored/<competition>/<season>/` and
`data/whoscored_events/<competition>/<season>/` folders are copied, validated
and checksum-verified into the shared season tree automatically on startup,
then removed from their old location. Both prefixed and older metadata names
are recognized, including flat `raw/*.json` response caches. Collections missing
their old job file use the league/season path. Raw-only collections migrate
without inventing fixture schedules or claiming complete inventory coverage.
Transfer imports perform the same migration and preserve the original worker;
conflicting originals remain in `data/ensemble_imports/`. Interrupted or changing
sources remain available for retry.

To migrate and rebuild this worker's inventory without scraping or importing
unrelated staged transfers, stop the scraper and run:

```bash
python -m tools.migrate_whoscored
```

Optional `--data-root PATH` and `--ensemble-root PATH` override the configured
folders. This command preserves `config/settings.yaml` and refuses to run while
another collector holds the data lock. Use
`--refresh-schedule` (or `collection.whoscored.refresh_schedule: true` in the config) to discover newly
finished fixtures in an ongoing season. `--limit` selects the first N finished
fixtures; omit it on a subsequent run to collect the rest. The collector uses the
shared request pacing, HTTP cache, and WhoScored cooldown ledger. A WhoScored
refusal pauses collection and retains saved progress.

Choose `collection.whoscored.access: http`, `browser`, `auto`, or `extension`.
`auto` tries HTTP and switches to visible Chrome after a parsing/access failure;
401/404/429 and offline misses do not trigger fallback. Existing YAML files
without `access` retain HTTP behavior. New settings use `auto`.

```bash
python whoscored_events.py --whoscored-transport extension --competition Bundesliga --season 2024/25 --limit 1
python whoscored_events.py --whoscored-transport browser --competition Bundesliga --season 2024/25 --limit 1
```

Visible-browser mode uses the optional `[browser]` install and a separate
WhoScored profile. Extension mode uses Soccer Collector **0.5.0**: run
`python -m tools.extension_setup`, reload the prepared extension in Chrome,
open WhoScored, and paste the connection code into **Connect this tab**.
The extension controls only that selected tab and can switch it between the two
providers during mixed collection. It reads WhoScored's loaded page source and
requests fixture JSON within the same browser session; cookies stay in Chrome.
WhoScored verification waits for the user for `collection.browser.manual_wait_seconds`
and then pauses for a resumable retry. The Sofascore-specific 1/5/30-minute
reload recovery is unchanged. WhoScored HTTP 429 waits apply to browser and
extension collection too; HTTP verification refusals do not impose a browser ban.
The API tester accepts `--whoscored-transport` as well.

WhoScored requests wait a random **15–30 seconds** between request starts by
default. Set `collection.whoscored.request_delay_min_seconds` and
`collection.whoscored.request_delay_max_seconds` in the YAML to adjust this range. Both must
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
