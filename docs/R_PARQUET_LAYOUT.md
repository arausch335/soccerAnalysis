# R concordance inputs

The public export mirrors the R project's `data/concordances` directory. Python
writes it under `dataset/concordances` by default. All table data is Parquet;
`manifest.json` records the schemas, row counts, source registry version,
checksums and integration status.

```text
data/
  concordances/
    fixtures/
      fixtures/part-00000.parquet
      extra/part-00000.parquet
      metadata/part-00000.parquet
    match_ids/part-00000.parquet
    team_ids/part-00000.parquet
    player_ids/part-00000.parquet
    metadata/
      match_ids/part-00000.parquet
      team_ids/part-00000.parquet
      player_ids/part-00000.parquet
      registry/part-00000.parquet
    entities/part-00000.parquet
    manifest.json
  processed_data/
```

Every table is split into at most 50,000 rows per file by default. Read the whole
folder, not just `part-00000.parquet`. A successful replacement removes old parts;
failed writes retain the previous publication. Each core, extra and fixture
metadata row has the same `match_entity_id`, making their joins one-to-one.

## Fixtures

The main table has exactly these columns, in order:

```text
match_entity_id
date, kickoff_utc, season, competition, matchweek, round_name, status, awarded
home_team_entity_id, home_team, away_team_entity_id, away_team
home_score, away_score, winner_code
has_player_stats
```

IDs are strings, dates are Parquet dates, kickoffs are UTC timestamps, score and
round numbers are nullable integers, and flags are booleans. Unplayed scorelines
remain null. Season labels use `YYYY-YYYY`.

`fixtures/extra` holds `match_entity_id`, venue, city, competition phase/note/type/
country, and `is_scheduled_for_scraping`. `fixtures/metadata` holds the source
provider IDs, fixture checks, matching methods, original labels and remaining
linkage/provenance fields. These do not appear in the main fixture table.

## IDs and names

Each of `match_ids`, `team_ids` and `player_ids` has exactly eleven columns:

```text
entity_id, alex_id, reep_id, sofascore_id, fotmob_id, whoscored_id
entity_name, reep_name, sofascore_name, fotmob_name, whoscored_name
```

`entity_id` uses a published Reep identity where known, otherwise the existing
stable Alex ID. Missing provider/Reep values stay null. `alex_id` preserves the
project alias even when Reep becomes available. Every identity has at least one
real provider ID. Match names use the provider's home/away labels.

The display name priority is Reep, Sofascore, FotMob, then WhoScored. A provider
name is exported only alongside that provider's ID. Names not present in the
saved sources remain null; a Reep ID alone does not supply a Reep name. Matching
continues to use IDs and reviewed fixture evidence, not display-name equality.
Fixture team labels use the selected entity name.

Profile aliases, historical names, contexts, safety flags and validation details
live under `metadata/<kind>_ids`. The Parquet registry snapshot preserves existing
Alex aliases and redirects during delivery to Main. Historical entity links live
under `entities`; they do not imply that predecessor/successor teams are identical.

## Export and read

Normal fixture builds and concordance rebuilds refresh this layout. To convert
already compiled inputs without scraping:

```powershell
python -m pip install -r requirements.txt
python -m tools.export_concordances
```

To write directly into the R project's layout:

```powershell
python -m tools.export_concordances --output "C:\path\to\R-project\data\concordances"
```

Use `--chunk-rows` to change the part size. This conversion uses compiled inputs;
it does not certify MSI integration. The audited saved-statistics rebuild remains
the route to an integration-confirmed publication.

```r
source("analysis/read_concordances.R")
concordances <- read_concordances("data/concordances")
fixtures <- concordances$fixtures
extra <- concordances$extra
match_ids <- concordances$match_ids
team_ids <- concordances$team_ids
player_ids <- concordances$player_ids
```

The reader needs `arrow`, `jsonlite` and `digest`. It verifies checksums, rejects
unlisted chunks, checks ID alignment and joins, and preserves native Parquet
types. The default requires confirmed integration. For an explicitly unconfirmed
inspection, use `require_integration = FALSE`.

Older CSVs outside this public directory remain internal compatibility inputs
for existing scrapers/linkers. They are not the R import interface or part of the
new direct fixture publication.
