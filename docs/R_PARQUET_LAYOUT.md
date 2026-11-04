# Dataset layout

The public dataset uses CSV files under `raw_data` and Parquet tables by default
under singular `concordance`. Use `--package --csv` for CSV concordance tables. See [dataset layout and R import](DATASET_LAYOUT.md) for paths,
schemas, migration and the shared build command. Older Parquet exports are
archived under `concordance/metadata/legacy` after a successful replacement.
