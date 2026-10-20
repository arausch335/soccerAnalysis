# Main monitor, worker collection and direct reporting

Main receives progress and operational logs directly over your private network.
Git distributes code and common configuration only. Workers never make progress
commits, push snapshots, rebase local work or transfer raw datasets through Git.

| Computer | Default role | Work | Dashboard |
| --- | --- | --- | --- |
| Main (`main`) | Monitor | No scraping | Opens automatically |
| MSI (`msi`) | Worker | Seven competitions, 2021/22, combined, fast spatial | Off |
| Headless (`headless`) | Worker | Seven competitions, 2022/23, combined, fast spatial | Off |

## 1. Install Tailscale

Install Tailscale on Main and every worker, sign them into the same private
network and leave it connected. Find Main’s Tailscale IPv4 address, typically
`100.x.y.z`. Use that address below. No router port forwarding or public website
is required. If macOS/Windows prompts about inbound connections, allow Python
for this private connection; port 8766 is the report receiver.

The reporting service binds specifically to Main’s private/Tailscale IPv4
address. The dashboard stays at `http://127.0.0.1:8765` on Main. Workers do not
need the dashboard or a browser tab for reporting. Their existing scraper
Chrome extension is prepared at startup and used when an API fails.

## 2. Upgrade the code, preserving old inventories

Stop the old scraper on every computer before updating. Keep its original data
folder, environment and `data/_system/config/identity.json`.

On a checkout that previously tracked worker JSON files, use this one-time
upgrade from the repository directory and its existing Python environment:

```bash
git status --short
git fetch origin main
python -c "import subprocess; exec(subprocess.check_output(['git', 'show', 'origin/main:tools/upgrade_worker_reporting.py'], text=True))"
```

The helper backs up the generated JSON files, verifies the copies, caches the
inventories locally, restores **only those generated JSON paths**, and pulls
with `--ff-only`. It does not restore settings or source files, touch identities
or raw datasets, stash changes, push, or rebase. Existing staged changes stop
the helper before restoration. If unrelated edits or local commits prevent the
pull, review them normally; do not hard reset or force push. The backups remain
under `data/_system/workers/legacy-git-backup/`.

New clones can simply use `git pull --ff-only origin main`. Install dependencies
only if creating an environment: `python -m pip install -r requirements.txt`.
This reporting feature adds no package dependencies.

## 3. Configure Main once

Replace the example IP with Main’s actual Tailscale IP:

```bash
python -m collection.reporting server --bind 100.80.12.3
```

This creates ignored, private `data/_system/config/reporting.json` and prints a setup command
and a separate token for each worker. Keep the tokens private. Re-running the
command preserves existing tokens, including when changing Main’s bind address.
Then start Main:

```bash
python main.py --worker-id main
```

Main opens the dashboard and listens on port 8766. It does not scrape by
default. Historical local inventories are imported into its reporting database
when the service starts, so previously collected coverage is retained.

## 4. Configure each worker once

On MSI, replace the IP with Main’s actual Tailscale IP:

```bash
python -m collection.reporting client --worker-id msi --url http://100.80.12.3:8766
python main.py --worker-id msi
```

The first command asks for MSI’s token from Main using hidden input. On
Headless, substitute `headless` and its own token. The chosen identity is saved;
subsequent launches can use `python main.py` or PyCharm Run with no arguments.
Do not change a worker ID to work around an identity conflict. Restore its
original data folder, or intentionally configure a genuinely new worker ID.

The Sofascore extension connection code prints immediately at worker startup,
before inventory auditing or Git checks. Connect it in Chrome while the worker
prepares. It stays idle until a browser request is actually needed and is reused
across season jobs. Main in monitor mode does not start an extension connection.

Reconnect the existing scraper Chrome extension as usual. FotMob still uses
HTTP JSON endpoints; reporting does not change collection transports.

Connection addresses and tokens live only in each computer’s ignored
`data/_system/config/reporting.json`. Shared provider preferences remain in
`config/settings.yaml`, and assignments/roles in `config/workers.yaml`.

## Delivery, offline operation and logs

After every saved match, the worker durably queues inventory changes and a
runtime snapshot. A background thread delivers them without waiting in the
scraper’s network path. It also sends progress at schedule/job transitions,
finish, failure and normal stop, with a heartbeat every 30 seconds. Initial
connection sends a full local inventory; subsequent match updates send only
changed records. Local JSON comparison and queue writes still add some disk
work, but a match does not wait for an HTTP request or Git push.

If Main sleeps, Tailscale disconnects, or authentication fails, the worker
continues scraping and retains reports in
`data/_system/workers/reporting-outbox.sqlite`. It retries in the background.
Retries are acknowledged and deduplicated; restarting either computer retains
unsent reports and received history. Keep the original queue/database files
alongside the worker’s original identity. Losing/replacing a queue after its
worker is registered requires deliberate identity recovery, not silently
claiming the same worker ID with a second stream.

