"""Move flat data/<competition>-YYYY-YY folders to data/<competition>/YYYY-YYYY."""
from __future__ import annotations

import argparse
from pathlib import Path

from filelock import FileLock

from collection.paths import collection_directory, collection_parts


def migration_plan(data_root: Path | str) -> list[tuple[Path, Path]]:
    root = Path(data_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")
    plan = []
    for source in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            collection_parts(source.name)
        except ValueError:
            continue
        destination = collection_directory(root, source.name)
        if destination.exists():
            raise FileExistsError(
                f"Refusing to merge {source} into existing destination {destination}"
            )
        plan.append((source, destination))
    return plan


def migrate(data_root: Path | str, *, dry_run: bool = False) -> list[tuple[Path, Path]]:
    root = Path(data_root).resolve()
    if dry_run:
        return migration_plan(root)
    with FileLock(str(root / '.collector.lock'), timeout=0):
        plan = migration_plan(root)
        for source, destination in plan:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
        return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=Path(__file__).parent / "data"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    plan = migrate(args.data_root, dry_run=args.dry_run)
    action = "Would move" if args.dry_run else "Moved"
    for source, destination in plan:
        print(f"{action} {source} -> {destination}")
    print(f"{action} {len(plan)} collection director{'y' if len(plan) == 1 else 'ies'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
