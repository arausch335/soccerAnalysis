# Standalone collection and coverage review

All entry points use `config/settings.yaml` for `paths.data`, competitions, and
seasons. Open the file named below in PyCharm and press Run, or use the equivalent
terminal command. They retain saved responses and stop on authentication,
challenge, or rate-limit responses. They share the existing collection lock,
pacing ledger, and cooldowns. No network settings need to change.

## WhoScored events

Run `whoscored_events.py`. Progress identifies competition, season, match ID and
teams, for example:

```text
WhoScored Premier League 2024-2025 | match 1821095 | Home vs Away: 1/380
```

Responses stay in `data/whoscored_events/<competition>/<YYYY-YYYY>/raw/whoscored/`.
JSON caches, schedules, checkpoints and safety ledgers are explicitly read as
UTF-8. This fixes Windows cp1252 `charmap` errors on UTF-8 names containing byte
0x81 without discarding or replacing characters. Existing UTF-8 caches can be
reused; the fix does not require deleting them. Truly corrupt files are still
reported as errors.

### Skip competition/season HTML discovery

Sofascore competition IDs are shared in `providers/sofascore_ids.py`, ported from
PR #1's pinned ScraperFC reference. ScraperFC is a reference, not a new runtime
dependency. The full-season Sofascore collector already uses the season-index
JSON endpoint; it does not navigate a competition HTML page. FotMob uses its
league JSON fixture feed and direct match-detail JSON requests.

WhoScored normally discovers season and stage IDs from HTML, reusing its HTTP
cache. If you already have verified stage IDs, add an entry under
`whoscored.stage_ids` in the same YAML file. Keys must use the canonical
competition name and full season label, such as `La Liga 2023-2024`; the value
must be a list of positive stage IDs. Use IDs taken from the provider's stage
URL or existing saved discovery data. They are not competition/tournament IDs.
For cups, list every main-competition stage needed for the season. Do not add
qualifying stages unless you intentionally want to collect those fixtures.

Configured IDs skip both competition and season HTML discovery in the normal
collector and the standalone WhoScored collector. They still request the fixture
JSON feed for each stage/month. `--refresh-schedule` updates saved finished
fixtures; omitting verified IDs retains normal cached discovery. Supplying only
some stages will yield an incomplete fixture denominator in the coverage review.

[ScraperFC reference used by the old PR](https://github.com/oseymour/ScraperFC/blob/50f5df9fae4141f91174debdb14cf87fb8ed810a/src/ScraperFC/comps.yaml).

## FotMob-only player_match_stats

Run `fotmob_player_stats.py`:

```bash
python fotmob_player_stats.py
python fotmob_player_stats.py --competition "La Liga" --season 2023/24 --limit 2
python fotmob_player_stats.py --offline
python fotmob_player_stats.py --refresh-schedule
```

Output is isolated at
`data/fotmob_player_stats/<competition>/<YYYY-YYYY>/player_match_stats.csv`.
It is not mixed into the Sofascore-keyed dataset or ID concordances. No WhoScored
or Sofascore client is constructed. It uses the FotMob decoder and stores raw
match details with the incident stream removed, as in the normal workflow.
`--limit` restricts new processing but preserves all previous saved scheduled
matches when rebuilding the CSV. `--offline` rebuilds from saved schedules and
responses. Schedule refresh includes newly finished games.

The shared schema is `collection/playerStatsSchema.py`. Each normal column has a
corresponding standalone column in the same order:

- Provider-dependent fields that FotMob can supply use `fmb_`, for example
  `fmb_minutes_played`, `fmb_passes_attempted`, `fmb_player_name`, `fmb_date`.
- Existing `fotmob_` fields become `fmb_` fields, for example
  `fotmob_rating` becomes `fmb_rating`.
- IDs are `fmb_match_id`, `fmb_player_id`, `fmb_team_id` and `fmb_opponent_id`.
  Values also have an explicit namespace, such as `fmb_4506485`.
  `fmb_opta_id` preserves the external Opta ID value, not a FotMob-native ID.
- Competition and season labels are common. Unsupported columns retain their
  normal names and remain NA: for example `sofascore_rating`, `ball_carries`,
  `average_x`, `average_y`, and `touch_count`.
- A supported column exists even if that particular match does not supply it.
  Blank CSV cells represent NA. Observed zeros stay zero. Unused substitutes keep
  their identity/lineup fields while statistical measurements stay NA.
- `fmb_player_stats_status` distinguishes available stats from a missing player
  stats entry. A saved match does not imply every optional statistic is available.

Both normal and standalone exports now include team/opponent ID columns. Normal
exports keep their original metric names and provider IDs. Native raw schedules
and raw payloads retain provider keys for resumability; `fmb_` is the CSV contract.

## Review saved data completeness

Run `review_data.py`. It reads every competition/season selected in the YAML and
prints separate match coverage for WhoScored events and Sofascore player stats:

```text
La Liga 2023-2024: whoscored events: 68% complete (258/380); sofascore player_match_stats: 75% complete (285/380)
```

This is an illustrative example, not a measurement of your saved data.

```bash
python review_data.py
python review_data.py --data-root /path/to/data --output /path/to/coverage.csv
```

The review is offline and does not modify collection files. Percentages mean
valid saved matches divided by saved finished fixtures. They exclude upcoming,
qualifying and awarded-only fixtures. WhoScored uses its standalone schedule
when available and can use responses from either standalone or normal
collection. Otherwise it audits mapped WhoScored raw events in normal match
checkpoints. Sofascore uses the normal schedule (or a saved Sofascore historical
fixture schedule) and checks lineups/statistics in raw responses or checkpoints.
A partial failure of FotMob/WhoScored enrichment does not make an otherwise saved
Sofascore match incomplete.

Each provider keeps its own ID namespace and denominator. WhoScored events must
have the expected match ID, finished status, a nonempty event stream and, when
using its own schedule, matching date and teams. Sofascore requires both populated
lineups and statistics for played players. Corrupt responses do not count.
These are match coverage checks, not percentages of nonempty cells or a guarantee
that every event or optional metric was supplied by a provider. Refresh schedules
before reviewing: a saved partial schedule can overstate whole-season coverage.
Missing schedules or no finished fixtures produce N/A, never an invented 100%.
The optional CSV includes exact counts, schedule paths, and audit notes.

## Connection identity

The existing HTTP transport already uses persistent sessions and curl_cffi's
Chrome connection profile. It also has randomized request spacing, HTTP caches,
and persistent cooldowns. No header rotation, IP rotation, proxy subscription,
or VPN change is introduced here. A VPN changes the public exit address; a
per-client proxy is another routing option. Neither resolves local text-decoding
errors or guarantees provider access.

For implementation details on header overrides, sessions, and proxy routing,
see the [curl_cffi documentation](https://curl-cffi.readthedocs.io/en/latest/quick_start.html).
