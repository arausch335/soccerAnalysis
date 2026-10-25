# Historical fixtures and provider concordance

Run `python main.py --fixtures` from the repository directory. `python -m fixtures`
and the installed `soccer-fixtures` command do the same thing. For an IDE run,
use `fixtures/dataset.py`; its defaults select season start year **2010**, ending
with the current season. The old `fixtures/builder.py` script also routes to this
builder; its `build()` function retains the legacy single-provider API.

The scope is Premier League/Championship, La Liga/La Liga 2,
Bundesliga/2. Bundesliga, Serie A/Serie B, Ligue 1/Ligue 2, Champions League,
Europa League, and Conference League. UEFA qualifying/preliminary rounds are
included and labelled. Conference League begins in 2021–2022. Bundesliga
promotion/relegation playoffs remain part of Bundesliga and use FotMob's
supplemental feed when available.

Only schedules are requested: no match details, lineups, statistics or player
event streams. Existing pacing, response caches, provider cooldowns and Chrome
extension fallback apply. `--api-only` disables extension fallback. A blocked
provider is paused for the rest of the run. The existing collector lock prevents
fixture collection from running alongside an active match collector.

## Output

`dataset/fixtures.csv` has one row per canonical Sofascore fixture. Uniquely proven
replacement records are consolidated; their original IDs remain in the attempt audit. Root paths follow
`config/settings.yaml`. Sofascore supplies baseline names, scores and ordinary
`match_id`/`home_team_id`/`away_team_id` fields. Each row also includes:

- `season` as `20xx-20xx`, `competition`, `competition_phase` and any playoff note.
- `date`, `kickoff_utc`, `status`, `awarded`, `matchweek`, `round_name`.
- `home_team`, `away_team`, `home_score`, `away_score`, `winner_code`.
- `venue` and `city` when present in the schedule response.
- Twelve explicit IDs: `{sofascore,fotmob,whoscored,reep}_match_id`,
  `{sofascore,fotmob,whoscored,reep}_home_team_id`, and
  `{sofascore,fotmob,whoscored,reep}_away_team_id`.
- Original provider home/away names, identity statuses and matching methods.
- `whoscored_stage_id`, `whoscored_stage` and `whoscored_round` when a matching
  published stage supplies them.

Future scores and unavailable IDs stay blank. Scores follow the shared final-score rules, including extra time when played. Penalty shootout scores
are not inferred from these fields. Reep supplies canonical IDs; a bridge without
an independently checked provider schedule is labelled `bridge_only`.

Supporting files in `dataset/fixture_concordance/` supplement the existing
match-statistics concordance without overwriting it:

| File | Contents |
| --- | --- |
| `match_ids.csv` | Four-source match IDs and competition/season context |
| `team_ids.csv` | Four-source club IDs, baseline names, competition/season and identity status |
| `attempts.csv` | Superseded/corrected IDs, replacement ID, original date/status/scores and reason |
| `team_aliases.csv` | Observed names/IDs, fixture evidence and matching method |
| `issues.csv` | Ambiguities, contradictory mappings and failed bridge checks |
| `coverage.csv` | Raw/canonical/finished counts, phase/status breakdowns, matching gaps, expected counts, source and errors |
| `manifest.json` | Scope, Reep release/checksums, missing IDs and validation totals |

## Matching

Candidates share the same competition and season. Matching checks the UTC day,
a maximum six-hour kickoff difference, home/away orientation, and known finished
scores. Published Reep links and reviewed IDs/name aliases provide identity
evidence. Contradictory published IDs reject a candidate.

A unique fixture against a known opponent, on the same day with the same final
scoreline, can identify a differently named team. `opponent_date_score` records
that evidence. Score alone does not join two unknown clubs. Swapped sides and
multiple equally plausible fixtures are not resolved by guessing.

Accepted IDs are reused across seasons when unambiguous, including a second pass
over earlier name variants. A fixture-validated historical profile is retained in
its competition/season; a different profile in another season does not erase it.
Competing IDs within one season remain flagged, with ambiguous team IDs blank. A provider match cannot be assigned to
two baseline matches. Published Reep IDs are never invented from name matches.

## Resume and refresh

Every WhoScored season enumerates **all published stages**, including group A–L
and Final Stage in Europa League 2017–2018, groups/finals in the Champions and
Conference Leagues, and league-phase/finals in newer seasons. Ordinary leagues
can expose a single stage through their Fixtures link. Stage-ID settings do not
restrict this full-history inventory. UEFA qualification is a separate published
competition, discovered from tournament options as well as anchors. Separate
German/French playoff directories and second-division playoff stages are included
when published for that season. Missing qualifying/playoff pages are coverage gaps.
Accented and percent-encoded Segunda División routes are supported.

