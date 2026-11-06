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
