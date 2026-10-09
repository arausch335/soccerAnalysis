# Resumable football scraper

Local, resumable collection and export tooling for Sofascore (primary), FotMob,
and WhoScored. Python 3.12 is recommended.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows use `py -3.12 -m venv .venv`, then `.venv\Scripts\Activate.ps1`. Run the remaining commands with `python`.

## Run from main on this device

Run `python main.py` in this repository, or press Run on the PyCharm project's
`main.py`. Set `ACTION`, `COMPETITIONS` and `SEASONS` near the top of that file; no
command line is required. `ACTION = 'auto'` resumes an existing collection or
starts it when it does not exist. Set `ACTION = 'resume'` to require continuation.
Collection uses Sofascore primary, FotMob and WhoScored secondary, with spatial
collection enabled. Set `SOFASCORE_SPATIAL_CAPTURE = 'fast'` to collect only
average positions, or `'all'` to collect heatmaps, rating breakdowns, average
positions, and shot maps. Changing an existing season to `'all'` and resuming
backfills missing spatial responses while reusing everything already cached.
The default lists cover Premier League, Bundesliga, and La Liga for 2024/25 and
2025/26. The batch checks every competition/season combination first, skips clean
completed collections, resumes existing incomplete collections, and then starts
new folders using names such as `bundesliga-2024-25` and `la-liga-2025-26`.
Set `BUILD_CSVS = False` in `main.py` to save checkpoints without rebuilding all
combined CSVs after every collection pass. Set it to `True` when desired, or set
`ACTION = 'export'` to rebuild existing CSVs later without making network requests.
The 2024/2025 collection is:

`data/premier-league/2024-2025`

Historical datasets and retired workflow files are kept outside the live data
root in `outputs/old_data`.

```sh
python main.py
python main.py --limit 1
python main.py status
```

## Collect and continue

```sh
python collect.py start --competition "Premier League" --season 2024/25 --name premier-league-2024-25 --limit 1
python collect.py status --name premier-league-2024-25
python collect.py resume --name premier-league-2024-25 --limit 1
```

`resume` is the continuation function. It reads saved job settings, checkpoints, availability decisions and HTTP caches. Remove `--limit 1` to process the season. `--limit` selects the first N finished fixtures; it is not a per-run match allowance. There is no fixed request-count cap or scheduled batch rest. Failed secondary providers leave usable primary rows and a partial status.

```sh
python collect.py resume --name premier-league-2024-25
python collect.py export --name premier-league-2024-25
python collect.py resume --name premier-league-2024-25 --offline
```

`export` rebuilds CSVs from completed checkpoints with zero HTTP. Offline resume can recover additional work from cached responses; uncached responses stay missing. Ctrl+C preserves previously saved responses and match checkpoints. An interrupted match is reconstructed from its cache on resume. This is a snapshot collector: schedule pages and successful responses are reused indefinitely. It does not automatically poll for new fixtures, corrections, or previously missing endpoints. For a different season use a different `--name` in the **same** data root.

For a direct post-scrape rebuild of every saved season, run `python build_csvs.py`. It is equivalent to
the default collection's `export` command: it makes no network requests, re-decodes
the saved FotMob and WhoScored responses with the current mappings, and regenerates
all analysis CSVs. Pass normal selection arguments for another collection, for
example `python build_csvs.py --name bundesliga-2024-25 --data-root path/to/data`.
Repeat `--name` to rebuild a selected subset.

`build_csvs.py` compiles `events.csv` by default. Use `--no-compile-events` when
you only want the other tables and plan to process the cached JSON directly in
R. Use `--replace-dataset` to atomically replace the corresponding packaged
`dataset/<competition>/<season>/` directory after a successful rebuild; this
mode requires event compilation. `--dataset-root` selects another package root.
Qualification fixtures remain in `schedule.json` and the collection checkpoints,
but are excluded from the relational snapshot and all rebuilt analysis CSVs.

The Python settings at the top of `build_csvs.py` independently control the
top-level `dataset/player_ids.csv`, `dataset/team_ids.csv`, and
`dataset/match_ids.csv` concordances. Set `BUILD_PLAYER_IDS_CSV`,
`BUILD_TEAM_IDS_CSV`, or `BUILD_MATCH_IDS_CSV` to select each table. With
`REFERENCE_CSVS_ONLY = True`, only the selected concordances are rebuilt and all
saved-season export/package steps are skipped.

