# Main monitor, worker collection and direct reporting

## Separate qualifiers, configuration completion and collection review

`Champions League Qualifiers` and `Europa League Qualifiers` are distinct
competitions in `config/workers.yaml`, alongside their main competitions. MSI
keeps its 2021/22 assignments and Headless its 2022/23 assignments, including the
new qualifier jobs. Main remains the instruction coordinator and monitor.
Sofascore's shared season feed is partitioned by its qualifying flag. FotMob uses
the separate qualifying competitions (10611 and 10613). WhoScored discovers a
qualifying directory from published links when available; its returned fixtures
must still match the selected qualifying game. Missing WhoScored coverage is a
reviewable collection issue, never invented event data.

Fast mode now requires player stats from all three providers, WhoScored events,
and the match details configured by the job; it does **not** request or require
spatial maps. All mode additionally requires average positions, heatmaps, shot
maps and action maps. Existing spatial data is retained. A season is Done when
Main knows its fixture target and every required resource is either collected or
explicitly reviewed unavailable. An old paused exit code is not a completion
verdict. Changing the data configuration or adding unfinished fixtures opens work
again; an unknown or empty target never becomes Done automatically.

In **Worker plan**, each season has full-width progress bars for player match
stats, events and match details, plus spatial maps in All mode. Colors/patterns
identify data types; grey is the unfinished part of that type's whole-season
target. Reviewed unavailable data has a separate hatch and is not counted as
collected. The status underneath shows Done, Needs review or In progress.

Use **Collection review** on Main (also linked from each assignment):

1. Inspect a flagged match's error, attempt count and missing resources. Repeated
   failures, reported unavailable resources, and the final few failed matches of
   an attempted season are surfaced for review. Identity review remains separate.
2. Select the particular resources that cannot be collected, enter the reason
   (for example, verified missing WhoScored matchCentreData), and confirm them
   unavailable. No data is manufactured and unrelated resources remain required.
3. Once all required data is accounted for, **Declare season done** records the
   outcome. **Retry selected data** or **Reopen reviewed exceptions** lets the
   workers check again. Every decision is retained in Main's audit history.

Decisions live in Main's ignored `data/_system/workers/completion.sqlite` and are
sent through Main's normal authenticated instructions. They apply to competition,
season and required-data configuration, so moving a job to another worker keeps
the decision, while switching Fast to All does not reuse the old exceptions.
Resolved seasons queue the normal completed-season transfer/build; raw data stays
on MSI and Main receives datasets. Local `completion.json` records the scope and
reviewed missing resources without claiming they were downloaded.

Legacy qualifying checkpoints are reused in their separate competition folders
when needed, with referenced raw files and collector provenance retained. The
old source stays intact. Background audits split legacy mixed schedules into
scoped inventory reports; allow workers to report this audit after upgrading.
Update and restart Main first, then update and restart MSI and Headless. Do not
copy Main's settings or identity files to workers.

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

### Stopping workers completely

In the checkout containing the running scraper, use:

```bash
python main.py --terminate
```

This stops all project Python collectors and monitors on that computer, including
background/PyCharm copies and Python child processes. It requests a graceful
shutdown, waits up to 10 seconds, then forces remaining processes to exit. It
checks that no matching processes remain and that worker/collector locks can be
acquired. Lock files are never deleted. Saved checkpoints and job history stay
intact; an interrupted current match is retried after restart. Unrelated Python
applications and other checkouts are outside its scope.

Use `python main.py --terminate --worker-id headless` to restrict the command to
one worker, or `--data-root PATH` for a specific data folder. `--timeout 20` allows
a longer grace period (maximum 30 seconds). The command runs before worker
startup, so it can stop the process that currently holds the worker lock.

Install updated dependencies after pulling (`python -m pip install -r
requirements.txt`); termination uses `psutil` for process discovery on Windows,
macOS and Linux. Run termination locally on each worker computer. Main's
reporting connection does not provide remote Windows process control.

### Live progress

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

Inventory audits use saved validation receipts in
`data/_system/cache/inventory.sqlite`. The first audit validates existing data;
later background checks reuse unchanged matches without reopening checkpoint tables or
provider responses. Normal workers start scraping without a blocking startup audit. New, changed or removed files are checked, including linked
WhoScored raw events. Identity decisions, Reep changes and relevant validation
code changes invalidate the receipts. Imported data always receives an
independent validation; receipts copied from another computer are not trusted.