A partially rendered WhoScored directory is refreshed once, and Chrome waits for
its selectors before returning HTML. Independent stages/months continue after a
transient failure; authentication/rate-limit stops still pause the provider.

Complete provider/competition/season schedules are checkpointed under
`data/_system/fixtures/cross_provider/`. Version 2 checkpoints record full-scope
collection. Re-running reuses them. Older checkpoints and ordinary collection
`schedule.json` files have no proof that all stages/qualifiers were fetched: an
online run upgrades them once, reusing valid response caches. Offline exports
retain their fixtures but report partial scope. Failed upgrades preserve the old
checkpoint and available partial results. Do not delete caches to upgrade. Filtering upcoming
fixtures does not truncate checkpoints. Partial responses remain in the shared
request cache. Missing supplemental feeds are reported while retaining available
main fixtures. Failed refreshes retain the last complete checkpoint, marked stale.

```sh
# Restrict scope; repeat --competition for several competitions.
python main.py --fixtures --start-season 2010 --end-season 2025 --competition "Premier League"

# Rebuild from saved schedules/caches, without networking.
python main.py --fixtures --offline

# Refresh only the current season, or every selected schedule.
python main.py --fixtures --refresh-current
python main.py --fixtures --refresh-schedules

# Export only finished fixtures.
python main.py --fixtures --no-include-upcoming

# Refresh public Reep bridges after discovering IDs, then rejoin them.
python main.py --fixtures --refresh-reep
```

Reep refresh is optional; otherwise the saved index is used. Newly discovered
fixture/team IDs are included in future `tools.refresh_reep` runs. The existing
importer verifies bulk checksums and follows published ID redirects.

On Main, refresh the reference index and rejoin the received CSV exports without
scraping providers or rereading raw match data:

```sh
python -m tools.reconcile_match_ids --refresh-reep
# Reuse the installed index, or preview changes without writing exports:
python -m tools.reconcile_match_ids
python -m tools.reconcile_match_ids --dry-run
```

`python -m tools.refresh_reep` also reconciles existing exports. Main applies its
current index automatically after accepting each new compiled fixture delivery.
Workers receive the refreshed compact index through normal reporting. Public
bulk downloads remain temporary; only relevant typed bridges are retained.

This fills unique published match/team/player bridges and follows published
canonical redirects. Existing native IDs and unresolved review conflicts are
preserved. One provider match cannot be newly assigned to multiple baseline
fixtures. Provider schedule matching also allows kickoff differences of up to
six hours across UTC midnight, with the same team, orientation, score and
ambiguity checks as other matches; a date or score alone is insufficient.

New provider IDs recovered from Reep are labelled `bridge_only` until that
provider's fixture is independently checked. They do not make failed schedule
requests, statistics or event streams complete. The reconciliation summary and
remaining conflicts are in `dataset/concordance/reconciliation/`; one previous
copy of changed exports is retained in `data/_system/identity/previous_exports/`.

Superseded postponed, cancelled or abandoned records are removed from project
fixture and match-ID exports when the played replacement is established. A
known replay replaces the original attempt, including an original incorrectly
labelled finished. Cup legs and distinct completed games remain separate; a
shared opponent alone does not establish a replay. The same policy runs during
worker exports and when Main reconciles received legacy CSVs. Removed attempts
are referenced only in `fixture_concordance/attempts.csv` for provenance.

Match scorelines include all goals through extra time and exclude shootout
kicks. Published final totals take precedence over extra-time period fields,
which are inconsistent in some historical Sofascore records. WhoScored's
cumulative AET value is normalized before comparison. Main can
repair old period-only scorelines from saved baseline schedules or compact
verified score evidence in `_system/identity/fixture_score_evidence.json`.
Identity conflicts are cleared only when a corrected baseline and an actual
saved provider fixture agree on teams, date and score. Corrections are recorded
in `dataset/concordance/reconciliation/score_repairs.csv`.

Human-confirmed alternate club profiles are listed by provider ID in
`providers/concordance/team_profiles.json`. They share one project identity,
while source fixtures retain their original provider IDs and names. New club
references prefer the first listed profile; old IDs remain valid aliases.
This does not create a global name alias for similarly named clubs.

Canonical club references use the first ID for each provider throughout exports:
Cesena `2742 / 584022 / 19415`, CSKA Sofia `3275 / 10144 / 1275`, and Farul
Constanța (including Viitorul) `3294 / 210132 / 9425`, in
Sofascore/FotMob/WhoScored order. Generic team IDs/names, reference tables and
packaged analysis CSVs use these choices. Provider-prefixed historical fixture
fields and raw responses retain the original IDs so their provenance is intact.
`concordance/reconciliation/match_urls.csv` lists all pending match comparisons;
their proposed provider URLs are not confirmations of those identities.