`--no-spatial` on `start` skips the expensive per-player map calls. `--providers sofascore fotmob` on `start` omits WhoScored. All providers and spatial endpoints are enabled by default, with Sofascore always primary. `probe` tests Sofascore with at most one request (or zero if cached).

## Outputs

The principal flattened CSV is:

`data/premier-league/2024-2025/player_match_stats.csv`

It has one row per player/match, including lineup members who did not play,
conventional statistics, and only three spatial fields: `touch_count`,
`average_x`, and `average_y`. `appeared` distinguishes unused
substitutes. Missing values are not filled with invented zeros. Scalar priority is
Sofascore → FotMob → WhoScored. Attempt/completion pairs are selected together
from one provider, preferring a complete pair to prevent mixing incompatible
denominators.

Supporting files include `spatial_maps.csv`, `passes.csv`, `dribbles.csv`, `shots.csv`,
`events.csv`, `provenance.csv`, `source_differences.csv`, `identities.csv`,
`identity_mismatches.csv`, `data_quality.csv`, `collection.csv`, and
`run_summary.json`. `spatial_maps.csv` is a long-form inventory of available
cached JSON payloads, including heatmaps, average positions, pass maps, dribble
maps, shot maps, and provider event maps. Its `json_file` and `json_path` columns
point to the raw data; unavailable maps are not fabricated. Pass and dribble
exports retain raw coordinates and source outcomes but omit Python-derived
geometry such as signed width, zones, entropy, progression, and danger scores.
Provider identities are matched explicitly. A unique exact-name, reviewed alias,
or provider-ID match is accepted even when shirt numbers disagree. A genuinely
ambiguous or unmatched player is omitted and recorded in
`identity_mismatches.csv`; valid players from the same game remain available.
`fotmob_team_statistics.csv` includes canonical `competition` and
Sofascore-derived `matchweek` values for each home/away team statistic row.
`team_match_results.csv` has one row per team and match for result/form work.
Awarded fixtures use the official score and remain present even when lineup and
detailed-stat endpoints are unavailable; `played_goals_for`, `awarded`, and
`data_status` preserve that distinction. Missing detailed statistics stay missing
rather than being filled with zeroes.

`events.csv` is the WhoScored event stream: one row per event and many rows per
match. It includes clock/period fields, players and teams, action and outcome,
coordinates, related events, shot geometry, cards, common classifications, full
qualifiers, and decoded satisfied-event flags. Raw match responses remain the
authoritative source; newly introduced event fields are retained in
`extra_event_json` until promoted to named columns.

FotMob remains enabled for fixtures, player statistics, team statistics, and
shots. Its incident timeline is deliberately discarded before new match responses
are cached and is not used to infer participation; WhoScored is the event stream.

### What each provider contributes

- **Sofascore (baseline):** season schedule, fixture IDs, dates, rounds, teams,
  scores/statuses, lineups, formations, player identities and baseline player
  statistics. Optional endpoints add average positions, heatmaps, rating
  breakdown pass/dribble records, and shot maps.
- **FotMob:** a second fixture index, player lineup/profile fields, detailed
  player-match statistics, long-form team statistics by period/group, and shots
  including expected-goal fields and locations when supplied. Its incident feed
  is intentionally not retained.
- **WhoScored:** lineups and player totals derived from its flags, plus the raw
  event stream used for `events.csv`: clocks/periods, action type/outcome,
  players/teams, start/end coordinates, related events, cards, shot fields,
  qualifiers, and satisfied-event classifications.

The normalized player table records the selected provider for every metric in
`provenance.csv`; raw cached JSON remains authoritative for fields not promoted
to a CSV column.

### Customizing the CSVs after scraping

- Edit `api/statistics.py` to add or rename normalized Sofascore/FotMob player
  statistics. `SCALARS` and `PAIRS` document the provider keys.
- Edit `api/whoScoredLoader.py` to add player-match totals derived from WhoScored
  event flags.
- Edit the `row` construction in `collection/seasonRunner.py` for profile or
  context columns that are not statistics.
