# Worker inventories shared through Git

Run `python main.py --inventory --worker-id main` on the main Mac, and choose
the configured `msi` ID on the original MSI data folder. Add any new ID to
`config/workers.yaml` before using shared worker mode.
The command validates saved local artifacts and writes `<worker-id>.json` here.
It also saves the chosen ID in `data/identity.json` (under the configured data
root); that identity travels with copied data folders. Subsequent collection
runs reuse it and update only their own worker inventory. Each inventory lists
data **held** on that worker; each record's `origins` identifies its collectors.
Imported data retains its origins without replacing the receiving worker ID.

Commit/push your own JSON file to share it. Pull other worker files before
starting collection. All files here are read for completed work; remote records
do not count as locally stored data. One computer must own each worker ID. IDs
use lowercase letters, numbers, hyphens or underscores, up to 64 characters.
Changing an ID leaves the previous inventory file for explicit review/removal.

Example from the main Mac:

```bash
python main.py --inventory --worker-id main
python review_data.py
git add ensemble_data/workers/main.json
git commit -m "Update main inventory"
git pull --rebase origin main
git push origin main
```

Use `laptop.json` on the laptop. Re-run `--inventory` after importing/transferring
data, or simply collect through `main.py` to update the local snapshot. This
command makes no scraper requests; it also performs any pending data imports.
Manual collection commands write inventories locally. The shared worker
supervisor (`python main.py` without scrape-selection arguments) automatically
shares this worker’s inventory and sanitized runtime status hourly after a saved
match, and at job start/finish, when enabled in `config/workers.yaml`. It fetches while running, applies code/config updates
between season jobs, and restarts for code/configuration changes. Uncommitted user
edits pause sync and stay intact. See [worker setup](../../docs/WORKERS.md). Raw data, local identity, detailed logs, transfer payloads and
the legacy aggregate `ensemble_data/provenance.json` remain ignored by Git.

Git provides published snapshots, not proof that another computer is online.
Work allocation comes from the shared `config/workers.yaml` plan, which rejects
overlapping data-type assignments.
