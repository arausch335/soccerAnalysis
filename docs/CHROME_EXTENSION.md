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
2. In normal Chrome, open `chrome://extensions` and enable **Developer mode**.
3. Choose **Load unpacked** and select this repository's `chrome_extension`
   folder. Select the folder containing `manifest.json`.
4. Pin **Soccer Collector** to the Chrome toolbar.

If you installed the Python package instead of using a Git checkout, run
`python -m tools.extension_setup` to print the installed extension folder.
There are no npm dependencies or JavaScript build steps. When an update changes
extension files, click its **Reload** button at `chrome://extensions`.

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
- If verification appears, Python pauses and the extension displays **!**.
  Complete the verification yourself in Chrome, then click **Resume**. The
  program does not interact with verification controls. Verification waits use
  `collection.browser.manual_wait_seconds` (120 by default); if this expires,
  rerun the collection when ready. A browser/extension 403 creates no day-long
  cooldown. An explicit 429 still pauses according to the shared rate-limit
  ledger.
- **Disconnect** releases the chosen tab and pauses an active Python request.
  Saved responses/checkpoints remain. A subsequent Python run prints a new code.
- Closing Chrome, closing the chosen tab, or leaving Sofascore causes a resumable
  failure. No other tab is selected automatically.

Navigation, resource requests and UI clicks are paced with
`collection.browser.delay_min_seconds` / `delay_max_seconds` (2–4 seconds by
default). UI click spacing supports up to 60 seconds; unusually long delays may
require increasing `timeout_seconds`. The additional verification window does
not remove a provider's access restrictions.

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
