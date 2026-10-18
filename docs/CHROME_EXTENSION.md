# Collect through your normal Chrome session

Soccer Collector is a JavaScript Chrome extension connected to the Python
collector. You open Sofascore in your existing Chrome profile, select that tab,
and explicitly connect it. Python coordinates the season, validates identities,
saves checkpoints, builds the normal CSVs, and prints timing/ETAs. JavaScript
navigates the chosen match tab, opens lineup/player panels, and reads known
Sofascore data resources from that page's own session.

This mode does not launch Playwright or open a second browser profile. It uses
an authenticated local HTTP connection on `127.0.0.1` with a fresh connection
code per Python run. No system-wide native-messaging registration is required.
It does not guarantee that Sofascore or Cloudflare will accept collection.

## Install once

1. Pull the latest `main` and install the normal Python requirements.
2. From the repository folder, run `python -m tools.extension_setup`. This creates
   `build/soccer-collector-extension` and prints its full path.
3. In normal Chrome, open `chrome://extensions` and enable **Developer mode**.
4. Choose **Load unpacked** and select the **prepared folder printed by the
   command**, containing `manifest.json`.
5. Pin **Soccer Collector** to the Chrome toolbar.

The same command works with an installed Python package. Use
`--output /path/to/an/empty/folder` to choose another location. There are no npm
dependencies. After pulling an extension update, rerun the preparation command
and click **Reload** at `chrome://extensions`.

If Chrome reports that `__init__.py` or another underscore-prefixed filename is
reserved, prepare the folder with the command above and load that folder rather
than the source/package directory. It copies only Chrome runtime assets and
excludes Python files, `__pycache__`, tests and other development files. Existing
local Python caches do not need to be deleted. A fresh prepared folder works
even if those caches remain in the checkout.

## Try one match first

Open any Sofascore match in normal Chrome, then run this from the repository:

```bash
python main.py --collection-types sofascore --sofascore-transport extension --competition "Premier League" --season 2024/25 --limit 1
```

This selects the first finished game in that season, so the chosen tab may
navigate to another match. The normal exports and existing season checkpoint
paths are used. The extension requests the additional per-player statistics,
team statistics, incidents and momentum used by the existing visible-browser
collector; `spatial_capture: fast` also requests average positions. Use
`--sofascore-spatial-capture all` to request the other spatial categories.

Python prints a connection code beginning with `http://127.0.0.1:`. With the
Sofascore tab selected, open Soccer Collector, paste the **entire code**, and
click **Connect this Sofascore tab**. It applies only to this chosen tab. Keep
it open while collection runs. Do not paste the code into Sofascore itself.

For a connection-only match-details check, use:

```bash
python main.py probe --collection-types sofascore --sofascore-transport extension --match-id 12436563
```

To package the one-match exports with the usual dataset builder, add
`--build-dataset`. Otherwise the raw responses and per-match checkpoints remain
saved; run `dataset_builder.py` to export/package them afterwards.

## Run from PyCharm with configuration

Choose the desired competitions/seasons under `collection` and set:

```yaml
collection:
  types: [sofascore]
  sofascore_access: extension
  action: auto
  spatial_capture: fast
  build_dataset: false
```

Keep the other settings in the supplied YAML file. Run `main.py`, then connect
the extension using the code it prints. Start with one competition and one
season and put `--limit 1` in PyCharm's run parameters for the first test.
Remove that limit after checking the saved match. A single connection is reused
across all the competitions/seasons in that `main.py` run. `combined` is also
supported: only Sofascore uses the extension, while FotMob/WhoScored retain
their own clients and settings.

## Pause, verification, disconnect

- **Pause** stops after the current step. Its wait is excluded from command
  timeouts, so Python can wait until **Resume**. Per-game elapsed time includes
  human waits and can affect the ETA.
- A visible verification challenge or denied data request (403) pauses Python
  and the extension displays **!**. A denied background request can happen
  without a visible checkbox; the message now distinguishes these cases.
  The extension passively watches for the browser's automatic clearance. Once
  match controls return and remain ready for four seconds, one confirmation
  request checks access. Successful clearance resumes collection automatically.
  If access remains denied, collection stays paused without expiring; inspect
  the tab and use **Resume** when ready, or **Disconnect** to stop. No challenge
  controls are clicked. `manual_wait_seconds` still controls the initial
  extension connection window and the separate Playwright verification wait;
  extension verification itself does not time out. A 403 creates no day-long
  cooldown. An explicit 429 retains its shared rate-limit wait.
- **Disconnect** releases the chosen tab and pauses an active Python request.
  Saved responses/checkpoints remain. A subsequent Python run prints a new code.
