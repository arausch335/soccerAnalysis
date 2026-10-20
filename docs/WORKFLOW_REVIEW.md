# Combined collection workflow and storage

## Collection

1. A scraping worker creates its local extension connection and prints the code
   before the inventory audit or its first season. Monitors do not start one.
2. Combined collection attempts Sofascore, FotMob and WhoScored through their APIs.
3. On access/invalid-response failure, that provider switches to the connected
   extension for the rest of the run. It opens the appropriate supported site
   in the chosen tab. This includes FotMob. A 429 is a shared rate-limit wait;
   a 404 is recorded as missing data, not treated as a transport failure.
4. Save provider raw data and validated match checkpoints before reporting
   progress. Partial matches remain retryable and retain their successful data.

Direct Playwright collection and standalone Sofascore/WhoScored/FotMob launchers
are removed. Historical artifact readers remain only to preserve imports,
offline exports and migrations; historical writers live only in test fixtures.
Existing settings with old transport keys upgrade in memory. Worker identities,
private connection settings and assignments are preserved.

## Data structure

```text
data/
├── premier-league/
├── bundesliga/
├── la-liga/
├── serie-a/
├── ligue-1/
├── champions-league/
├── europa-league/
│   └── YYYY-YYYY/
│       ├── raw/
│       │   ├── sofascore/
│       │   ├── fotmob/
│       │   └── whoscored/
│       ├── match_checkpoints/
│       ├── schedule.json, job.json, collection.csv
│       └── exported tables and coverage metadata
└── _system/
    ├── config/          # identity + private reporting settings
    ├── workers/         # runtime and reporting SQLite databases/queues
    ├── logs/extension/  # operational extension traces
    ├── network/         # API sessions/cookies and pacing state
    ├── cache/
    │   ├── responses/   # compressed request recovery cache, ~256 MiB total
    │   └── recovered/   # unique historical responses; retained until raw save
    ├── fixtures/        # supporting fixture schedules
    ├── imports/         # import journals and retained provenance
    ├── analysis/        # existing actions/events/VAEP Parquet + reference CSV
    ├── locks/
    └── provenance.json
```

The hidden `.soccer-scraper.json` root marker identifies the data directory.
Existing retired cache/profile folders move under `_system` until cleanup;
unique or unrecognized data is preserved. No live Chrome profile is removed.

## Source structure

```text
main.py                         # combined/manual or worker entry point
config/{settings,workers}.yaml
collection/
  cli.py, seasonRunner.py        # combined workflow and saved-data export
  worker.py, codeUpdates.py      # assignments and updates between jobs
  reporting.py, workerRuntime.py # receiver, queues, live progress and logs
  ensemble*.py, whoScoredData.py  # data/provenance imports and migrations
providers/
  *Client.py, *Loader.py         # provider APIs and validated data decoding
  identity.py, concordance/teams.json   # fixture/player matching and team concordance
  transports/
    sofascore.py, fotmob.py, whoscored.py
    bridge.py, diagnostics.py
    sofascore_extension.py, sofascore_payloads.py
chrome_extension/
  worker.js, protocol.js
  page.js, who_page.js, fotmob_page.js
storage/
  layout.py, requestCache.py, pandasStore.py
tools/
  compact_cache.py, extension_setup.py
  migrate_whoscored.py, migrate_data_layout.py
dashboard/, models/, analysis/, dataset_builder.py
```

## Disk cleanup

The initial local audit found approximately 8.9 GB of WhoScored HTTP responses
and 1.9 GB of FotMob HTTP responses outside season raw data. These are candidates,
not a promise that all bytes can be deleted. The cleanup previews its actual
savings, checks parsed response equality against canonical raw data before
removing duplicates, and verifies compression before replacing unique responses.
The initial preview is read-only. Applying cleanup takes the worker and
collector locks and refuses to run while either is active.
