# Sofascore scrapers: transfer package

This project packages the fixture scraper for Python 3.10+ on Windows, macOS,
and Linux. A helper brings your existing player match information scraper from
your local SoccerAnalysis project into the same repository.

**The ZIP currently contains the fixture scraper and the transfer helper.** Your
player scraper is on your computer, so run the helper below to include it. Its
original folder is left unchanged. The player scraper's dependencies and run
command cannot be verified until that code is available.

## 1. Include your local player scraper

Unzip this package and open a terminal in the extracted `sofascore-scrapers`
folder. Run the command for your operating system, adjusting the path to match
your existing SoccerAnalysis folder.

Windows:

```powershell
py tools/include_player_project.py --source "C:\Users\Alexander\PycharmProjects\SoccerAnalysis"
```

macOS:

```bash
python3 tools/include_player_project.py --source "/Users/Alexander/PycharmProjects/SoccerAnalysis"
```

The helper creates `player_match_information/` with Python source files,
dependency manifests and documentation, preserving subfolders. It excludes
virtual environments, editor settings, environment files and common data/output
folders. A manifest records the copied files. It stops if the destination already
exists, so it cannot overwrite an earlier copy.

If your player scraper needs additional assets or config files, copy those into
that folder as well. The helper copies code and dependencies; it cannot identify
runtime assets without inspecting the original scraper.

## 2. Transfer using a private GitHub repository

Follow [PRIVATE_GITHUB.md](docs/PRIVATE_GITHUB.md). It gives GitHub Desktop and
terminal instructions for publishing this folder privately, then cloning it on
another computer. Publishing does not run either scraper.

## 3. Run the fixture scraper

From the extracted or cloned project folder:

Windows:

```powershell
py -m sofascore_scrapers.fixtures --output data/fixtures
```

macOS/Linux:

```bash
python3 -m sofascore_scrapers.fixtures --output data/fixtures
```

No dependencies are needed for fixtures. Its scope is England, Spain, Germany,
Italy and France's top two divisions, plus the UEFA Champions League, Europa
League and Conference League. Kickoffs start at January 1, 2010 UTC; published
future fixtures are included. Conference League starts with its first season in
2021/22.

Requests are serial with 15–25 second gaps. HTTP 403 stops immediately;
rate-limit and transient errors get increasing backoff. Rerun the same command
to resume from saved pages. CSV, raw JSONL, SQLite checkpoints and a coverage
report are saved under `data/fixtures/`. Git ignores generated outputs.

For a small trial:

```bash
python3 -m sofascore_scrapers.fixtures --competition 17 --max-requests 3 --output data/trial
```

On Windows, substitute `py` for `python3`. To export checkpoints without making
requests, add `--export-only`. See `--help` for pacing and date options.

The first live request in the cloud workspace returned HTTP 403 `challenge`,
so **no fixture data was collected and live pagination remains unverified**.
Moving the code does not establish that Sofascore will accept requests from the
destination computer. The internal API may change.

## 4. Run the existing player scraper

On the destination computer, open `player_match_information/` in PyCharm and
create a new Python environment. Install the copied requirements if present:

```bash
python -m pip install -r requirements.txt
```

Use the player script's existing entry point and configuration. Preserve the
working directory it used in your original project. Virtual environments are
recreated on the new machine rather than copied between computers.

## Optional installation

You can also install the fixture command into a Python environment:

```bash
python -m pip install .
sofascore-fixtures --output data/fixtures
```

## Verification

```bash
python -m unittest discover -s tests -v
```

Tests run offline. GitHub Actions runs those checks on Windows, macOS, and Linux
after you publish. No live scrape runs automatically.
