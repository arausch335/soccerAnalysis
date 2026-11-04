# Full fixture history on MSI

The compiled catalogue is independent of raw scraped coverage. Receiving a ZIP
does not install that catalogue. Use a fixture-only transfer, whose manifest
contains every public table, checksum, size and fixture count.

On Main (or another computer holding the full validated catalogue):

```sh
python -m tools.fixture_transfer --dataset-root dataset --export /path/to/new-full-fixture-history
```

Copy the completed folder outside MSI's `ensemble_data` first. Verify it there:

```sh
python -m tools.fixture_transfer --verify C:/transfer/new-full-fixture-history
```

Move the verified folder under MSI's `ensemble_data/`, then run MSI normally:

```sh
python main.py --worker-id msi
```

The supervisor installs a durable private baseline, retains the source transfer,
and queues builds with complete saved inputs, including previously blocked jobs.
The next build merges the baseline with MSI's existing fixtures using provider
match keys and the receiving identity authority. Local fixtures and reviewed
identity decisions are retained. Repeated ingestion is idempotent. The baseline
and merge receipts survive temporary builds and dataset delivery.

For manual packaging without the supervisor, install the baseline explicitly:

```sh
python -m tools.fixture_transfer --import C:/transfer/new-full-fixture-history
python main.py --package --parquet
```

Use `--dataset-root PATH` on both commands if MSI uses a non-default dataset
folder. Never install into the raw `data` directory. An incomplete or damaged
bundle is rejected; receiving files alone never marks a partial transfer ready.

Internal linker inputs remain CSV. Public concordance tables follow the selected
format (Parquet by default). An old CSV publication is read in its declared format
while producing the new Parquet bundle. Temporary MSI builds restore and validate
the last acknowledged public publication from immutable archive objects, before
indexing raw inputs, so a missing live public file cannot derail final assembly.

## Reporting and Main imports

Identity review is persisted with the report transaction and processed separately
from the HTTP acknowledgement. Lost acknowledgements replay without duplicate
logs or repeated completed review. Pending identity work survives a Main restart.
Restart Main after updating to load this receiver change; workers retain and retry
their existing reporting queues. Do not delete those queues.

Main's raw archive is not synchronized from MSI. Main receives only MSI's published
datasets. Equal publication/receipt hashes mean Main has installed the latest
publication even while MSI has failed or pending builds. A newer publication is
needed before another dataset import can happen.

Neither a successful fixture transfer nor successful packaging proves that all
Main raw data is preserved on MSI. Complete the separate integration audit and
partial-file check before deleting any originals.
