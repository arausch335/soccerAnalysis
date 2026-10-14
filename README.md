# soccerAnalysis

Collect football match and player data from Sofascore, FotMob, and WhoScored,
build analysis-ready datasets, and explore the results locally.

## Start in PyCharm

Open the repository root. If you already have an environment on this computer,
select its existing Python interpreter; there is no need to recreate it.
Python 3.12 is recommended. Install the collection dependencies if needed:

```bash
python -m pip install -r requirements.txt
```

Open **`main.py`** and edit the settings at the top:

- `ACTION`: `auto` starts missing collections and resumes unfinished ones;
  `resume`, `status`, and `export` select a specific workflow.
- `COMPETITIONS` and `SEASONS`: select the competitions and seasons to process.
- `SOFASCORE_SPATIAL_CAPTURE`: `fast` collects average positions; `all` collects
  every supported spatial endpoint.
- **`BUILD_DATASET`**: set `True` to rebuild CSVs from saved checkpoints and put
  the resulting tables into `dataset/` after the collection batch. `False`
  saves checkpoints without rebuilding the dataset.

Press **Run**. Collection checkpoints, cached responses, provider cooldowns,
and progress stay under `data/`. Completed seasons are skipped during collection
but are still included when dataset building is enabled.

## One dataset-building step

Run **`dataset_builder.py`** to rebuild all saved seasons without scraping.
There is no separate CSV-building or dataset-manager step. Each build:

1. Re-decodes saved checkpoints and exports the CSVs, including WhoScored events.
2. Packages each season under `dataset/<competition>/<YYYY-YYYY>/` with
   `events.csv`, `player_match_stats.csv`, `team_match_stats.csv`, and `fixtures.csv`.
3. Refreshes combined `player_ids.csv`, `team_ids.csv`, and `match_ids.csv` at the
   dataset root. Unknown provider IDs stay blank.

The builder stages each packaged season before replacing its previous version;
a failed export keeps the previous packaged season. Raw responses and collection
checkpoints are retained. Combined ID tables use all saved collections, even
when you rebuild only selected seasons. `dataset/fixtures.csv`, if present from
the fixture builder, augments the FotMob-keyed team reference; season fixture
tables retain their Sofascore IDs.

Edit the builder's settings to select seasons or reference tables. The default
build is offline. Only explicitly enabling `REFRESH_PUBLISHED_TEAM_IDS` downloads
an external team-ID register.

Optional terminal commands:

```bash
python main.py --build-dataset
python main.py export --competition Bundesliga --season 2025/26
python dataset_builder.py --name bundesliga-2025-26
python dataset_builder.py --references-only
```

## Organization

```text
main.py                 Collection settings and the everyday workflow
dataset_builder.py      Export, season packaging, and combined ID tables
collection/             Collection commands, checkpoints, and CSV exporters
providers/              Provider clients, adapters, identity matching, and safety
fixtures/               Historical fixture-only collection and building
models/                 Match, player, team, and statistics models
storage/                Local persistence
analysis/               Offline analysis, event enrichment, roles, and viewers
tools/                  Explicit maintenance tools
tests/                  Offline regression tests
data/                   Local raw responses, caches, and checkpoints (ignored)
dataset/                Local analysis-ready tables (ignored)
outputs/                Local analysis reports and visualizations (ignored)
```

For fixture-only backfills, edit the settings in `fixtures/builder.py` and run
`python -m fixtures.builder`. It writes the separate historical
`dataset/fixtures.csv`; its default provider is FotMob. The standalone Sofascore
fixture collector is available as `python -m fixtures.collector --help`.

Analysis commands replace the former `run_*.py` wrappers:

```bash
python -m analysis.eda --help
python -m analysis.event_layer --help
python -m analysis.role_pipeline --help
python -m analysis.match_visualization --help
```

Install optional analysis dependencies with `python -m pip install -r
requirements-role-analysis.txt`. See [collection details](docs/COLLECTION.md)
for provider fields, checkpoint behavior, and export schemas.

## Existing local projects

Keep your existing `.venv`, `.idea`, `data`, `dataset`, and `outputs` at the
repository root. This layout has no `player_match_information/` wrapper. Update
PyCharm run configurations to the new script paths and use the repository root
as the working directory. Archive old source files before replacing them; do
not move or recreate your environment. Do not run the old and new collectors
against the same data at the same time.

## Verification

```bash
python -m unittest discover -s tests -t . -v
```

Tests use temporary directories and mocked provider responses. They do not run
live scrapes. Provider availability and live pagination still require separate
verification. HTTP refusals and cooldowns remain persistent across runs.
