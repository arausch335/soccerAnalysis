# Main monitor, worker collection and progress publishing

The shared plan in `config/workers.yaml` controls all computers. Its current
assignments are preserved:

| Computer | Default role | Work | Dashboard |
| --- | --- | --- | --- |
| Main (`main`) | Monitor | No scraping | Opens automatically |
| MSI (`msi`) | Worker | Seven competitions, 2021/22, combined, fast spatial | Off |
| Headless (`headless`) | Worker | Seven competitions, 2022/23, combined, fast spatial | Off |

## Update and start each worker

1. Stop its old Python scraper with Ctrl+C or PyCharm’s Stop button. Keep its
   original data directory, browser profile and `data/identity.json` in place.
2. In that computer’s existing repository and Python environment, run:

   ```bash
   git status --short
   git pull --ff-only origin main
   python main.py --worker-id msi
   ```

   Use `--worker-id headless` on Headless. The identity is saved, so subsequent
   launches can simply use `python main.py` or PyCharm Run with no arguments.
3. Ensure Git has a commit name/email and is authenticated for both fetch and
   push without interactive prompts. Workers disable Git prompts so a login
   request cannot hang a scrape. Configure missing Git identity normally with
   `git config user.name` and `git config user.email`; use your own values.
4. Connect the existing scraper Chrome extension as usual if the common settings
   use extension access. No dashboard opens on workers. This does not turn off
   the browser/extension used to collect data. FotMob continues through HTTP JSON
   endpoints; its transport has not changed.

No new package dependency was added. If creating an environment for the first
time, install `python -m pip install -r requirements.txt` and follow the browser
setup in [BROWSER_COLLECTION.md](BROWSER_COLLECTION.md).

The reporting upgrade revisits assigned jobs once using their existing
checkpoints, so earlier attempted jobs gain current coverage and timing reports.
A first pending job audits saved local files before scraping. This can take
several minutes for a large historical archive. Existing matches are resumed;
raw data is not deleted or overwritten by Git updates. If Git reports local
changes or divergence, stop and review them. Do not use a hard reset or force
push. Older machine-specific selections should be moved into the shared plan,
with common collection preferences preserved in `config/settings.yaml`.

Each worker ID belongs to one original data folder. The program refuses a
conflicting ID before changing local identity. Data copied for import belongs
in `ensemble_data`, where the receiving computer keeps its identity and run
state. Never resolve an identity conflict by copying another computer’s
`identity.json` over your own.

## Main computer

```bash
python main.py
```

Main defaults to monitor mode: it opens `http://127.0.0.1:8765`, checks Git every
120 seconds and receives workers’ published progress. It does not start
scraping, even if an assignment is accidentally added while its role remains
`monitor`. Keep this program running; the separate `--dashboard` view does not
synchronize Git.

Both roles retain both capabilities:

```bash
python main.py --monitor --no-dashboard  # Monitor without a browser
python main.py --with-dashboard          # Worker plus optional dashboard
python main.py --worker                 # Explicitly execute this computer’s assignments
python main.py --dashboard              # Read-only view; no scraping or Git sync
python main.py --retry --once           # Revisit assigned work using checkpoints
```

To make Main scrape, assign it work and either change its role to `worker` or
launch with `--worker`. Avoid overlapping assignments; the plan validates
competition, season and requested data types before collection starts.

## Hourly snapshots, safe code updates

Shared settings:

```yaml
git:
  poll_seconds: 120
  publish_seconds: 3600
  publish_inventory: true
  auto_restart: true
```

Workers fetch Git refs while scraping. Once an hour has elapsed, they publish
progress immediately **after the current match is saved**. They also publish
when a job starts, finishes, pauses, fails or is stopped normally. No match is
interrupted merely to meet the hourly deadline. A verification wait or very
long match can delay publication beyond an hour. Failed publication keeps local
progress and appears in Git status; the next scheduled attempt retries it.

Mid-season publishing uses an isolated Git index and creates a snapshot on top
of the latest remote branch. It changes only this worker’s inventory and
sanitized runtime files. It does not change the running checkout, HEAD or user
index. Concurrent worker pushes are retried without force pushing; successful
publication receipts allow the worker to consume its own commits safely later.

Source/configuration updates still apply **between season jobs**. The worker
closes the saved job, updates its checkout and restarts Python before another
job begins. It also rechecks updates after a long startup audit. Local source
edits, staged files, unrelated commits or another checkout claiming the same
worker snapshots pause automatic sync and remain intact. Raw data, cookies,
profiles, local identity and detailed errors stay out of Git. Only these files
are eligible for automatic publication:

- `ensemble_data/workers/<id>.json`
- `ensemble_data/runtime/<id>.json`

With the default hourly publication and two-minute monitor polling, Main
normally sees progress within approximately an hour plus two minutes. The
browser refreshes its local view every five seconds. Git snapshots are not a
live connection; the dashboard always shows their age and flags overdue
running-job updates.

## Coverage grid and current jobs

The main view aligns equal-width season columns across all competitions. Rows
are grouped into player match stats (Sofascore, FotMob, WhoScored), WhoScored
events, Sofascore spatial maps and other match data. Spatial rows expand into
average positions, heatmaps, shots and action maps.

Solid worker colors mean complete; worker-colored hatching means assigned but
unfinished; light grey means missing and unassigned. Unavailable responses and
records that need verification have distinct patterns. Spatial summaries count
match × map-type sets, so one available map never implies every map is saved.
Hover for exact counts and click a season for provider IDs and stored evidence.
Filter competitions, categories and seasons, or highlight a worker without
changing the underlying coverage totals.

Transfers do not count as new collection. Copies are deduplicated; multiple
collectors for the same saved unit count once. New records retain resource-level
collection credit. Older ambiguous origins are neutral mixed credit instead of
an invented worker split. Linked FotMob native and combined player statistics
count once; old unlinked provider identities may require an audit before exact
cross-provider deduplication is possible. Provider records in the summary remain
separate IDs, not a unique real-world fixture count.

Saved local schedules and workers’ published selected-fixture lists supply
season targets. Combined event targets can use the baseline fixture target,
explicitly labelled in the tooltip. If a total has not been discovered, the
bar says “saved” and “target pending”; it does not claim the season is complete.

Current jobs show elapsed time since the reported start, games scraped this
run, cached/skipped matches, failures/partial matches, remaining attempts and
ETA. ETAs use recent successful active scrape times, exclude cache-only work
and verification waits, and are provisional while calibrating. Remote counts
and ETA stay tied to their last report even while the elapsed clock advances.
A stopped/paused job freezes elapsed time at its saved snapshot.

New collections and audits record explicit availability for every supported
resource. An audited optional resource with no collection evidence is treated
as not attempted, not unknown. Truly legacy inventories without resource
availability need an offline audit, normally done before the worker’s first
pending job. Stop a worker before a separate manual audit:

```bash
python main.py --inventory
```

Unassigned/monitor computers do not start a full archive audit automatically.
Use `--retry` for attempted/paused assignments; they are not retried in an endless
loop. Edit assignments on Main and commit/push the common plan to distribute
new work. Explicit CLI selections and standalone launchers retain their
previous behavior, without the shared supervisor’s automatic Git actions.