Every supervisor checks local inventory in the background, normally every 30
seconds, and reports changed holdings. Main never starts collectors in monitor
mode. Checks skip busy seasons and yield promptly when scraping requests a season;
they never hold the global collector lock while auditing. Worker reports continue to
arrive independently, and the dashboard refreshes every five seconds.

To run an incremental audit explicitly, stop the scraper first:

```bash
python main.py --inventory
```

To force a complete audit and refresh the receipts:

```bash
python main.py --inventory --full-inventory
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

A missing current club (`primaryTeam: null` on FotMob) or missing career data
remains missing evidence. It cannot automatically confirm a player identity.
Malformed optional profile responses are saved as lookup errors for review;
match collection continues instead of stopping the worker.

Reviewed Zini club-name variants `APO Levadiakos` / `Levadiakos` and `AEK B` /
`AEK Athens B` are included in the shared team concordance. The public
[Sofascore profile](https://www.sofascore.com/football/player/zini/1002334) and
[FotMob profile](https://www.fotmob.com/players/1106943/zini) provide the matching
birth date and dated club history used to check this example.

### Reep ID concordance

On Main, run `python -m tools.refresh_reep` to refresh Reep's public bulk ID
bridges and rebuild the exported references. No Reep account or hosted API key
is needed. Bulk downloads are temporary; the retained index lives at
`data/_system/identity/reep.json`. It contains all published team bridges for
our three providers, plus player and match bridges relevant to saved local IDs
and reported worker match inventories. Refresh again after collecting new IDs.
The release, checksums and CC0 license are recorded in the index.

Main distributes a changed index automatically through direct reporting. Update
and restart Main and workers to enable this feature. A new index changes the
identity revision, making paused seasons eligible for a checkpoint retry.
Reep joins are typed (player, team, match), follow published redirects, and
never overwrite explicit reviewed player mappings. Unique team-ID bridges can
resolve different club names. Date, home/away side, score and lineup checks
still apply. Ambiguous or contradictory IDs are not guessed; existing name and
profile investigation remains available for identities absent from Reep.

Startup inventory validation and the offline coverage report use those same
team-ID checks for integrated WhoScored events. A provider name variant alone
does not invalidate a saved match with matching team IDs. Truly invalid raw
responses remain untouched and report the failed check (match ID, date, teams,
finished status, or events) alongside the file path.

`python dataset_builder.py --references-only` uses the saved index without
downloading anything. The four-source exports are:

- `dataset/concordance/match_ids.csv`
- `dataset/concordance/team_ids.csv`
- `dataset/concordance/player_ids.csv`
- `dataset/concordance/manifest.json` (release and coverage counts)

Each CSV includes `sofascore_id`, `fotmob_id`, `whoscored_id`, `reep_id` and
`identity_status`. Status is `linked`, `partly_verified` (some observed IDs have
no published bridge), `unmapped`, or `conflict`. Missing IDs remain blank, and
conflicting observed IDs are retained for review with no assigned Reep ID.
Player rows retain each club/season stint. Team exports also retain clubs that
have no FotMob ID yet. The existing top-level three-provider CSVs remain builder
references. These tables describe saved local collections: progress reporting
sends worker match IDs, not workers' complete player/statistics datasets. Import
those datasets before rebuilding a combined export across all computers.

If a worker reports `Chrome extension not connected` and then pauses, stop that
worker, pull the latest code, restart with `python main.py --worker-id msi --retry`
(use `headless` on Headless), and paste its newly printed connection code into
Soccer Collector in Chrome. Keep the extension connected. Saved checkpoints are
reused; a paused plan does not mean every match completed.

## MSI raw archive and dataset delivery

The shared `storage` section in `config/workers.yaml` assigns MSI as the archive
hub at `http://100.116.229.51:8767` and Main as a dataset consumer. Leave Tailscale
connected on all three machines. Allow inbound Python/TCP 8767 on MSI's Tailscale
interface in Windows Firewall; the service binds only to that private address.
Main's existing progress receiver remains on port 8766. No router forwarding is
needed.

For the initial upgrade, stop each worker, update the checkout, and start Main
first, followed by MSI and Headless:

```bash
python main.py --terminate
git pull --ff-only origin main
python -m pip install -r requirements.txt
```

On Main:

