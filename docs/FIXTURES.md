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

`dataset/fixtures.csv` has one row per Sofascore fixture. Root paths follow
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

Future scores and unavailable IDs stay blank. Scores follow the existing fixture
export's Sofascore display/normal-time/current preference. Penalty shootout scores
are not inferred from these fields. Reep supplies canonical IDs; a bridge without
an independently checked provider schedule is labelled `bridge_only`.

Supporting files in `dataset/fixture_concordance/` supplement the existing
match-statistics concordance without overwriting it:

| File | Contents |
| --- | --- |
| `match_ids.csv` | Four-source match IDs and competition/season context |
| `team_ids.csv` | Four-source club IDs, baseline names and identity status |
| `team_aliases.csv` | Observed names/IDs, fixture evidence and matching method |
| `issues.csv` | Ambiguities, contradictory mappings and failed bridge checks |
| `coverage.csv` | Competition/season/provider, counts, source and errors |
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

Accepted IDs are reused across seasons, including a second pass over earlier
name variants. One-to-many or many-to-one club mappings are flagged and their
ambiguous provider team ID remains blank. A provider match cannot be assigned to
two baseline matches. Published Reep IDs are never invented from name matches.

## Resume and refresh

Complete provider/competition/season schedules are checkpointed under
`data/_system/fixtures/cross_provider/`. Re-running reuses them. Filtering upcoming
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

The manifest distinguishes schedule-request completion, complete four-source
ID coverage, and independent provider fixture validation. A completed request
does not prove that a provider has every historical match. Exit code 2 signals
missing/partial schedules; available fixtures are still exported with gap reports.
If no baseline schedule is available, an existing dataset is preserved and only
the coverage report is updated. Interruptions retain completed checkpoints.

Once Sofascore's past schedule has finished paginating, a `404` on
`events/next/0` means no upcoming fixtures and does not make that season partial.
Missing past pages, failures on later promised pages, and other HTTP errors
remain coverage failures; their available fixtures are retained.

## Direct compiled-file delivery to Main

Fixture builds automatically queue `fixtures.csv` and the six
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