- Edit `collection/whoScoredExports.py` to promote more event details into named
  event-stream columns.

After any of these changes, run `python build_csvs.py`; scraping does not need to
be repeated.

Raw response caches and per-match checkpoints are kept alongside exports. Some public endpoints may be unavailable or may have changed schema. A successful connection to the season directory does not establish that every match/map endpoint works.

Pitch channels are aligned to regulation markings rather than split into equal
fifths: the center spans the width of the six-yard box, each half-space runs
from the six-yard-box edge to the penalty-area edge, and each wing runs from
the penalty-area edge to the touchline. Detailed spatial, pass and dribble
exports also carry 1-30 zone numbers across six lengthwise slices and five
box-aligned lateral channels. Numbers advance from the defensive goal toward
the attacking goal, and from provider y=0 to y=100 within each slice.

## Heat, pass and dribble maps

```sh
python render_maps.py data/premier-league/2024-2025 MATCH_ID PLAYER_ID
```

Use IDs from the flattened CSV. This writes a three-panel PNG under that season's `maps/`, without network activity. It plots actual Sofascore touch coordinates and actual pass/dribble coordinates. WhoScored's identified Pass and TakeOn events supplement missing Sofascore action categories when their coordinates and outcomes are present. FotMob supplements conventional statistics and shots; shot positions are not treated as touch/pass/dribble maps. Blank maps are labeled. Coordinates remain in the provider's frame; no movement trajectories or playing direction are inferred. Derived spatial geometry is not included in the CSV exports.

## Request safeguards

- Original `curl_cffi` direct-session method; one sequential request at a time. The previous browser fallback was removed.
- 8–15 seconds between request starts globally across providers; no parallel map workers. There is no fixed request-count cap or scheduled batch rest. This timing reduces load, not detection guarantees.
- HTTP 401/403/429 stops requests to that provider for at least 24 hours. A longer `Retry-After` wins. Server failures, invalid JSON and network failures pause for at least one hour. No automatic retries in the CLI.
- Cooldowns and request timestamps survive restarts and transfers. One process lock spans all seasons in a data root. Use one data root and only one active device; independent copies cannot coordinate pacing or cooldowns.
- Successful responses, schedule pages and HTTP 404s are cached. Known missing match endpoints are not retried on resume.
- Player heat-map availability flags are respected. Rating maps probe representative starters first. After missing responses across three distinct matches with no observed supported schema, further calls for that map category are **deferred** for the season. This is a conservative sampling heuristic, not proof that all matches lack data. See `capabilities.json`; partial coverage may be missed. These decisions are intentionally retained on resume.
- There is no proxy rotation, browser challenge fallback or automatic cooldown clearing. Moving devices is for portability, not circumventing a refusal.

Sofascore's terms prohibit burdening its servers with automated requests. These conservative defaults cannot guarantee permission, access, or freedom from bans. Provider references: [Sofascore terms](https://www.sofascore.com/sr/terms-and-conditions), [FotMob terms](https://www.fotmob.com/term-of-service). Follow provider access requirements.

The supported collection entry points are `main.py`, `collect.py`, and
`python -m collection`. Low-level client classes remain available for tests and
library integrations; those callers must use the safe clients to get persistent
limits.

## Project layout

- `main.py` resumes the default collection from PyCharm or the terminal.
- `collect.py` provides start, resume, export, status, and probe commands.
- `build_csvs.py` rebuilds every CSV from saved data without making requests.
- `eda.py` creates the offline analysis report and analysis tables.
- `render_maps.py` renders cached player maps.
- `transfer.py` safely packages or restores collection state when needed.
- `migrate_data_layout.py` moves legacy flat season folders into the canonical
  competition/season layout; use `--dry-run` to preview changes.
- `api/`, `collection/`, `models/`, and `storage/` contain the implementation.
- `data/<competition>/<YYYY-YYYY>/` contains each live season collection.
- `outputs/old_data/` is the archive for historical data and retired files.

## Tests

```sh
python -m unittest discover -s tests -q
```

Tests use mocked responses: provider priority, identity joins, missing maps,
checkpoint/resume, persistent pacing, refusal handling, cached 404s,
offline mode, and CSV rebuilding.