```bash
python main.py --worker-id main
```

On MSI / Headless, use the appropriate saved worker ID:

```bash
python -m tools.extension_setup
python main.py --worker-id msi
# On Headless instead: python main.py --worker-id headless
```

Reload the generated extension folder in Chrome if it was already installed.
Paste the connection code printed at startup and click Connect. The supervisor
keeps the code alive and waits before starting scraping or profile investigations.
An extension disconnect pauses further work; reconnecting lets an interrupted
season resume its checkpoints. Inventory auditing and background reporting can
continue while waiting.

Storage credentials are provisioned automatically through the existing
authenticated reporting connection. Each computer receives only its storage role
and credentials in ignored `data/_system/config/storage.json`. Main stores the
master storage secrets in ignored `storage-secrets.json` beside it. There is no
new token to copy. Existing reporting configuration and worker identities stay
in place. Provisioning and initial storage startup may take about a minute.

- Headless queues a saved match's active checkpoint and referenced provider raw
  files. A durable local queue uploads immutable, checksum-verified chunks to MSI,
  resuming at the last accepted offset if interrupted. Completed-season snapshots
  include the season's raw archive and active checkpoints. Retired checkpoint
  generations, machine settings, sessions, logs and live databases are excluded.
- MSI receives uploads while scraping. A separate background thread validates and
  incorporates completed seasons into
  `data/<league>/<season>/raw/<provider>` and their existing checkpoint locations.
  Collection credit remains with the original worker. Checkpoint batches have
  durable receipts; imports resume after interruption. A foreground request for
  the same season makes an import yield. Other seasons can import concurrently
  with scraping. A partially scraped season
  is stored by MSI but does not trigger a dataset publication.
- MSI builds the configured dataset tables and match/team/player concordance
  after its own completed seasons and after incorporating remote completed seasons.
  Failed builds keep the last published dataset intact and remain queued for retry.
  Builds use pinned input snapshots, so their export locks never block the live
  scraper. CSV exports and dataset builds are deferred out of supervised collection.
  Concordance can read saved match checkpoints even before flat CSVs are exported.
- Main polls MSI every 30 seconds and downloads new prepared dataset files,
  including `dataset/concordance/`. Its storage credentials cannot download raw
  objects. Existing Main dataset seasons are preserved. All files in a publication
  are verified before installation; an interrupted installation retries, and its
  receipt is written only when all files are installed. Individual destination
  files are replaced atomically; this is not an atomic swap of the entire dataset.
- Worker progress and logs still go directly to Main independently of data
  transfers. The worker cards distinguish queued uploads, received packages waiting
  for incorporation, incorporation/building, and datasets received by Main.
  Storage milestones appear in the default important-log filter. The archive card
  shows its background incorporation/build state separately from the current scrape.
  Large publication copies and dataset downloads no longer hold the upload lock.

Saved completed jobs from the current worker plan are queued once on startup, so
upgrading does not require re-scraping those seasons. Use the normal explicit
`--retry` flow for paused assignments or historical seasons outside that plan.
Keep MSI running to receive and build data even after its scraping plan finishes.

Background staging checks available disk space before each batch and keeps 1 GiB
free for collection. Low space pauses maintenance with source data retained.
Failed maintenance retries after five minutes instead of repeatedly blocking job
startup. Drive exports support verified, resumable, capacity-sized bundles; see
[the import instructions](../ensemble_data/README.md). Checkpoint archives use
short hashed paths, and Windows paths use the extended-length namespace.

Temporary transfer copies are removed after acknowledgment on the producer and
after incorporation on MSI. **Original worker files are retained**, as are Main's
existing raw archives. This upgrade does not delete or migrate those historical
copies automatically. No raw files are sent from MSI to Main. Transfer state,
receipts and temporary files live under `data/_system/storage/`; do not delete
that directory while an upload, incorporation or build is pending.

### Authority and safe assignment handoffs

There is one command path for supervised collection:

1. Edit and push `config/workers.yaml` from Main. It defines assignments; Git
   distributes that plan and code. Overlapping combined assignments are rejected.
2. Before starting a season, the worker refreshes its inventories from Main and
   requests ownership of that assigned season. Main checks the exact plan revision,
   worker identity and existing owner before granting a durable claim.
