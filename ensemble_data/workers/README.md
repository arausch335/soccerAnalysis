# Worker inventories shared through Git

Run `python main.py --inventory --worker-id main-mac` on the main Mac, and choose
a different ID on every other computer (for example `laptop` or `desktop`).
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
python main.py --inventory --worker-id main-mac
python review_data.py
git add ensemble_data/workers/main-mac.json
git commit -m "Update main-mac inventory"
git pull --rebase origin main
git push origin main
```

Use `laptop.json` on the laptop. Re-run `--inventory` after importing/transferring
data, or simply collect through `main.py` to update the local snapshot. This
command makes no scraper requests; it also performs any pending data imports.
The program writes inventories locally; it never automatically commits, pushes
or pulls them. Raw data, local identity, detailed logs, transfer payloads and
the legacy aggregate `ensemble_data/provenance.json` remain ignored by Git.

Git provides snapshots when committed/pulled, not live work allocation. Use
different league/season assignments for workers scraping simultaneously.
