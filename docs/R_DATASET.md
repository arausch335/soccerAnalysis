# Using this dataset in R

Copy this entire `dataset` folder after `python main.py --package` completes.
No Python checkout, `data` directory, worker, Tailscale or Internet connection is
needed to read it. Install the R packages once on your analysis computer:

```r
install.packages(c("arrow", "jsonlite", "digest"))
```

From your R project's working directory:

```r
source("dataset/read_dataset.R")
ds <- read_dataset("dataset")
ds$coverage$summary
ds$tables                         # Available competition / season / table files
fixtures <- ds$concordance$fixtures
player_ids <- ds$concordance$player_ids
players <- read_dataset_table(ds, "player_match_stats",
                              competition = "Premier League", season = "2023-2024")
teams <- read_dataset_table(ds, "team_match_stats",
                            competition = "Premier League", season = "2023-2024")
```

Use the actual copied folder path if you place it elsewhere. Both readers live
inside this folder. The loader verifies every exported file against
`dataset-manifest.json`, checks concordance keys and reads all numbered Parquet
chunks. Seasons load on demand; loading the catalogue does not load every stat
row into memory. A missing table or selection is an error, not an empty result.
The first full verification reads all files and can take a little time.

Python packaging writes canonical match/team/opponent/player ID columns directly
into the season CSVs and records recovery, unresolved and conflict counts in
`id-resolution.json`. Registry aliases and redirects apply to every provider and
season. Team fallback uses only the same fixture’s sides, exact names or its
known opponent. Contradictory provider IDs remain flagged for review.

`read_dataset_table` preserves native IDs as strings, preserves numeric statistics,
combines differing season columns by name, and adds `match_entity_id`,
`player_entity_id`, `team_entity_id` and `opponent_entity_id` where applicable.
These canonical columns join to `entity_id` in the concordance ID tables;
`match_entity_id` joins to the fixture catalogue. Original native IDs are retained.
Historical provider aliases are followed through the saved identity registry.
Native IDs use the table's declared baseline namespace; team statistics use their
row's `provider_team_id`/`provider` for team identity. Missing links remain NA and
produce a warning. The reader preserves Python’s fixture-backed recoveries and
checks them against the canonical ID tables; it does not silently overwrite conflicts. Never join native ID numbers across providers without their
namespace. Team statistics are long format (period/group/field/value); players
are wide format. A team can have multiple rows per match and provider.

`coverage.json` measures actual packaged statistics against the fixture catalogue.
Fixture-only seasons have no player/team stat file unless those exports exist.
A structurally valid package does not certify complete raw-data integration.
The loader warns when MSI integration has not been audited. To require that
separate certification, use `read_dataset("dataset", require_integration = TRUE)`.
The lower-level `read_concordances` reader retains its strict default.

Private rebuild caches under `concordance/metadata/compiled`, `legacy` and
`fixture_baselines` are ignored by the R loader. Public data, readers and review
files are listed by checksum in `dataset-manifest.json`. To verify a copied
folder again, run `verify_dataset("dataset")`. Do not edit packaged files by hand. In the Python project,
`python main.py --resolve-ids` repairs an existing local dataset and refreshes
its audits and R checksums without rebuilding season exports. Use `--package`
when you want to rebuild/publish saved data.

## Complete provider team bridges

`team_ids` has one representative native ID per provider and canonical team. It
is not a complete lookup when a club has multiple accepted provider profiles.
Use the Python-exported alias metadata through the bundled reader instead:

```r
team_bridge <- subset(ds$concordance$team_bridge, provider == "whoscored")
# provider_team_id -> entity_team_id, both character columns
```

The bridge contains every accepted alias, including IDs learned from validated
matched fixture sides. A link learned at home also applies to away appearances.
Python collects both sides before adding links, and rejects conflicting claims,
existing contradictory mappings and explicitly withheld aliases. No global name
matching, hard-coded team ID or new canonical identity is used. Recovery counts
and conflict examples are included in `coverage.json`; recovered evidence is in
`concordance/metadata/team_ids`. The R report no longer needs to infer these links
when it uses `team_bridge`. Rebuild the reference bundle on MSI with
`python main.py --package --references-only` to update an existing dataset without
re-exporting season statistics.

## Player aliases from saved lineups

Reference publication also repairs missing WhoScored player links when saved
Sofascore and WhoScored lineups identify the same player on the same team in a
validated fixture. It prefers a unique normalized name. Where the given name
differs, it requires a shared surname and a shirt number unique on both
lineups. Older fixture rows without a validation label can use an accepted
match alias after checking the raw match date, team sides and known scores.
The bridge reuses the existing canonical player, including bench/event players
without populated statistics. It clears an automatic `ambiguous_provider_link`
hold only if a validated fixture has a confirmed SofaScore lineup, a unique
exact name and matching unique shirt number on the same side, and no competing
lineup or identity evidence. Manual rejections and other holds stay in force.

Recovered links are saved in the exported identity registry and
`metadata/player_ids`; recovery, conflict and skipped evidence counts appear in
`coverage.json`.
Read them through `ds$concordance$resolve_provider_ids("player", "whoscored", ids)`.
The report does not need a player-specific override. Run
`python main.py --package --references-only` on the machine with the saved raw
lineups, copy the refreshed dataset folder, then reload it in R. Missing lineup
evidence stays unresolved and requires identity review.

To inspect a particular missing ID on a machine with the raw cache and bundle,
run `python -m concordance.playerBridge --match-id MATCH_ID --player-id PLAYER_ID`.
This command reports the published link and available evidence for one fixture
without changing the dataset. Packaging checks for competing claims across
fixtures before clearing a hold.



## Transfer raw WhoScored events to another machine

On MSI, package the saved match responses independently of the regular dataset.

```powershell
python main.py --package-raw-events
```

The default `raw_events_dataset/` is next to the configured `dataset/` folder.
It contains the original WhoScored match JSON and a checksum manifest. Files
keep the `<competition>/<season>/raw/whoscored/` layout used by the R report.
The export uses the raw files available on MSI at the time it runs. It does not
scrape missing matches. Add `--competition` or `--season` to limit the export.
Repeat either flag to select multiple values, and use `--output` to choose a
new destination for another export.

```powershell
python main.py --package-raw-events --competition "Premier League" --season 2024-2025 --season 2025-2026 --output raw_events_dataset_pl
```

On the destination machine, copy the regular dataset for fixtures and IDs,
and point the R report's `scraper_data_path` at the raw events folder.

```r
scraper_data_path <- "/path/to/raw_events_dataset"
```

Match the report's `leagues` and `seasons` to the selection you transferred,
and set `generate_matches_reference <- TRUE` for the first run.
