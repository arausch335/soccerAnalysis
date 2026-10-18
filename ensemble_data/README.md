# Bring collections together

Paste another computer's entire `data` folder here, for example
`ensemble_data/laptop/data/` or `ensemble_data/data/`. Stop copying before running
`python main.py`. It imports all marked folders before any collection starts.
Use `python main.py --import-only` to import without starting a scraper.

Successful imports are removed from this folder. Incomplete/invalid imports stay
here. The regular data folder contains an import journal, checksums, preserved
conflicting originals and machine-specific state under `ensemble_imports/`.
Machine-specific browser profiles, cookies and safety settings are archived,
not activated on a different computer. Never paste files while an import runs.

`provenance.json` is the small portable progress file. After scraping, copy it
to this folder on the next computer before starting `main.py`. It identifies
provider/collection type, competition, season, match ID and completed coverage.
That computer skips work with matching coverage, while keeping remote-only
matches distinct from files actually available locally. Copying this file does
not transfer data; eventually import all data folders on the main Mac.

The file updates after every saved match and is rebuilt/merged from local
artifacts at startup. For simultaneous computers, use different league/season
selections: a manually copied file cannot coordinate new work in real time.
Use `main.py --ignore-provenance` to collect missing local data even if the
portable map records it elsewhere; existing local caches/checkpoints still resume.

Run `review_data.py` for local data completeness, including imported WhoScored,
Sofascore and FotMob data. To scrape all league/season combinations in the YAML,
run `python main.py` without `--competition`, `--season` or `--name`. The default
Sofascore access is `extension`; one extension connection serves the whole run.
