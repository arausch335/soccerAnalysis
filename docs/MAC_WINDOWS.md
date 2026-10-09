# Run on macOS or Windows

Install Git (or GitHub Desktop) and **64-bit Python 3.12** from
https://www.python.org/downloads/. macOS Intel and Apple Silicon and Windows
x64 have compatible binary dependencies; no C compiler is needed for this setup.
Choose the Python installer for your computer's architecture. Windows ARM and
32-bit Windows were not checked.

Clone the private repository using [these instructions](PRIVATE_GITHUB.md).
Until the changes are merged, select the `simplify-scraper-outputs` branch.
Start the commands below in the cloned repository's root folder. Paths with
spaces work; quote your path when using `cd`.

## macOS Terminal

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r player_match_information/requirements-tested.txt
cd player_match_information
../.venv/bin/python collect.py --help
```

If Python is available only as `python3`, first check `python3 --version` reports
3.12, then use `python3 -m venv .venv`. You can run the programs directly:

```sh
../.venv/bin/python main.py
../.venv/bin/python build_csvs.py
../.venv/bin/python build_fixtures.py
```

## Windows PowerShell or Command Prompt

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r player_match_information\requirements-tested.txt
cd player_match_information
..\.venv\Scripts\python.exe collect.py --help
```

If the Python launcher `py` is unavailable, check `python --version` reports
3.12, then use `python -m venv .venv`. Run the programs directly:

```powershell
..\.venv\Scripts\python.exe main.py
..\.venv\Scripts\python.exe build_csvs.py
..\.venv\Scripts\python.exe build_fixtures.py
```

These commands do not require activation or changes to PowerShell's execution
policy. Edit each script's settings before running it: `main.py` collects data,
`build_csvs.py` rebuilds saved outputs, and `build_fixtures.py` collects fixtures.
The [player README](../player_match_information/README.md) explains those settings.

## PyCharm

Open the cloned repository and select an existing project interpreter:
`.venv/bin/python` on macOS or `.venv\Scripts\python.exe` on Windows. Set the
working directory of each player script's run configuration to
`player_match_information`. Run `collect.py --help` to check setup without
requesting provider data.

## Move saved data between operating systems

Recreate `.venv` on each computer; virtual environments contain OS-specific
binaries and paths. Code moves with Git. To move player collection data, stop
the collector and run from `player_match_information` on the first computer:

```sh
# macOS
../.venv/bin/python transfer.py pack data collection-state.zip
```

```powershell
# Windows
..\.venv\Scripts\python.exe transfer.py pack data collection-state.zip
```

Copy `collection-state.zip` to the second computer's `player_match_information`
folder, then run the corresponding command there:

```sh
# macOS
../.venv/bin/python transfer.py restore collection-state.zip data
```

```powershell
# Windows
..\.venv\Scripts\python.exe transfer.py restore collection-state.zip data
```

The destination `data` directory must not already exist. Archives use portable
relative paths, preserve UTF-8 names, and include caches, checkpoints and cooldown
state. Continue on only one computer at a time. Dataset and fixture outputs
outside `data` can be rebuilt or copied separately, keeping `fixtures.csv` with
`fixtures.metadata.json`.

## Verification and live access

On October 9, 2026, dependency wheels resolved for Python 3.12 on Windows x64,
macOS Intel and macOS Apple Silicon. A fresh Linux environment was installed
using the versions in `requirements-tested.txt` and checked with offline tests.
Regression tests exercise UTF-8 cache reads and collection transfer/rebuild with
a simulated Windows default encoding. Native macOS/Windows execution was not
available for this check.

A single live request to
`https://www.sofascore.com/api/v1/event/11352532` returned **HTTP 403** with
reason `challenge` from the cloud environment. The safe client made one request,
stopped, and persisted a 24-hour Sofascore cooldown. This does not establish
whether requests from your own computer will succeed. The fixture builder's
403 fallback to FotMob remains covered by offline tests; no live FotMob request
was made during this check.
