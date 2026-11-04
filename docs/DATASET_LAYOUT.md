# Dataset export

Run `python main.py --package` from the repository root to build the saved local
dataset. `python dataset_builder.py`, `soccer-dataset`, and
`python -m tools.export_concordances` are compatibility entrypoints to the same
builder. They do not scrape or start workers. Collection's export/build flags use
this builder too; fixture collection and audited ID rebuilds share its publisher.

```text
dataset/
  raw_data/
    <league>/
      <YYYY-YYYY>/
        fixtures.csv
        player_match_stats.csv
        team_match_stats.csv
  concordance/
    fixtures.parquet
    match_ids.parquet
    team_ids.parquet
    player_ids.parquet
    metadata/
      manifest.json
      fixtures.parquet
      fixture_extra.parquet
      match_ids.parquet
      team_ids.parquet
      player_ids.parquet
      registry.parquet
      entities.parquet
      compiled/      # Private linker inputs, coverage, provenance and audit reports
      legacy/        # Previous export layout, archived after a successful replacement
```

Season fixtures describe each saved league/season. The consolidated fixture
catalog includes received fixture history and available local schedules. The
season tables retain their existing CSV schemas and canonical `YYYY-YYYY` season
labels. Flattened event streams are not compiled or copied into the dataset,
even if an older settings file enables `events`.

`concordance/fixtures.parquet` contains match and team entity IDs, date, UTC kickoff,
season, competition, matchweek/round, status, awarded, names, scores, winner code
and `has_player_stats`. Extras such as venue, competition notes and scrape-target
flags live in `metadata/fixture_extra.parquet`; matching evidence and provider IDs
live in `metadata/fixtures.parquet`. These three tables join one-to-one on
`match_entity_id`.

Each ID table has six IDs and five names, in this order:

```text
entity_id, alex_id, reep_id, sofascore_id, fotmob_id, whoscored_id,
entity_name, reep_name, sofascore_name, fotmob_name, whoscored_name
```

Parquet preserves IDs as strings and absent values as nulls. When using CSV,
read IDs as strings and preserve empty fields as missing values. Reep is the
canonical identity where known; otherwise the existing Alex identity remains
stable. Names follow Reep, Sofascore, FotMob, then WhoScored priority. Historical
relationships and validation details stay under metadata and do not imply
provider equivalence.

The manifest records checksums, row counts and integration status. Publication
validates schemas, unique IDs and foreign keys before replacing the previous
concordance directory. Failed writes preserve the previous publication. Source
responses/checkpoints stay in `data/`; the dataset contains analysis exports.
Concordance tables default to Parquet, including the supporting tables directly
under metadata. They are single files compressed with Zstandard. Use `--csv` for
CSV concordance tables, or `--parquet` to explicitly request Parquet. The manifest
records the selected format. Raw season tables remain CSV in either mode.

`metadata/compiled/fixtures.csv` is a private working input with the full provider
IDs and matching evidence. It is used to regenerate the public fixture, ID and
metadata tables. It deliberately retains the CSV import format; it is not a
second analysis export. Other compiler inputs and caches also stay private.

## Incremental updates and full rebuilds

```sh
python main.py --package --incremental   # Default; --incremental can be omitted
python main.py --package --rebuild       # Regenerate all selected saved outputs
python main.py --package --csv           # Incremental build with CSV concordances
python main.py --package --parquet       # Incremental build with Parquet concordances
```

Incremental mode compares saved input paths, sizes, modification times and active
checkpoint generations with the last successful packaging run. It also checks
export presence/modification, code, settings, identity decisions, and received
compiled inputs. New or changed seasons are regenerated as complete CSVs rather
than appended, preventing duplicate rows. Unchanged seasons and their cached ID
observations are reused. Shared concordance tables are recombined when their
inputs change; an entirely unchanged dataset is left untouched. Missing or
modified outputs are repaired. Switching the concordance format republishes all
public concordance and supporting tables, removes the previous format's tables,
and updates the successful-run checkpoint. It reuses unchanged season CSVs and
private ID/fixture inputs. Repeating the same format with unchanged inputs does
no work. A code upgrade can still invalidate older projection caches once.

The default is `dataset.concordance_format: parquet` in `config/settings.yaml`.
This also controls MSI's background builds. CLI format flags override it for
that run. To keep background exports in CSV, set this value to `csv`.

Full rebuild mode ignores these caches, recompiles every selected saved season,
and rebuilds the shared ID tables and fixture catalog from saved inputs. It
preserves the identity registry, received fixture history, archived exports and
provider responses. It does not re-download data. Seasons available only as
received/exported CSVs remain available; rebuilding is not a purge of saved
history. Use `--name` to limit season CSV processing in either mode; the combined
concordance remains global.

Tracking is stored in `concordance/metadata/package-state.json`, with per-source
ID observations in `concordance/metadata/compiled/id_cache`. A failed build does
not advance the successful-run checkpoint, so the next incremental run retries
unfinished work. The first incremental run without a checkpoint processes all
available saved inputs. These are local caches and are not transferred as public
dataset tables. MSI background exports retain them between temporary snapshot
jobs and across restarts, using the original data root as their source identity.

## R import

```r
source("analysis/read_concordances.R")
concordance <- read_concordances("dataset/concordance")
fixtures <- concordance$fixtures
player_ids <- concordance$player_ids
stats <- read.csv("dataset/raw_data/premier-league/2024-2025/player_match_stats.csv")
```

The reader detects the manifest format and requires `jsonlite`, `digest`, plus
`arrow` for Parquet. It checks checksums and joins, and
requires integration confirmation by default. For an explicitly unconfirmed
inspection, pass `require_integration = FALSE`.

## Updating an existing export

Run the same packaging command. Legacy root CSVs, `fixture_concordance`, old
league folders and `concordances` Parquet exports are retained under
`concordance/metadata/legacy` after the replacement validates. Private linker
inputs are retained in `metadata/compiled`. No provider responses are moved or
deleted. Update external consumers to the paths above; paths under metadata are
implementation/supporting data and are not the public analysis interface.
