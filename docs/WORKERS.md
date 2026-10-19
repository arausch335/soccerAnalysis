# Shared worker plan and collection dashboard

Run `main.py` in PyCharm with no arguments, or run `python main.py`. It starts
only the assignments belonging to this computer and opens
`http://127.0.0.1:8765` in your browser. It remains running after its plan has
been attempted, waiting for changes. Use Ctrl+C to stop it.

The initial plan preserves Main Mac (`main`): the seven existing competitions,
2023/24, `combined`, and `fast` spatial capture. MSI (`msi`) has no assignments
and waits without making scraper requests. Historical inventories for other
worker IDs remain visible as “Inventory only.”

## The two shared files

- `config/settings.yaml`: common provider access, delays, dataset options and
  paths. These preferences are shared by Git on every computer. Its competition,
  season, type and spatial selections are defaults for manual commands and the
  standalone launchers; worker assignments override those four selections.
- `config/workers.yaml`: the coordinator, named computers, colors, assignments,
  dashboard preferences and Git update policy. Edit this file on Main Mac,
  commit, and push it. Each worker reads the same plan.

The only computer-specific identity is `data/identity.json`, which stays out of
Git. Existing identities are reused. On the original MSI data folder, launch:

```bash
python main.py --worker-id msi
```

The ID must appear in the shared plan. Each ID belongs to exactly one data
folder, identified by its existing `instance_id`. Do not create another folder
with an ID already represented by a worker inventory. Copies intended for
import go in `ensemble_data`, where imports preserve the receiving identity.

An assignment expands each competition × season into a saved season job:

```yaml
assignments:
  - id: premier-events
    competitions: [Premier League]
    seasons: ['2022/23']
    collection_types: [whoscored_events]
    spatial_capture: fast
```

Types are `combined`, `sofascore`, `whoscored_events`, and `fotmob_players`.
`combined` includes Sofascore plus WhoScored/FotMob enrichment and the saved
WhoScored event responses. Standalone FotMob statistics are a separate type.
`fast` collects average positions; `all` requests every supported spatial
endpoint. Overlapping data-type assignments for the same competition/season
are rejected before collection. Disabled workers do not receive jobs.

## What the dashboard means

Overview shows distinct **provider match records**, deduplicated across worker
inventories. Sofascore and WhoScored IDs remain separate; these totals are not
a count of unique real-world fixtures. Each computer’s card distinguishes
records held locally from records originally collected there. Match origins
are historical match-level evidence, not proof that every field came from the
same original computer.

Worker plan shows colored assignment bars and all requested data types for
each computer. Coverage distinguishes player statistics, team match statistics,
incidents, momentum, average positions, heatmaps, shots, action maps, provider
enrichment, native WhoScored events and standalone FotMob statistics. It shows
saved, partial, unavailable, deferred, failed, unknown, and missing states.

Coverage targets use saved schedules on the computer serving the dashboard,
including any extra inventoried matches. An absent schedule means “unknown,”
not zero or 100%. Old inventories cannot distinguish successful spatial data
from terminal unavailable/deferred attempts; the dashboard shows those as
unknown. Before its first pending assigned job, a worker audits its local files
and adds explicit resource availability. Unassigned workers remain idle. `python main.py --inventory` can also perform that audit
without scraping; stop that worker before running a manual audit. The dashboard
itself only reads snapshots and schedules.

Other computers’ activity is explicitly a **last published snapshot**, not a
live connection. New snapshots are published between season jobs. Local
activity updates after schedules and individual saved matches. Raw data still
needs a folder transfer before a different computer can build datasets from it.

```bash
python main.py --dashboard       # View only: no scraping, migration or Git sync
python main.py --once            # Attempt pending assigned jobs, then exit
python main.py --retry --once    # Revisit the plan using resumable checkpoints
python main.py --no-dashboard    # Worker without a browser dashboard
```

A job is recorded as attempted, with its exit code. Paused jobs stay visible
and require `--retry` to revisit; failed provider requests are not retried in an
endless loop. Adding assignments or changing shared settings makes affected
jobs eligible again. Manual explicit commands such as `--competition`,
`--season`, `--collection-types`, `status`, `--inventory`, and `--import-only`
retain the previous CLI behavior. They do not start automatic Git sync.

## Automatic Git updates

The shared plan defaults to checking `origin/main` every 120 seconds.

1. A background thread only **fetches** refs while a season job runs.
2. After the job saves and returns, the worker updates its checkout. It uses a
   fast-forward update; it never stashes, resets, or overwrites user edits.
3. Changes to code or shared configuration close the worker and dashboard, then
   restart Python before another job begins. Saved checkpoints and attempted
   job state survive. A removed assignment cannot continue into another job.
4. Only this worker’s `ensemble_data/workers/<id>.json` inventory and sanitized
   `ensemble_data/runtime/<id>.json` status may be committed and pushed
   automatically. These contain football IDs, worker labels, statuses and
   timestamps. Raw data, identity, profiles, cookies and detailed errors remain
   ignored. Configure normal Git credentials and commit identity beforehand.

Git must be on the configured branch. Uncommitted source/settings changes,
staged files, unrelated local commits or an unfinished Git operation pause
automatic synchronization and appear in worker status; local collection can
continue with its loaded configuration. Only the worker’s own generated
snapshot commits may be rebased after a competing push. A conflicting snapshot
rebase is aborted. No force pushes or automatic source commits are used.
If another checkout changes this worker’s snapshots, sync pauses for an identity
check. Snapshot backups are retained under ignored `data/worker_runtime/git-backup`.

Set `git.enabled: false` to disable Git actions, `publish_inventory: false` for
pull-only synchronization, or `auto_restart: false` to stop after applying a
code/config update and restart manually. New package dependencies still need a
normal environment installation; the updater does not run package installers.
An already running older version needs one manual pull and restart to gain this
supervisor. The local dashboard binds only to loopback and has a read-only API.

## First update on another computer

Keep data and local identity in place. Check changes before pulling:

```bash
git status --short
git pull --ff-only origin main
python main.py --worker-id msi
```

If `settings.yaml` has older machine-specific selections, back it up outside
Git first. Move work selections into `workers.yaml` on Main Mac, preserve any
common preferences in the shared `settings.yaml`, and commit those changes.
Git intentionally refuses to overwrite uncommitted conflicting settings.
Keep credentials in environment variables and browser profiles in ignored data
folders. Use relative paths in the common settings to avoid machine-specific
absolute paths.