If reporting has not been configured yet, the supervisor still saves progress
and logs into the local queue. Configure it and restart to begin delivery.
There is no Git snapshot fallback. Keep Main awake for live reporting; queued
updates catch up when it returns. Stopping a worker leaves pending messages
saved, and they deliver on its next launch.

Open **Worker logs** on Main to filter received output by computer, importance or
text (for example a match ID or provider). Important updates are the default; choose
all logs, errors, warnings/errors, completed seasons, or starts/finishes/pauses. It includes worker console output,
Python errors reported during collection, and operational extension diagnostics
including browser actions, verification, recovery and timeouts. Known reporting
tokens, URLs and common credential fields are redacted before transmission.
Local console output and original diagnostic files remain available. Raw
response bodies, cookies, screenshots and captured pages are not uploaded.

The log view refreshes every five seconds. Lines queue immediately and send in
background batches; Main retains the most recent **10,000 lines per worker**,
with up to 300 matching lines shown in the browser. The local unsent queue grows
while offline until delivery succeeds. It is a monitoring tail, not an unlimited
central archive of every historical log. Abrupt process kills cannot forward a
partial console line still buffered in memory.

Main stores received state in `data/_system/workers/reporting.sqlite`, with
ignored inventory/status views under `ensemble_data/reporting/`. The database
belongs to Main; no database file is opened across the network. The dashboard
marks a direct worker disconnected after three missed heartbeats. Git checks
continue every two minutes solely for code and shared assignment changes.

Workers also receive other computers’ inventory evidence approximately every
30 seconds for ensemble completion checks. This never pretends remote raw data
exists locally. Live reports do not reserve work: the shared plan continues to
reject overlapping assignments. Transfer/import the actual dataset folders
separately when needed.

## Missing worker updates

On a worker, run this read-only check in its normal Python environment:

```bash
python -m collection.reporting status
```

It prints the configured Main address, worker state, queued message count and
connection result without printing any token. It distinguishes an unreachable
receiver, a rejected token and an original data-folder/queue conflict. Main’s
address must be its actual Tailscale IPv4 address, including port 8766
(e.g. `http://100.106.146.82:8766`). Updated clients default plain HTTP to
8766 when the port is omitted. Never delete an identity or
queue to hide a conflict; preserve them and restore the correct originals.

“Direct reporting enabled” means the local configuration loaded. The separate
“Connected to Main” console message confirms successful report delivery.
Progress/log uploads run independently from downloads of other workers’
inventories, so a slow inventory download cannot prevent reporting. Inventory
responses are compressed when both computers support the updated protocol.
After pulling code, restart Main and workers to load the changes.

## Shared controls and safe code updates

```yaml
git:
  enabled: true
  remote: origin
  branch: main
  poll_seconds: 120
  auto_restart: true
reporting:
  enabled: true
  heartbeat_seconds: 30
```

Workers fetch code while scraping and apply a fast-forward update only between
saved season jobs, followed by a controlled restart. Local edits, staged files,
unrelated untracked files, local commits or an unfinished Git operation pause
updates and preserve your work. Workers need Git fetch access, not Git commit
identity or push credentials for reporting. Remove a worker’s write access if
you do not otherwise use that machine to push code.

Main defaults to monitor and workers default to scraping without a dashboard.
Both capabilities remain available:

```bash
python main.py --monitor --no-dashboard  # Monitor receiver without a browser
python main.py --with-dashboard          # Worker plus optional local dashboard
python main.py --worker                  # Explicitly run this computer’s assignments
python main.py --dashboard               # Read-only view; does not start receiver or Git
python main.py --retry --once            # Revisit assignments using saved checkpoints
```

Assign Main work and use `--worker` if you want it to scrape. Configuration
addresses/tokens and queue/database files are machine state: imports archive
another computer’s copies instead of activating them on the receiving computer.
Do not copy Main’s reporting config onto a worker.

## Windows save conflicts

Atomic JSON, collection CSV and imported-file replacements retry brief Windows
access/sharing errors for about two seconds. The destination is never deleted
to force a replacement; persistent permission errors still pause collection.
An error saving provenance does not erase the match checkpoint saved before
it. Restart the worker after updating to resume from its checkpoints and caches.
If the same file remains inaccessible, close any program holding it open and
check the file’s write permissions; do not delete identity or provenance files.

## Coverage and current jobs

The coverage grid aligns equal-width season columns across competitions, with
player match stats (Sofascore, FotMob, WhoScored), WhoScored events, expandable
Sofascore spatial maps, and other data. Solid worker colors indicate complete,
hatching indicates planned unfinished work, and light grey indicates unplanned
missing data. Unavailable and needs-verification states remain distinct.
Copies count once; resource owners preserve collection credit.

