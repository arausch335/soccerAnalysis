# HTTP identities and visible-browser Sofascore collection

Install into your existing environment, from the repository root:

```bash
python -m pip install ".[browser]"
```

Use installed Chrome by default. For installed Edge, set `channel: msedge`.
Alternatively, set `channel: chromium` and install the matching browser with
`python -m playwright install chromium`. Browser support is optional: ordinary
HTTP collection and offline dataset building do not require Playwright.

## Run from PyCharm

Edit `config/settings.yaml`, then run one of these files:

- `main.py`: normal provider workflow, with the configured Sofascore transport.
- `sofascore_browser.py`: visible-browser Sofascore collection, requesting all
  spatial categories. It uses the same competition/season selections, data root,
  checkpoints and CSV schema. Existing seasons can be resumed and upgraded.
- `dataset_builder.py`: rebuild the selected outputs from saved data offline.

`sofascore_browser.py` inherits `collection.action` and `collection.build_dataset`.
With `build_dataset: false`, collection saves checkpoints and raw data; run
`dataset_builder.py` or pass `--build-dataset` to produce packaged CSVs.

```bash
python main.py --competition "Premier League" --season 2024/25 --limit 2
python sofascore_browser.py --competition "Premier League" --season 2024/25 --limit 2 --build-dataset
python -m collection resume --name premier-league-2024-25 --sofascore-transport browser --providers sofascore --sofascore-spatial-capture all
```

The last command also rebuilds collection CSVs unless `--no-build-csvs` is used.
Explicit provider/spatial selections apply on resume; saved raw responses and
unrelated local data stay in place. A browser run revisits an existing season
when browser detail or requested spatial coverage is unfinished.

## Configure the two transports

```yaml
transport:
  sofascore_mode: auto
  http:
    profiles: [chrome, chrome131]
    languages: ["en-US,en;q=0.9"]
    proxy_env: SOCCER_PROXY_URL
    delay_min_seconds: 8
    delay_max_seconds: 15
    attempts: 2
  browser:
    channel: chrome
    delay_min_seconds: 0.5
    delay_max_seconds: 1.5
    timeout_seconds: 30
    manual_wait_seconds: 120
```

`http` uses the HTTP client only. `browser` opens a visible browser when uncached
data is needed. `auto` begins with HTTP, then switches once to the browser for
that run if HTTP is blocked, unavailable, or returns invalid data. It does not
switch on a 404, authentication error, or rate limit. Existing HTTP 403 cooldowns
remain saved, while a normal browser session may still be tried. Explicit 429
waiting periods apply to both transports. An unavailable browser dependency or
browser installation produces a setup message rather than discarding data.

HTTP browser profiles are selected once and saved in
`data/http_sessions/<provider>/identity.json`. The resolved profile and locale
remain consistent across resumes. The library supplies matching User-Agent,
client hints and connection behavior. HTTP cookies are saved separately with
restricted file permissions; their domains and expiry remain intact. Selecting
a different profile starts with a fresh cookie jar. The HTTP settings also apply
to WhoScored and FotMob; WhoScored retains its separately configured request
spacing.

An optional HTTP(S)/SOCKS proxy URL comes from the named environment variable.
Set it in your IDE's run configuration or shell, not in YAML. Credentials are
not included in identity manifests or transport diagnostics. The visible browser
uses the same optional proxy setting and a dedicated persistent profile in
`data/browser_profiles/sofascore`. Its browser identity comes from installed
Chrome/Edge; HTTP header/profile overrides are not injected into that browser.
No VPN subscription, system VPN configuration, or automatic IP rotation is added.
Keep browser profiles and cookie files private when transferring collection data.

Browser action spacing is separate from the slower HTTP pacing. The default is
0.5–1.5 seconds of extra spacing; set both values to zero to remove that added
wait. Rendering, panel loading and serial collection still take time. A visible
browser can receive a rate limit, so 429/Retry-After responses still pause it.
Neither mode runs parallel match requests.

## Data coverage and provenance

