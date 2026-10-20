# Soccer Collector extension

The combined collector always prints a local connection code at startup. Connect
one chosen Chrome tab on Sofascore, WhoScored or FotMob. APIs are attempted first;
the extension remains idle until needed and switches that same tab between sites.
No separate Chrome profile or Playwright collection is used.

Prepare/reload after pulling an update:

```sh
python -m tools.extension_setup
```

Load `build/soccer-collector-extension` from `chrome://extensions`. Version 0.6.0
adds FotMob host access, which Chrome needs to accept. Then run the worker's usual
command, paste its fresh connection code and click **Connect this tab**. Keep the
selected tab open. The extension controls only that tab. Main in monitor mode
does not need an extension.

## Pace and recovery

Extension navigation/click delays use `collection.extension.delay_min_seconds`
and `delay_max_seconds` (0.5–2 seconds). WhoScored extension requests use this
short pace rather than its 15–30 second API request spacing.

**Pause** waits after the current operation; **Resume** continues the connected
request. Closing the selected tab, disconnecting or expiring a command preserves
saved data and requires reconnecting/resuming. A provider page failure does not
itself invalidate the shared connection for the next provider.

Sofascore verification pauses collection and schedules real page reloads after
1, 5 and 30 minutes. Ready, cleared pages can resume automatically after a
confirmation read. If the scheduled recovery cannot finish, inspect the selected
tab and use **Resume**. No verification control is clicked automatically.
WhoScored displays verification instructions in the chosen tab and waits for
manual completion. Actual 429 rate-limit waits apply across both transports.

Operational traces go to `data/_system/logs/extension/` and are forwarded to Main.
Credentials and raw page bodies are excluded from those logs. Request recovery
uses the common compressed cache under `data/_system/cache/`; authoritative
responses stay in each league season's `raw/` directory.
