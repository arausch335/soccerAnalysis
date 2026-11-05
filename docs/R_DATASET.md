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
folder again, run `verify_dataset("dataset")`. Do not edit packaged files;
regenerate or replace the package when data changes.
