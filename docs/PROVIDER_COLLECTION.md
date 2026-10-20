# Provider data in combined collection

Use [the combined collection workflow](COLLECTION.md). All providers try their
APIs first, then use the single connected Chrome extension. Standalone provider
launchers and native browser control have been removed.

Raw provider responses belong under `data/<league>/<season>/raw/<provider>/`.
WhoScored supplies events and player statistics, FotMob enriches player/match
statistics, and Sofascore supplies baseline identities, statistics and maps.
Provider IDs stay namespaced and team/player identity checks run before merging.

Historical standalone data is still readable by offline migration/import tools.
`python -m tools.migrate_whoscored` moves older WhoScored folders into the season
raw layout. Existing data is preserved when a conflict needs review.
