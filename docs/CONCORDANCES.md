# Confirm MSI integration, then rebuild the identity tables

The raw archive being received is different from its contents being incorporated.
Main's last old MSI report showed received packages awaiting incorporation and a
portable import still being verified. A current audit must run on MSI; Main's
compiled files or old worker reports cannot prove that MSI has all scraped data.

## Recover the importer first

An older importer incorrectly treated the preferred 128 MiB batch size as a
maximum file size. A larger CSV, or shared CSV overhead, could produce a false
"waiting for disk space" message despite ample capacity. The importer now admits
one oversized file when the actual staging reserve fits, and flat CSVs are no
longer recopied as common metadata for every checkpoint. A genuine shortage
reports the checked destination path and required/available bytes. MSI needs
space on the volume containing its configured `data` directory, not just another
drive. Original files and durable receipts are retained.

Update MSI's checkout and restart its worker so it runs the fixed importer:

```sh
python main.py --terminate
git pull --ff-only origin main
python main.py --worker-id msi
```

Keep Tailscale, Main reporting, Headless and MSI's receiver running while queues
drain. Wait for producer queued uploads to reach zero, MSI received-but-not-
incorporated packages to reach zero, and portable imports to finish. Do not
delete the queues, receipts, raw files, or staged portable folders to force this.
Once drained, stop new scraping and allow a fresh inventory report from each
worker. Freeze collection/import writes for the audit/rebuild. If inputs change,
the rebuild refuses to replace the last bundle.

## Export expected holdings on Main

From Main's repository directory:

```sh
python -m tools.audit_integration --export-expected expected-msi.json
```

Copy `expected-msi.json` to MSI's repository folder. It contains reported resource
holdings, timestamps and queue counts; no credentials or raw response bodies.
The command uses authenticated reports already stored on Main. Reports older
than 24 hours, empty expectations and outstanding producer uploads cannot pass
the MSI proof. Reconnect a stale worker and export again rather than treating its
old report as current. An export only covers the workers that have reported:
check the displayed/recorded worker list includes every computer holding data.

MSI also needs the latest compiled fixture baseline because Headless sends that
directly to Main. If absent/stale on MSI, copy these compiled inputs from Main
into the corresponding paths under MSI's `dataset` directory:

- `fixtures.csv`
- `fixture_concordance/team_ids.csv`
- `fixture_concordance/attempts.csv`

This supplies the full 2010+ schedule and reviewed team/replacement references.
Raw scraped data continues to go to MSI; the rebuild makes no provider requests.

## Prove integration on MSI

```sh
python -m tools.audit_integration --expected-inventory expected-msi.json --output integration-audit.json
```

Paths default to `config/settings.yaml`; use `--data-root` and `--ensemble-root`
when the archive or portable-import directory is elsewhere. The audit:

1. Reads MSI's archive ledger without changing it. Both incomplete uploads and
   received-but-not-incorporated packages block readiness.
2. Detects staged portable imports, including unfinished copies. Uploads set
   aside for review also block full integration; inspect them with
   `python -m tools.review_imports` on MSI.
3. Revalidates actual local holdings without trusting inventory-cache hits.
4. Compares reported resources/capabilities with validated MSI holdings. Empty
   queues alone cannot establish completeness.

Success is exit code 0 and `state: ready`. Exit code 2 means read `problems` and
`missing` in the report. This proves integration of reported scraped holdings,
not that every scheduled match has been scraped. Partial scraped data remains
partial; reviewed identity/resource conflicts are not approved by this audit.

## Rebuild and publish from MSI

```sh
python -m tools.rebuild_concordances --integration-report integration-audit.json --publish
```

The command rebuilds from active raw-data checkpoints, saved player-stat exports,
saved identity evidence and provider-only archives. Retired checkpoint generations
are ignored. A scheduled game with no saved player-stat evidence is not added
to the scraped match table. Collected players are reindexed from the data, rather
than copying an older player list. Fixture-only/provider bridges enrich IDs but
do not establish scraped-stat coverage.

MSI publishes compiled CSVs through its existing dataset channel. Main receives
them through normal dataset polling. Raw payloads are not published. Rebuilds
read only stored evidence and do not start a scrape. Publication requires a
ready bundle and MSI's own identity. `--validate-only` checks an existing bundle.