- Closing Chrome, closing the chosen tab, or leaving Sofascore causes a resumable
  failure. No other tab is selected automatically.

Navigation, resource requests and UI clicks are paced with
`collection.browser.delay_min_seconds` / `delay_max_seconds` (2–4 seconds by
default). UI click spacing supports up to 60 seconds; unusually long delays may
require increasing `timeout_seconds`. The additional verification window does
not remove a provider's access restrictions.

## Recovery and diagnostic logs

Click delays are sampled independently between the configured minimum and
maximum for each click. Setting both values equal produces a fixed delay.
The diagnostic trace includes the actual sampled click delays.

The extension waits for match controls to appear rather than waiting for all
advertisements and background resources to finish loading. If match content
does not appear, it reloads the intended match once. A transient page-script or
network failure can reopen the intended match and retry the operation once.
It does not browse random links or navigate unrelated pages. A 403 waits for
automatic clearance or manual Resume, and a 429 retains its rate-limit wait;
neither is retried as a transient page error. After recovery is exhausted,
Python saves the checkpoint and pauses rather than continuing with an unusable
connection. Running the same command again resumes incomplete work.

Python prints the full diagnostic log path before the connection code:
`data/extension_diagnostics/sofascore/<UTC timestamp>-<run id>.jsonl` (relative to
the configured data root). Each line is a JSON record containing a UTC timestamp
and an operation/connection event. It records requested match/player IDs or
endpoint, current match URL, page title/readiness, visible challenge evidence,
player-panel/dialog counts, observed clicks/delays, HTTP status, recovery
attempts, errors and elapsed time. Compare `command_started` and
`command_finished`/`command_failed` to find the exact failing step. This is
operational metadata, not a full page recording or screenshot; response bodies,
cookies, request headers, connection codes and tokens are excluded. Logs stay
local and are ignored by Git. Share the log from a failed run when reporting
an extension problem. Offline cache-only runs do not start a trace.

Each attempted browser/extension match also prints a `[Coverage]` summary and
updates `collection_coverage.csv` inside its competition/season collection
folder. The report has competition, season, match/teams, resource, status and
reason columns. Statuses distinguish `complete`, `unavailable` (such as 404),
`failed`, `visible_only` (Playwright fallback fields), `skipped_not_played`,
`not_attempted` after a pause, and spatial `deferred` requests. Details already
collected before a later failure are preserved. `browser_coverage.json` keeps
the same resource evidence with structured errors. The report describes
requested resources, not a guarantee that every statistical field exists;
unavailable fields remain NA in the normal exports. Resources outside the
selected spatial configuration are not requested. Matches not reached yet
remain `pending` in `collection.csv`.

## Data and limits

The extension uses the same Sofascore IDs, raw field meanings, and normal
`player_match_stats` schema. Available values are validated by the existing
loaders and exported through the existing dataset builder. Unavailable values
stay NA; observed zeroes stay zero. Spatial/detail endpoints returning 404 are
recorded as unavailable. Identity mismatches or rejected responses cannot be
recorded as complete data.

The source-specific browser response cache is
`data/extension_cache/sofascore/`. Match checkpoints, coverage and normal raw
responses stay under the selected competition/season. Coverage records identify
the extension access method. Offline replay uses saved responses without
starting the local server or connecting Chrome.

Permissions cover `www.sofascore.com`, a local `127.0.0.1` connection, extension
session storage, page scripting, and a reconnect alarm. The extension never
sends its connection token into the site page, exports Google credentials, or
reads another browser profile. Connection details live in extension session
storage rather than Chrome Sync and change on each Python run. The local server
accepts only authenticated commands/results from the connected extension,
rejects unrelated web origins/Host headers, and limits message sizes.

The first implementation has automated tests for schema parity, individual
player requests, caching, manual verification, pause/resume, authentication,
origin/endpoint restrictions, and an actual installed extension talking to
Python against intercepted local fixtures. **A live match in your everyday
Chrome still needs checking:** synthetic tests cannot establish Cloudflare
acceptance or prove every historical endpoint is available on the provider.

## Development checks

```bash
python -m unittest discover -s tests -q
node --test chrome_extension/tests/*.test.js
```

The optional end-to-end extension test needs Playwright and its Chromium test
browser. It uses an isolated profile and intercepted local fixtures:

```bash
python -m playwright install chromium
SOCCER_EXTENSION_UI_TESTS=1 python -m unittest tests.test_extension_ui -v
```

On Windows, set `SOCCER_EXTENSION_UI_TESTS=1` in the run environment rather than
using the shell assignment shown above. Playwright is a test dependency only;
the actual extension collector needs the normal Python requirements and Chrome.