The manifest distinguishes schedule-request completion, complete four-source
ID coverage, and independent provider fixture validation. A completed request
does not prove that a provider has every historical match. Exit code 2 signals
missing/partial schedules or failed expected-count checks; available fixtures are still exported with gap reports.
If no baseline schedule is available, an existing dataset is preserved and only
the coverage report is updated. Interruptions retain completed checkpoints.

Once Sofascore's past schedule has finished paginating, a `404` on
`events/next/0` means no upcoming fixtures and does not make that season partial.
Missing past pages, failures on later promised pages, and other HTTP errors
remain coverage failures; their available fixtures are retained.

## Fixture attempts and counts

A postponed/canceled/abandoned league entry is superseded only by a unique active
record with the same teams/orientation, competition/season/phase and compatible
round/leg. Finished replacements can have moved earlier. Cup legs and replays
are retained unless an explicit reviewed correction identifies the replacement.
Unresolved postponements stay in the dataset. Original source schedules are never
modified, and `attempts.csv` keeps the old and replacement native IDs and fixture
fields; raw response bodies are not included in the compiled audit.

Reviewed exceptions cover FotMob's abandoned Bournemouth–Luton match 4193691
(2023–2024), replayed as 4421797, and Sofascore's Blackpool–Wigan opening-day
venue swap 1379596 → 1380091 (2010–2011). FotMob cancellation/postponement/abandonment
reasons take priority over its `finished` flag.

Coverage separates raw entries, canonical fixtures, finished games and superseded
attempts for each provider and phase. Completed Premier League seasons in this
scope must have 380 canonical and 380 finished games. Current seasons do not have
a finished-game requirement. Other leagues are not forced to 380: team counts,
playoffs, formats and shortened seasons differ. The manifest reports request-scope
completion, expected-count checks, missing IDs and independently matched fixtures
separately. A successful download alone does not prove a complete dataset.

## Direct compiled-file delivery to Main

Fixture builds automatically queue `fixtures.csv` and all seven
`fixture_concordance/` files for **worker → Main** delivery. Raw data continues
to use **worker → MSI**. Compiled fixture bundles never enter MSI's raw outbox.

Pull the update and restart Main and the scraping worker. The sender uses the
worker's existing authenticated reporting connection to Main's Tailscale address
and port; no extra port or credential is needed. Main's reporting receiver
accepts only the compiled fixture bundle, verifies sizes/checksums, and installs
it in Main's configured dataset directory before acknowledging success.
Other datasets remain in place. Once a direct fixture publication has arrived,
subsequent MSI dataset polling cannot replace it with an older fixture copy.

If Main is offline or still running older code, the immutable bundle stays under
`data/_system/compiled/outbox/`. The normal worker reporting process retries on
a separate background thread every 30 seconds, independently of reports/logs
and raw uploads. An offline fixture build only queues delivery, without network
requests. `--no-send-to-main` keeps a fixture build local.

To send an already compiled dataset or retry its queue without scraping:

```sh
python -m collection.compiledDelivery
python -m collection.compiledDelivery --retry-only
```

Each publication is the fixture builder's complete exported snapshot, including
its scope and coverage report. A restricted `--competition` build consequently
publishes that restricted snapshot. Only a full-scope build should replace a
full-scope fixture history on Main. Sources, raw responses, request caches and
credentials are never included in a compiled fixture bundle.


The default fixture command initializes one persistent Soccer Collector connection
at startup and shares it across Sofascore, FotMob and WhoScored. The connection
uses the worker's existing `data/_system/config/extension-session.json`, just
like normal collection, and stays alive across competition/season changes.
Connect the printed code once; individual providers use APIs first and reuse
this same connection when they need extension fallback. All extension diagnostics
for this command are written under `_system/extension_diagnostics/fixtures/`.
Offline and `--api-only` runs do not open an extension listener.
Interrupted fixture runs reuse their saved season schedules when rerun with the
same command; do not remove their checkpoints to recover a connection.

WhoScored fixture fallback uses published full competition/season/stage links;
the abbreviated `/stages/<id>/fixtures` route returns a missing-page error.
Secondary leagues are discovered from their country's tournament dropdown.
Schedule HTTP 404s get one extension fallback; a missing page in the extension
remains a coverage failure and is reported promptly. Update Python **and reload
Soccer Collector in Chrome's extensions page** after pulling this change.
Saved connection credentials and completed provider season schedules are retained.