For an explicitly incomplete local test, `--allow-unconfirmed --output <folder>`
creates a preview with `ready_for_R: false` and a separate preview registry. It
does not change the production ID registry. The R loader rejects previews by
default. Do not use this switch to bypass the MSI integration step for production.

## File layout

```text
dataset/concordance/
  full/
    fixtures.csv
    entities.csv
    teams.csv
  scraped/
    matches.csv
    teams.csv
    players.csv
  bundle.json
  alex_registry.json
  id_redirects.csv
  quarantine.csv
```

`full/fixtures.csv` is the 2010+ fixture table with shared match/home/away entity
keys. It also recovers observed Sofascore fixtures missing from an older global
publication when a saved season fixture/schedule establishes them. Proven
superseded attempts are not reintroduced. `full/teams.csv` contains teams used
in that fixture history. Generic reviewed club profiles are canonicalized;
provider-prefixed fixture fields retain original native IDs.
Every observed native team profile is retained in `provider_aliases` JSON arrays,
including historical aliases such as Viitorul's Sofascore ID alongside the
reviewed canonical Farul profile. Validation checks that every provider-prefixed
fixture team ID is represented in this reference or its alias arrays.

`full/entities.csv` contains directed historical team relationships, with IDs
for both endpoints, dates/precision, relationship types and provenance. A merger,
successor or `distinct_from` edge is not an instruction to merge team identities.
Researched endpoint peers do not become automatic provider identity bridges;
endpoint `safe_for_cross_provider_joins` flags remain false.
Relationships whose endpoints lack every provider ID go to `quarantine.csv`;
the original history catalog remains intact. No provider ID is invented to make
such a relationship pass the R import requirement.

`scraped/matches.csv` contains only matches with saved player-stat evidence.
`scraped/teams.csv` contains teams observed in those matches. `scraped/players.csv`
has one row per player identity, with competitions/seasons/clubs retained in JSON
context columns. These contexts do not multiply player-level joins.

Legacy reference files remain available for older consumers, but the six tables
above are the new import interface. `bundle.json` is separate from the legacy
concordance manifest, so older reference refreshes cannot erase its metadata.
Checksums and key validation reject partial or mixed publications.
When subsequent season exports or a new full fixture delivery arrive, an older
bundle is marked not ready for R. Re-audit/rebuild after incorporation so newer
statistics cannot silently reuse stale identity references.

## Shared IDs

Each ordinary row has:

- `entity_id`: the required shared key, published Reep ID or Alex fallback.
- `reep_id`: a published Reep ID only; it stays blank when unknown.
- `sofascore_id`, `fotmob_id`, `whoscored_id`: available native provider IDs.
- `alex_id`, `id_source`: the stable project alias and provenance.
- `identity_status`, `safe_for_cross_provider_joins`: unresolved conflicts remain
  visible even after assigning a project key.

Fallback prefixes are `am` for matches, `at` for teams and `ap` for players,
followed by a deterministic 24-character hexadecimal token. Names and row order
never determine identity. The registry persists provider aliases; adding another
provider does not mint a different ID. A later published Reep bridge becomes the
preferred `entity_id`, while `id_redirects.csv` records the old Alex alias.
Keep/back up `data/_system/identity/alex_ids.json`. The compiled registry copy
can restore it if absent. Published Reep redirects are retained too.

Every exported canonical record must have an `entity_id` and at least one real
provider ID. Historical relationships apply this requirement to both endpoints.
An Alex key identifies a project record; it does not validate a disputed peer ID.
Missing provider mappings stay blank, and contradictory profiles stay flagged.

## R

```r
source("analysis/read_concordances.R")
ids <- read_concordances("dataset")
fixtures <- ids$full$fixtures
players <- ids$scraped$players
```

The reader requires `readr`, `jsonlite` and `digest`. It checks the integration
flag, six file checksums, unique shared keys and required provider anchors.
IDs remain character. Fixture dates/kickoffs and numeric score fields are typed.
For cross-provider joins, filter `safe_for_cross_provider_joins`, exclude missing
join IDs, and use an explicit many-to-one relationship. Use the directed team
history layer with relationship/date rules rather than treating ancestry as
identity equivalence.
