# Player match statistics scraper

Resumable Sofascore collection with FotMob and WhoScored enrichment. The main
provider requests, parsers, identity matching and pacing are preserved. The
FotMob-only backup season runner has been removed. This project exports player
match statistics and ID audit tables; it no longer produces visualizations,
event-stream CSVs, role reports, or derived spatial/pass/dribble tables.

## Setup

Use 64-bit Python 3.12 on Windows or macOS (Intel or Apple Silicon). See the
[Mac and Windows setup guide](../docs/MAC_WINDOWS.md) for installation,
PyCharm and data transfer. From this directory on macOS:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, use `py -3.12 -m venv .venv`, then
`.venv\Scripts\python.exe -m pip install -r requirements.txt`. Run scripts
with `.venv\Scripts\python.exe` or optionally activate the environment first.

## Collect and resume

Edit `ACTION`, `COMPETITIONS`, `SEASONS`, `SOFASCORE_SPATIAL_CAPTURE`, and
`BUILD_CSVS` in `main.py`, then run `python main.py` or press Run in PyCharm.
`ACTION='auto'` resumes incomplete collections, skips complete ones, and starts
missing seasons. Your existing competition and season selections are retained.
`BUILD_CSVS=False` saves match checkpoints for a later offline rebuild.

For a single collection:

```sh
python collect.py start --competition "Premier League" --season 2024/25 --name premier-league-2024-25 --limit 1
python collect.py status --name premier-league-2024-25
python collect.py resume --name premier-league-2024-25
python collect.py export --name premier-league-2024-25
```

`--limit N` selects the first N finished fixtures, rather than N additional
matches per run. Sofascore remains primary; FotMob and WhoScored supply
supplementary player statistics. Failed secondary requests leave primary data
available and the match marked partial for continuation. Qualification fixtures
stay in cached schedules/checkpoints but are excluded from player exports and
combined concordances.

The existing capture modes are unchanged:

- `fast`: cache average positions only and export `average_x` / `average_y`.
  It makes one average-position request per match when uncached, with no
  heatmap, action-map or shot-map requests.
- `all`: cache heatmaps, rating breakdowns, average positions and shot maps.
- `--no-spatial`: omit optional spatial acquisition.

To backfill heatmaps on an existing collection while reusing cached data:

```sh
python collect.py resume --name premier-league-2024-25 --sofascore-spatial-capture all
```

Cached rating/action maps and shot maps are retained as raw provider data but
are not processed into separate event, pass, dribble, shot or spatial exports.
WhoScored's existing conventional counters derived from provider flags remain
part of the player statistics scraper.

`export` uses checkpoints and cached responses without making HTTP requests.
`resume --offline` can use cached responses too; uncached responses remain
missing. Successful match responses and season schedules are reused on resume,
so this collector does not automatically poll for newly finished fixtures or
provider corrections. Each competition/season uses its own collection name.

## Player CSV

The principal output is
`data/<competition>/<YYYY-YYYY>/player_match_stats.csv`. It has one row per
player/match, including unused lineup members. `appeared` distinguishes players
who took part. Conventional player statistics, lineup/profile fields, FotMob
fields and completion rates remain available.

Metric priority remains Sofascore → FotMob → WhoScored. Attempt/completion pairs
come from one provider together, preferring a complete pair. Missing values stay
blank; they are not replaced with invented zeros.

The only spatial columns are:

| Column | Meaning |
| --- | --- |
| `average_x`, `average_y` | Provider-reported `averageX` / `averageY` for that player and match; available in `fast` and `all` |
| `touch_count` | Number of points in the cached Sofascore player heatmap |
| `heatmap_xy` | JSON array of original touch coordinates, e.g. `[{"x":10,"y":20}]` |

Average positions are copied directly from the provider's average-position
response, not calculated from heatmaps. Missing players or coordinates stay
blank. Touch coordinates retain the provider's values, order and repeated points.
No centroid, zones, pass geometry or movement trajectory is derived. A
missing/unavailable heatmap has blank values. An observed empty heatmap has
`touch_count=0` and `heatmap_xy=[]`. Invalid coordinates or a cache identity
mismatch stop export rather than silently changing the data.

Supporting files are `identities.csv`, `identity_mismatches.csv`,
`lineup_differences.csv`, `collection.csv` and `run_summary.json`. Raw responses,
match checkpoints, and the relational season snapshot remain available for
resume and ID mapping. Legacy checkpoint snapshot types are still readable.
Older extra export files already on your computer are left on disk; new builds
no longer read or create them.

## CSV and dataset builders

`build_csvs.py` uses Python settings at the top of the file; it has no CLI flags.
The existing settings are retained:

- `REFERENCE_CSVS_ONLY=True` rebuilds only the selected combined concordances.
  Set it to `False` to rebuild saved-season player CSVs.
- `COLLECTION_NAMES=()` selects all saved seasons; a tuple of collection names
  selects a subset.
- `REPLACE_DATASET=True` also replaces each packaged season directory after a
  successful player export. Its default remains `False`.
- `BUILD_PLAYER_IDS_CSV`, `BUILD_TEAM_IDS_CSV`, and `BUILD_MATCH_IDS_CSV` select
  concordances independently.
