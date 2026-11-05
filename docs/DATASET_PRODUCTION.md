# Dataset production and coverage

Run `python main.py --package` on MSI to create the local analysis dataset and
publish it for Main. Normal `main.py` collection and background raw imports never
build or publish a dataset. Main continues receiving completed publications.
Restart existing workers once after upgrading so old automatic-build code stops.

Incremental mode is the default. New/changed seasons are exported, unchanged
season exports and ID observations are reused, and a fully unchanged run keeps
its publication. Use `--rebuild` only for an explicit full regeneration.
Successful packaging acknowledges the input revisions actually processed; inputs
received during a build remain pending. Failed builds retain the previous
publication and retry only when you run `--package` again.

Public concordance tables default to Parquet directories:

```text
concordance/fixtures/part-00000.parquet
concordance/fixtures/part-00001.parquet
concordance/match_ids/part-00000.parquet
concordance/team_ids/part-00000.parquet
concordance/player_ids/part-00000.parquet
concordance/metadata/manifest.json
```

Chunks contain at most 50,000 rows. Supporting public metadata tables use the
same directory layout. Private compiler CSVs remain under `metadata/compiled`;
season analysis tables remain CSV under `raw_data`. Old single-file Parquet and
explicit CSV publications remain readable. Use the manifest/R reader to load all
chunks; never assume there is only one part.

Teams from every provider and fixture source enter the canonical identity
registry. The old “FotMob teams” log described a FotMob-keyed legacy reference;
it was not a count of every canonical team. New logs count canonical teams and
show linked/missing IDs separately for each provider. Reep/Alex identities remain
the stable join keys. Missing provider IDs are linkage gaps, not proof that
statistics are missing; having three provider IDs does not prove statistics exist.

Each completed package prints a coverage review and writes `dataset/coverage.json`.
The dashboard’s **Dataset** page (`/dataset`) describes the actual local package:

- Fixture catalogue totals by competition/season.
- Unique matches with actual packaged player/team statistics and their coverage
  against the fixture catalogue, plus row counts.
- Fixture flags reporting saved-stat evidence, shown separately from packaged rows.
- Match/team/player provider ID linkage.
- Unlinked packaged match keys and available statistic fields.

The review is included in MSI publications, so Main shows the same coverage after
receiving the package. It is a packaging review, not certification that all remote
raw data has been archived or incorporated. A fixture-only historical season can
legitimately have zero packaged statistic coverage.

## R handoff

A successful explicit package includes `read_dataset.R`, `read_concordances.R`,
`README.md` and `dataset-manifest.json` in the dataset root. The manifest verifies
every public file and records native ID namespaces for each season table. Main
verifies this handoff before installing it. Copying the dataset folder to a new
R project requires no Python checkout. See [the bundled R guide](R_DATASET.md).

## Canonical IDs and missing schedules

Python adds `match_entity_id`, `team_entity_id`, `opponent_entity_id` and, for
player tables, `player_entity_id` directly to season exports. Season fixture
tables receive home/away canonical team IDs. Native IDs remain unchanged. The
resolver follows all saved provider aliases and registry redirects, independently
of which native ID the public reference lists first. It can recover a missing
team/opponent from that exact fixture’s side, unique exact name or known opposing
team. It does not use global fuzzy names or overwrite contradictory identities.

Every package includes `id-resolution.json` with per-file counts and sample
unresolved/conflicting rows. Coverage review and the Dataset page distinguish
these canonical join gaps from missing optional provider IDs. Unchanged annotated
CSVs retain their contents and timestamps.

A saved job without `config.json` or `schedule.json` is deferred. Other available
seasons can package successfully; any previous export is retained. Deferred
seasons appear in `coverage.json` and remain pending on MSI. A missing schedule
is never represented as a complete zero-match season. When saved inputs arrive,
the next explicit incremental package processes the season.

## Resolve IDs without rebuilding the dataset

```sh
python main.py --resolve-ids
python main.py --resolve-ids --dataset-root /path/to/dataset
```

The command repairs an existing local dataset using its saved concordance,
provider aliases, redirects and fixture evidence. It refreshes `id-resolution.json`,
coverage and the bundled R handoff/checksums. Unchanged files retain their
contents and timestamps. It does not scrape, export seasons, rebuild concordance,
start workers or publish to Main. Missing schedules do not prevent this repair.
When source metadata is absent, the published fixture metadata supplies known
native ID namespaces. Optional `--data-root` supplies saved namespace metadata.

Resolution uses the same package and dataset locks as the normal pipeline. A
failed resolution pass leaves the live files untouched; installation starts only
after validation succeeds. The next `--package` reuses the repaired season
outputs and publishes the verified result. `--package` continues to include ID
resolution automatically.