The visible client first opens the match page and uses the actual lineup,
statistics and player panels. It captures native structured responses from the
website. Where a resource has not loaded through a panel, it requests that
first-party resource inside the loaded browser session. This is still dependent
on Sofascore serving the resource; a browser cannot reconstruct absent data.

For each match it attempts:

- Both full squads, formations, shirt numbers, starters and substitutes.
- Every played player's match-statistics object, including passing, shooting,
  defending, goalkeeper fields, physical measurements and any additional raw
  fields supplied for that match. The ordinary metric mappings populate the
  existing player_match_stats columns.
- Team match statistics, incidents and momentum.
- Average positions, player heatmaps, match shots and player action maps when
  requested and available. The standalone browser entry point requests all of
  these spatial categories.

Match, player and team IDs remain Sofascore IDs. No `fmb_` prefixes are introduced
for this transport. Missing measurements stay NA; observed zero values stay zero.
Unused substitutes retain lineup information without invented playing stats.
Structured player fields are merged into the original raw/checkpoint records,
including fields without a current flat-column mapping. Existing provider
enrichment is retained when an older checkpoint is upgraded.

Browser captures are cached in `data/browser_cache/sofascore`, with endpoint,
source page, timestamp and capture method. Match checkpoints record transport
provenance and detail coverage. Each collection's `browser_coverage.json` lists
complete, unavailable, failed, or visible-only detail resources. Team statistics,
incidents and momentum are retained in each season's raw payloads for downstream analysis.
Team statistics are also exported to `sofascore_team_statistics.csv` and the combined,
provider-labelled `team_match_statistics.csv`; dataset packaging uses the latter
for `team_match_stats.csv` when available. Existing FotMob-only exports remain readable.

If full player JSON is unavailable but the correct player's statistics are
visible, a conservative parser preserves supported rendered numeric fields.
It does not infer totals from rounded percentages or derive coordinates from
lineup diagrams. Such a match remains partial because full structured coverage
has not been verified. A true 404 is recorded as unavailable; a timeout, wrong
identity, malformed payload or blocked panel is a failure, not missing-by-design.
Sofascore's older action-map endpoint can return 404 even when scalar passing
and dribbling statistics are present. Those scalar values remain usable.

## Ads, overlays and interruptions

The browser blocks known advertising-network hosts while leaving first-party
scripts, styles and player images available. It closes unsolicited popup tabs,
dismisses recognized ad-close controls, rejects optional consent when that
explicit control exists, and handles the website's limited-access interstitial.
It never clicks advertising links or generic agreement/download buttons.

A late overlay gets one dismissal/retry. If a UI control remains inaccessible,
the collector tries the corresponding resource within the existing page session.
Unresolved failures save a screenshot and rendered text under
`data/browser_diagnostics/sofascore` and leave the match partial. Network traces,
request cookies and proxy passwords are not dumped there. New ad providers or
layout changes may require updating selectors or the ad-host list.

A verification page waits for manual attention in the visible window for the
configured time, then pauses if unresolved. Closing the window or interrupting
Python preserves responses already written. Run the same selection again to
resume. The collector does not reset saved provider cooldowns to force retries.

## Validation

Offline regression tests cover transport selection, a single sticky fallback,
cache identity, wrong-match/player rejection, proxy-secret redaction, cookie
persistence, 429 waits, zero added browser delay, parsed missing values, and
fallback beginning during spatial collection. A real visible-Chrome integration
test uses intercepted local fixture data to exercise an ad overlay and player
panel without contacting the live site.

A live sample (Chelsea–Liverpool, 4 May 2025, event 12436563) produced 40 squad
rows, 30 played-player statistics records, and average positions for all 30
played players. Team statistics, incidents, momentum, shots and heatmaps were
retrieved. The legacy action-map endpoint returned 404 and was recorded as
unavailable. This validates the sample, not coverage of every competition or
historical match.

Additional live checks retrieved season discovery and populated lineups for an
Arsenal–Leeds match in 2021/22 and Fulham–Bournemouth in 2025/26. A single live HTTP
attempt failed and successfully selected the saved browser transport fallback.
