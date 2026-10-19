# Local worker inventories

`<worker-id>.json` describes that computer’s validated local holdings and the
original collectors. These files are ignored by Git. Run `python main.py
--inventory --worker-id main` to rebuild an inventory offline, then start the
normal worker supervisor to send it to Main.

Main receives durable reports over the private reporting service. Workers also
receive other computers’ evidence under `ensemble_data/reporting/workers` for
ensemble completion checks. Remote evidence never creates local raw data or
checkpoints. Preserve `data/identity.json` and the original worker data folder.
See [setup, logs and upgrades](../../docs/WORKERS.md).