3. Every local inventory, including MSI's imported raw data, reports validated
   holdings and saved target schedules to Main. Main combines those reports and
   sends versioned instructions with the remaining matches/resources and explicit
   approvals for globally completed matches. MSI stores data; it does not assign
   work. Supervised skip decisions use Main's approvals, never an independently
   merged local inventory. Unknown schedules require discovery rather than being
   treated as complete. Assignment, upload receipt and dataset availability do
   not count as match completion. Failed audits withdraw that season's unverified
   holdings from the next report; original data remains available for repair.
4. Main can order a recheck when its remaining-work evidence changes, even if the
   worker's local job history says finished. The worker records the instruction
   revision it attempted, so unchanged incomplete evidence cannot cause a tight
   retry loop. Cached local files are still checked for validity when used.
5. MSI separately confirms receipt and incorporation of raw files, then publishes
   datasets after completed seasons. Main's dashboard reports these stages separately.

A claim has **no automatic expiry**. If a worker loses Main, its active season can
continue using its last instructions and queue reports/uploads locally.
A new season waits for Main. A new assignee cannot take the same season while its
old owner holds the claim, even if Main or that worker restarts. Each worker keeps
its private current ownership receipt under `_system/workers/active-assignment.json`.

Normal completion releases ownership at the next supervisor boundary. A worker
restart also releases its saved old receipt before asking for new work. For a
handoff, stop the old worker, update the plan, and restart it with the new plan so
it can release the receipt. Then the new worker receives authorization. Do not run
manual collection commands in parallel with supervised workers; manual collection
is an explicit local workflow outside this assignment coordinator.

If the old machine cannot restart, first verify its scraper is stopped. On Main,
inspect claims and clear only that stopped worker's season:

```bash
python -m collection.jobControl status
python -m collection.jobControl clear --worker-id headless --season premier-league/2022-2023 --confirm-stopped
```

Clearing a claim does not delete data or mark any match complete. Clearing while
the old scraper is still running would permit duplicate collection. Ordinary
network outages never clear ownership automatically.

### WhoScored match-local missing data and timeouts

Soccer Collector 0.7.1 separates the ordinary data-read deadline (normally
30 seconds) from the longer wait for an actual visible verification screen.
A local extension deadline is labelled `extension_timeout`, rather than being
reported as an HTTP 408 response. The Python client also understands older
extensions' synthetic 408 results.

Missing match data, timeouts and transient server errors do not open an hour-long
WhoScored extension cooldown. Old saved extension cooldowns for those errors are
ignored on upgrade. Actual rate limits and authentication stops remain in force;
verification still requires the visible extension workflow. If an identified,
fully loaded match page explicitly says statistics are unavailable and contains
no match data object, collection reports that match as unavailable immediately.
An unresolved timeout remains incomplete/retryable rather than proving permanent
absence. Other matches continue.

For a one-time Main-to-MSI raw-data copy, use the portable
SD-card workflow in [ensemble_data/README.md](../ensemble_data/README.md). It keeps
machine tokens/settings separate and verifies every copied data file before import.


Update Main and restart its monitor **before** upgrading workers to the
instruction protocol. A new worker waits if Main cannot supply matching
instructions. The authenticated reply is saved in ignored
`data/_system/workers/main-instructions.json`; no new token is needed. Instructions
refresh with the background reporting poll and each new job authorization.


### Reporting catch-up

`python -m collection.reporting status` checks Main's reachability, token, original
worker identity and queue identity. “Connection OK” does not mean Main has
acknowledged the queued reports. A large queue with repeated acknowledgement
timeouts can mean Main is overloaded even while that health check succeeds.
Update and restart Main first, then update workers between saved jobs. Keep the
original reporting databases: acknowledged batches are removed automatically,
and retries do not duplicate logs. Identity observation timestamps no longer
trigger repeated uploads of unchanged evidence. Reporting requests allow 30
seconds for acknowledgement; inventory downloads still use a separate connection.


### Optional profile checks on Main

Workers record ambiguous player identities and send candidates to Main. They no
longer visit player profiles during collection or while idle. To investigate the
shared queue, run Main with:

```bash
python main.py --worker-id main --identity-review
```

Paste the connection code printed immediately by Main into Soccer Collector.
Profile requests wait for that connection. Main checks birthdays, current clubs
and careers in the background while monitoring and reporting continue. Provider
APIs use the connected extension as their fallback. Main reuses the same bridge
for all three providers and shares confirmed identities back to workers. The
flag is optional and accepted only on the configured coordinator; ordinary Main
launches continue to monitor without opening an extension connection.

