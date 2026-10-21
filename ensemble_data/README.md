# Import copied collections

Use this folder for a one-time SD-card or external-drive transfer. Main remains
the monitor; MSI is the central raw archive. Headless can also import a local copy
when needed. Importing onto Headless does not automatically forward all historical
Main seasons to MSI.

Prepare a portable bundle on the source computer (replace the example card path):

```sh
python -m tools.prepare_transfer --output "/Volumes/CARD/main-data"
```

The output must be a new folder outside `data/`, with an existing parent. The
helper locks the source collection, copies canonical and legacy provider raw data,
active checkpoints, season files and original collection credits, and verifies
checksums. It excludes machine credentials, reporting queues, caches, profiles and
retired checkpoint generations. Source files remain untouched.

On MSI, stop the worker before copying:

```sh
python main.py --terminate
```

Copy the complete `main-data` folder from the card into MSI's checkout as
`ensemble_data/main-data/`. Do not copy loose league folders directly over its
live `data/` folder. Then, on MSI:

```sh
python -m tools.prepare_transfer --verify ensemble_data/main-data
python main.py --import-only --worker-id msi
python main.py --worker-id msi
```

Main can keep monitoring while MSI imports. An active source collector prevents
export; Main's monitor may continue reporting while the export holds its
inventory/collection lock. The same bundle can be imported on another worker
using its own existing ID, but MSI is the intended central raw archive.

The importer verifies the portable bundle again, validates and merges checkpoints
and raw data, keeps the receiver's identity/settings, and preserves original
collector credit. It archives conflicts and writes an import journal under
`data/_system/imports/`. A successful import removes only the staged bundle under
`ensemble_data/`; keep the card and original source until the result is checked.
An invalid bundle stays staged for investigation. macOS metadata files are ignored.

The refreshed receiver inventory is sent to Main when its supervisor resumes.
Supervised workers still obtain season ownership from Main. Shared assignments
are in `config/workers.yaml`; reporting inventories are evidence of held data,
not a substitute for copying the underlying raw files.
