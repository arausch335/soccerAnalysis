# Soccer event layer

This is a standalone, read-only consumer of the collector's `events.csv` export. It never imports the collector, makes network requests, or writes into the collection directory. The collector can continue running while this layer works from its latest stable CSV.

## Data flow

`events.csv` → validation and semantic enrichment → relationship inference → possession segmentation → player-match outputs

The pipeline publishes four connected datasets plus a manifest:

- `events_enriched`: one row per provider event, retaining every original column and adding normalized geometry, 15-zone locations, event semantics, paired-duel links, possession IDs, inferred pass recipients, and inferred carries.
- `possessions`: one row per inferred team possession with timing, location, progression, and result.
- `player_match_features`: one row per player-match with compact passing, receiving, progression, attacking, defensive, and spatial features.
- `player_match_profiles.jsonl`: nested descriptive records intended for reporting and visualization.
- `manifest.json`: input checksum, pipeline/config versions, output format, and row counts.

All inferred values are labeled. Raw provider outcomes remain intact, while `effective_outcome` captures football semantics such as a nominally successful `OffsidePass` ending possession. `StandingSave` is retained only in the raw qualifier JSON and is not treated as a generic event meaning.

## Run safely

In PyCharm, use module name `analysis.event_layer` in a Python run configuration with the repository root as its working directory. It processes the full current export and writes to `outputs/event-layer-full`. To try one match first, add `--trial` to the PyCharm run configuration's parameters.

The equivalent terminal commands from the project root are:

```bash
.venv/bin/python -m analysis.event_layer --trial
.venv/bin/python -m analysis.event_layer
```

The lower-level command, run from the repository root, is:

```bash
../.venv/bin/python -m analysis.event_layer.cli \
  ../data/premier-league/2024-2025/events.csv \
  ../outputs/event-layer-trial \
  --limit-matches 1
```

Remove `--limit-matches 1` for the whole export. `--format auto` is the default: it writes Parquet when a Parquet engine is installed and otherwise writes CSV. Install the `parquet` extra when Parquet becomes the integration contract.

The command fingerprints the input before and after reading. If the collector replaces the export during that interval, the run stops without publishing derived tables; rerunning will use the next stable export. Each output file is replaced atomically.

## Modeling boundary

`player_match_features` describes the match in which the events occurred. It is an analysis target or reporting table, not a safe same-match prediction input. A later trait builder should sort matches by kickoff and calculate lagged rolling features using earlier matches only.

Recipient, carry, and possession fields are transparent heuristics rather than provider facts. Their thresholds live in `PipelineConfig`, and confidence/status columns should be retained through downstream analysis. The current input adapter targets the collector's WhoScored export; a second provider should be normalized to the required canonical columns before calling `digest_events`, leaving all downstream logic shared.

## Test

```bash
../.venv/bin/python -m unittest discover -s tests -q
```

## Integration boundary

Future integration should invoke this pipeline only after an offline export completes, or point it at a copied/versioned export. Do not add event inference to the request loop. Provider-specific adapters can be added ahead of `digest_events`; everything after the required canonical columns is provider-neutral.