Identity review compares provider records in separate columns, including career
history and aligned match references. The suggestion score is not displayed.
Existing evidence and reviewed decisions remain intact. Update/restart MSI and
Headless to remove their old inline and idle profile investigation paths.


### Automatic reconnection, remaining games and frozen completion

Soccer Collector 0.8.0 keeps its connection credentials in private local extension
storage. Python saves its matching loopback port and token in the ignored
`data/_system/config/extension-session.json`. Normal code restarts reuse this
connection. Temporary network errors retry automatically; Chrome startup restores
the saved connection and can recreate its provider tab. Clicking Disconnect clears
automatic reconnection. A manual Pause still requires Resume. Keep normal Chrome
running while collecting; website verification may still require attention.

After this upgrade, update the unpacked extension folder, reload the extension in
Chrome and connect the newly printed code once. If using the prepared extension,
run `python -m tools.extension_setup` (or use its `--output` for the folder you
already loaded) before clicking Reload in Chrome. Subsequent normal worker
restarts reuse that pairing; do not delete the local session configuration.

Main's granted assignment now supplies the exact remaining match IDs before
provider fixture discovery or checkpoint loading. Resolved games are omitted in
one pass, while progress retains the full season target. Unknown schedules are
still discovered normally. Completed seasons are frozen for their specific
required configuration and excluded from routine audits and future scrape jobs.
Use Reopen season on Main to reassess them. Changing required data creates a new
completion scope. Worker `--retry` retries unfinished work; it does not override
Main's frozen Done decision.


## Main identity review and extension upgrades

Run `python main.py --worker-id main --monitor --identity-review` to check player
profiles on Main. Leave Chrome open and connect Soccer Collector to the code
printed by Main. Main checks the connected extension supports profile lookups;
if it is too old, the identity review page asks you to reload it instead of
recording lookup failures. After reloading the current extension and reconnecting,
Main retries lookups that failed with an incompatible-extension error immediately.
Successful profile evidence and human identity decisions are preserved.

After pulling code, prepare the folder loaded in Chrome with
`python -m tools.extension_setup` (or pass `--output` with its original folder).
Then open `chrome://extensions`, click Reload on Soccer Collector, and confirm
its popup shows version 0.8.1 or later. Git changes alone do not reload Chrome's
running extension. A source-folder installation can simply reload that extension.

## Bundesliga promotion/relegation playoffs

Bundesliga jobs retain Sofascore's two end-of-season promotion/relegation playoff
matches. FotMob fixtures combine Bundesliga (54) with Bundesliga Qualification
(9081), deduplicated by FotMob match ID. Missing supplemental coverage does not
discard the regular league fixtures. Provider match IDs are matched in the usual
way, so playoff statistics and events use the same collection pipeline.

Dataset `fixtures.csv` keeps `competition=Bundesliga` and adds
`competition_phase=playoffs` plus a `competition_note` explaining the separate
FotMob feed. Other main-draw games have `competition_phase=main`. Provider round
names remain intact. The source schedule and match IDs are unchanged. Done
seasons remain frozen; explicitly reopen a season on Main to retry previously
closed playoff gaps.


WhoScored profile review retains birthdays and recorded History club-seasons even
when Current Team is blank. Missing fields remain unavailable and block automatic
confirmation; they do not erase the rest of the profile. API pages without a
loaded History table fall back to the connected extension. Version 0.8.2 also
retains the validated header if the History table fails to load before its normal
deadline. Old "needs its loaded career table" failures are retried on Main after
upgrading. WhoScored History contains competition participation seasons, not a
complete transfer registry; repeated club-seasons are shown once.

Identity review groups repeated observations by provider/baseline match IDs and
side. It keeps richer fixture names and reporting-worker provenance while showing
one match row per fixture pair. Existing review decisions remain unchanged.


Player proposals with two known birthdays more than one calendar month apart are
automatically rejected and removed from manual review. The limit is measured
from the earlier birthday to the same day of the next month (clamped at month
end). Equal-to-one-month, smaller differences, and missing birthdays remain for
review; they never establish an automatic match. Existing human decisions are
preserved. Automatic birthday rejections keep their dates and reason in reviewed
history and are shared with workers like other rejected identity pairs.
