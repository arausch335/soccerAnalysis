# Provider ID reference

ScraperFC is the reference package for Sofascore ID discovery and endpoint usage.
The [Sofascore documentation](https://scraperfc.readthedocs.io/en/latest/sofascore.html)
describes season lookup, match IDs, lineup player IDs, player match statistics,
average positions and heatmaps.

The configured unique-tournament IDs were checked against
[ScraperFC's competition reference at revision 50f5df9](https://github.com/oseymour/ScraperFC/blob/50f5df9fae4141f91174debdb14cf87fb8ed810a/src/ScraperFC/comps.yaml)
on October 9, 2026. All 13 agree. `api/sofascore_ids.py` is the shared source used
by the player collector and fixture builder. ScraperFC is a reference source,
not an installed runtime dependency; provider requests still use this project's
clients, caches and global pacing.

| Competition | Sofascore unique-tournament ID |
| --- | --- |
| Premier League | 17 |
| Championship | 18 |
| La Liga | 8 |
| La Liga 2 | 54 |
| Bundesliga | 35 |
| 2. Bundesliga | 44 |
| Serie A | 23 |
| Serie B | 53 |
| Ligue 1 | 34 |
| Ligue 2 | 182 |
| Champions League | 7 |
| Europa League | 679 |
| Conference League | 17015 |

## Resolve the other IDs from responses

Use `https://www.sofascore.com/api/v1` as the API base. Keep the namespaces distinct:

| ID | How this project obtains it | API path that uses it |
| --- | --- | --- |
| Competition | Shared unique-tournament reference above | `unique-tournament/{competition_id}/seasons` |
| Season | Match the requested season label to exactly one entry in the season index, then use its `id` | `unique-tournament/{competition_id}/season/{season_id}/events/last/{page}` |
| Match/event | Each fixture's `id`; the match URL's `id` also identifies the match | `event/{match_id}` and `event/{match_id}/lineups` |
| Team | `homeTeam.id` / `awayTeam.id` in the fixture | `team/{team_id}` |
| Player | `player.id` in the match lineup | `player/{player_id}` and `event/{match_id}/player/{player_id}/statistics` |
| Average positions | The match ID; returned positions are joined by player ID and home/away side | `event/{match_id}/average-positions` |
| Heatmap | Match ID and lineup player ID; used only when requested by the capture mode | `event/{match_id}/player/{player_id}/heatmap` |

A season's calendar year is not its Sofascore season ID. Likewise, a player
profile ID does not identify a match. The profile endpoint cannot substitute
for a failed match-statistics endpoint.

## FotMob fallback

FotMob's competition IDs remain in `build_fixtures.py`; they are not Sofascore
IDs and are not supplied by ScraperFC's Sofascore reference. The FotMob client
checks season availability before reading its fixture feed, including the
separate qualifying feeds for UEFA competitions.

On a Sofascore 403, the fixture builder restarts the full selected output using
FotMob and its own IDs. `fixtures.metadata.json` records the actual provider and
checksum. Keep that sidecar with the CSV so concordance generation interprets
IDs correctly. Do not reuse a numeric Sofascore ID as a FotMob ID; cross-provider
links belong in the audited player, team and match concordance CSVs.

The reference check verifies the configured ID values. It does not establish
that every provider season or historical match has the requested data.
