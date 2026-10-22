# Import copied collections

Main remains the monitor; MSI is the central raw archive. Worker scraping,
background inventory, imports and dataset publication run independently.

Prepare a verified portable bundle on Main (replace the example card path):

```sh
python -m tools.prepare_transfer --session main-to-msi --output "/Volumes/CARD/main-data-01"
```

The helper checks free drive space **before** copying. It accounts for the card's
allocation blocks (often 128 KiB on exFAT), rounds up each file and directory, and
reserves at least 512 MiB or 2% of available space, whichever is larger. A named session exports complete checkpoints with their raw dependencies
in independently verifiable bundles that fit the available space. Optionally add
`--max-gib 20` to cap the file contents of one bundle further; filesystem
allocation is checked separately. If more data remains, deliver the first
bundle, then repeat with the **same session** and a **new output name**, such as
`main-data-02`. The local session receipt skips files already exported; keep or
successfully import each earlier bundle before reusing the card. A new session
name starts a fresh export. Original Main files are never deleted.

If the copy is interrupted or free space changes, **rerun the identical command**
with the same output path and options. The helper preserves verified card files
in a sibling folder such as `.main-data-01.incomplete/`, together with a pinned
source snapshot and plan under Main's
`data/_system/storage/portable-exports/in-progress/`. It checks completed files
and reuses them, recopies damaged or partial files, and only publishes the final
bundle after verification. Do not delete these recovery folders or try to import
an `.incomplete` folder. They are cleaned automatically after successful export.

An interruption after the final folder appears but before its session receipt is
saved can also be recovered by rerunning the same command. The session only marks
files exported once the complete selected bundle has passed verification.

Versions before this fix deleted the temporary copy when export failed; those
old failed copies cannot be resumed. Original collector data is retained.

The source collector can keep scraping: export briefly pins each saved checkpoint
without holding the collector lock for the copy. Later saved data is picked up
on the next call with the same session. Export excludes credentials, reporting
queues, HTTP/browser caches, retired generations and derived dataset snapshots.

On MSI, copy the bundle to a temporary folder **outside** `ensemble_data/`. Once
the copy finishes, check it and move the complete folder into `ensemble_data/`:

```sh
python -m tools.prepare_transfer --verify "C:/transfer/main-data-01"
```

For example, move `C:/transfer/main-data-01` to
`<MSI checkout>/ensemble_data/main-data-01`. MSI's normal worker supervisor notices
it automatically. **Do not stop the scraper to import.** Headless and Main may
keep running too. Background imports require the portable manifest; an ordinary
folder without one is left alone, so a half-copied folder cannot be consumed.
The explicit `--import-only` command remains available for legacy offline imports.

Imports work in resumable batches sized to MSI's available staging space, keeping
1 GiB free for new scraper checkpoints. If space is insufficient, background work
waits with the source retained. It does not ask the scraper to wait. Importing a
season yields when the foreground collector requests that same season; other
season work continues independently. Confirmed parts have durable receipts and
are not merged again after a restart.

The receiving computer keeps its settings and worker identity. Original
collection credit is retained. Exact incoming checkpoint bytes are stored under
short hashed archive paths, with their original paths recorded in the import
journal under `data/_system/imports/`. Successful imports remove only their staged
bundle; keep Main's originals and the card until you have checked MSI's result.
Corrupt or incomplete bundles remain staged for investigation.

Background inventory reports changed holdings and saved season schedules to Main.
Main alone computes remaining work and sends worker instructions; MSI is the
storage hub, not a second coordinator. Dataset builds use pinned
inputs and publish independently of scraping. Completed network seasons and the
final bundle in a portable session queue dataset publication for Main. Earlier
portable chunks may contain only part of a season. Raw files stay on MSI; Main
receives prepared datasets and concordance.