Current jobs show elapsed time, games scraped, cached/skipped, failed/partial,
held elsewhere, remaining attempts and provisional ETA. ETAs recalculate from recent successful matches in the current run; cache-only
work and verification waits do not train the scrape rate. Every selected
checkpoint is counted until it is checked against current requirements, rather
than trusting an older CSV completion flag. Main can reconstruct a provisional
estimate from older live worker reports that incorrectly claim zero remaining.
Active phases show checking/finishing instead of a misleading zero-second ETA.
The card names the current or most recent provider. Counts and ETA remain
based on the latest report; the dashboard does not invent progress while a
worker is disconnected. Targets come from saved schedules and reported fixture
lists. A season with no discovered target is never presented as 100% complete.

Inventory audits validate old data before a pending job. This can take several
minutes on a large archive. Monitor/unassigned computers do not automatically
scan their full archive. To rebuild explicitly, stop the scraper first:

```bash
python main.py --inventory
```

Then start the normal supervisor to report the refreshed inventory. Explicit
manual collection commands and standalone launchers retain their prior behavior;
direct reporting and remote logs are provided by the shared supervisor.

### Reviewing identities on Main

Open **Identity review** in Main’s dashboard. Suggestions show the provider and
Sofascore player IDs, names, match, lineup side, and shirt numbers when available.
Team suggestions require a match on the same date with an independently matched
opponent. Scores rank suggestions; they are not probabilities. Confirm the same
identity or reject the proposed link. Suggestions are never applied automatically.
Starter disagreements, missing players, and unavailable provider responses remain
partial until the underlying evidence is corrected; a confirmation does not simply
change a completion flag.

Decisions and the review queue live in `data/_system/identity/review.sqlite` on
Main. Back up this file with Main’s other operational state. They travel through
authenticated direct reporting, separately from Git, and are cached on each worker.
Workers receive decisions at their next inventory poll (normally 30 seconds), use
them when resolving subsequent matches, and run one saved-checkpoint pass over
previously incomplete assignments per new confirmed revision. Raw provider results
are reused; completed jobs are left alone. A confirmation received during a job also
permits a follow-up pass, so earlier matches in that job are not missed. Rejections
hide that proposed link and do not trigger recollection.

Install the updated collector on Main and workers, then restart them. Previously
exported player mismatches seed the queue at startup; newly encountered player and
team mismatches report suggestions after a saved match. To revisit all old paused
assignments immediately, start the worker with `python main.py --worker-id msi
--retry` (replace `msi` with its worker ID). The flag applies to that launch only.

Current jobs show **matches processed**, with complete, partial, failed, cached,
and held-elsewhere counts separately. ETA uses recent non-cached complete and
partial matches that performed network collection, excluding explicit verification
waits. It estimates the current pass, including partial results, rather than the
time needed to resolve every outstanding provider problem. Main can also rebuild
this estimate from older workers’ existing reports.

Temporary fixture-discovery outages get at most two additional attempts during a
season, after five minutes and then fifteen minutes. Authentication and rate-limit
errors are not retried this way. Fixture months already downloaded successfully
remain usable if another month fails. The review tab separates these provider
errors from identity suggestions; absent qualification coverage is not repaired by
merging unrelated names.

### Automatic player identity investigation

Before a player-name mismatch can be confirmed manually, a worker looks up both
player profiles. Profile requests use the existing API-first transports and the
same connected Chrome extension when needed. Soccer Collector 0.7.0 adds only
public player profiles, transfer history, and career-table reads to its existing
provider allowlists. Prepare and reload the extension after pulling this update.

An identity is automatically confirmed only when the full birth dates and current
clubs agree and dated career histories agree across at least two distinct clubs.
Career comparison uses reviewed club-name concordances, requires overlapping
dates, rejects substantive conflicting stints, and ignores administrative entries
lasting no more than seven days. A shared club, matching shirt numbers, or a fuzzy
name match alone cannot confirm an identity. Sparse WhoScored historical coverage
stays incomplete; missing history is never interpreted as agreement. Multiple
qualifying candidates and existing human rejections prevent automatic confirmation.
Existing same-side, unique-player and starter checks still apply when enriching a
match.

The dashboard shows birthdays, clubs, dated histories, source links and the result
of each check. Successful checks appear as **Automatically verified** in decision
history; unavailable or conflicting evidence goes to manual review. Queued profile
checks are labelled and cannot be confirmed prematurely. Workers also receive
Main’s older unchecked suggestions through direct reporting, investigate one group
between games or while idle, and send back the evidence. Main stays a monitor.

Only compact normalized profile records are kept in the existing identity SQLite
file: successful lookups are reused for seven days and failed lookups for six
hours. Request scratch data uses the existing bounded cache. No separate player
page archive or large permanent HTML cache is created. An optional denied Sofascore
profile read does not trigger the long match-verification reload workflow.

Reviewed Zini club-name variants `APO Levadiakos` / `Levadiakos` and `AEK B` /
`AEK Athens B` are included in the shared team concordance. The public
[Sofascore profile](https://www.sofascore.com/football/player/zini/1002334) and
[FotMob profile](https://www.fotmob.com/players/1106943/zini) provide the matching
birth date and dated club history used to check this example.
