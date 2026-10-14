# Collection reference

## Collect and continue

```sh
python -m collection start --competition "Premier League" --season 2024/25 --name premier-league-2024-25 --limit 1
python -m collection status --name premier-league-2024-25
python -m collection resume --name premier-league-2024-25 --limit 1
```

`resume` is the continuation function. It reads saved job settings, checkpoints, availability decisions and HTTP caches. Remove `--limit 1` to process the season. `--limit` selects the first N finished fixtures; it is not a per-run match allowance. There is no fixed request-count cap or scheduled batch rest. Failed secondary providers leave usable primary rows and a partial status.

```sh
python -m collection resume --name premier-league-2024-25
python -m collection export --name premier-league-2024-25
python -m collection resume --name premier-league-2024-25 --offline
```

`export` rebuilds CSVs from completed checkpoints with zero HTTP. Offline resume can recover additional work from cached responses; uncached responses stay missing. Ctrl+C preserves previously saved responses and match checkpoints. An interrupted match is reconstructed from its cache on resume. This is a snapshot collector: schedule pages and successful responses are reused indefinitely. It does not automatically poll for new fixtures, corrections, or previously missing endpoints. For a different season use a different `--name` in the **same** data root.

Run `python dataset_builder.py` to rebuild and package saved seasons. See the root README for builder settings.

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

- Edit `providers/statistics.py` to add or rename normalized Sofascore/FotMob player
  statistics. `SCALARS` and `PAIRS` document the provider keys.
- Edit `providers/whoScoredLoader.py` to add player-match totals derived from WhoScored
  event flags.
- Edit the `row` construction in `collection/seasonRunner.py` for profile or
  context columns that are not statistics.
- Edit `collection/whoScoredExports.py` to promote more event details into named
  event-stream columns.

After any of these changes, run `python dataset_builder.py`; scraping does not need to
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
python -m analysis.render_maps data/premier-league/2024-2025 MATCH_ID PLAYER_ID
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

## Tests

```sh
python -m unittest discover -s tests -q
```

Tests use mocked responses: provider priority, identity joins, missing maps,
checkpoint/resume, persistent pacing, refusal handling, cached 404s,
offline mode, and CSV rebuilding.