- `REFRESH_PUBLISHED_TEAM_IDS=True` optionally downloads the published team
  register using FotMob fixtures. It remains `False`, so normal rebuilds are
  offline. Leave it disabled for Sofascore fixture builds.
- `STANDARDIZE_FIXTURE_TEAM_NAMES=True` updates combined fixture display names
  using the rebuilt team reference and the recorded fixture provider IDs.

Run `python build_csvs.py` after configuring those settings. An independent
packaging command is also available:

```sh
python dataset_manager.py --replace
```

Each `dataset/<competition>/<YYYY-YYYY>/` package contains only
`player_match_stats.csv` and `fixtures.csv`. Packaging requires the player CSV,
`config.json` and `schedule.json`; it does not require event or team-statistic
CSVs. `--replace` atomically replaces a season directory after preparing both
files. It does not modify the collection's raw data or match checkpoints.

## Fixtures

Run `python build_fixtures.py` after editing its Python settings. Its default is
`PROVIDER='sofascore'` with `FALLBACK_ON_403=True`. A Sofascore HTTP 403 stops
Sofascore requests and switches the entire selected fixture build to FotMob,
reusing available checkpoints. Completed Sofascore checkpoints are retained,
but the published combined CSV uses only FotMob IDs after fallback. Other errors
do not trigger this switch. FotMob failures stop the build and leave the prior
output intact. Explicit `PROVIDER='fotmob'` remains available.

The saved 403/cooldown state causes subsequent runs to continue on FotMob during
the pause, including any longer `Retry-After`. After the pause expires, the next
run tries Sofascore again. Request pacing stays global across both providers.
This fixture fallback is separate from the removed FotMob-only player backup;
the player match collector still uses Sofascore as its baseline.

Its scope is the top two divisions of England, Spain, Germany, Italy and France,
plus the Champions League, Europa League and Conference League. It starts with
2010/11, except Conference League starts with 2021/22. Current-season refreshing,
upcoming fixtures and qualifying feeds retain their existing settings.

The output is `dataset/fixtures.csv`. `dataset/fixtures.metadata.json` records
the actual provider, requested provider, fallback reason and CSV checksum,
allowing concordance builders to
interpret its match/team IDs correctly. Keep these files together. A mismatch
stops the reference build and asks for a fixture rebuild. Historical combined
fixture CSVs without metadata are interpreted as FotMob, matching the existing
default; rebuild an older Sofascore fixture CSV to attach its correct namespace.

## Provider IDs and API host

ScraperFC is the chosen ID/endpoint reference. The shared competition IDs are in
`api/sofascore_ids.py`, with a pinned reference revision. See
[ID_REFERENCE.md](docs/ID_REFERENCE.md) for the competition list and rules for
finding season, fixture, team and player IDs. No extra scraping package needs
to be installed; the existing clients retain their pacing and checkpoint logic.

The Sofascore client uses `https://www.sofascore.com/api/v1`. Successful HTTP
responses cached under the previous `api.sofascore.com` host remain reusable,
including offline. Negative HTTP caches are specific to their host; provider
cooldowns remain shared and are not cleared by this host change.

## ID concordances

Combined tables are written at the top level of `dataset/`:

| File | Context columns, followed by `sofascore_id`, `fotmob_id`, `whoscored_id` |
| --- | --- |
| `player_ids.csv` | `competition`, `season`, `team_name`, `player_name` |
| `team_ids.csv` | `team_name` |
| `match_ids.csv` | `competition`, `season`, `date`, `home_team_name`, `away_team_name` |

Provider IDs remain exact integer strings. Unknown IDs stay blank. Player team
stints remain separate rows. Cross-season links are backfilled only through
shared known provider IDs with a unique corresponding value, never from a name
alone. Reviewed aliases and corrections remain in place.

The build rejects conflicting ID links in both directions, including rows with
missing third-provider IDs; it does not select an ID by majority vote. It also
rejects reused player IDs under conflicting names and match IDs under conflicting
fixtures. All requested tables are validated before existing concordances are
replaced. Fixture-only teams and matches can remain partial rows rather than
being assigned guessed provider IDs. Missing-ID counts are printed for teams.

## Request safeguards and transfer

Request safeguards are unchanged: one sequential request at a time; global
8–15 second spacing across providers; persistent 401/403/429 cooldowns of at
least 24 hours, extended by longer `Retry-After` values; cached successes and
404s; and one process lock for the data root. Unsupported map categories retain
their existing capability deferrals. These controls reduce request load but do
not establish live availability.

Move code with Git. Move saved player collection state separately:

```sh
python transfer.py pack data collection-state.zip
python transfer.py restore collection-state.zip data
```

Restore only into a destination that does not exist. The archive includes caches,
checkpoints and request/cooldown state. Use one active device and data root at a
time so independent copies do not issue overlapping requests.

## Offline verification

```sh
python -m unittest discover -s tests -q
```

Tests use mocked payloads to cover provider enrichment, heatmap coordinates,
identity conflicts, concordance namespaces, missing values, dataset packaging,
checkpoint/resume, cooldowns, cached 404s and offline export.
