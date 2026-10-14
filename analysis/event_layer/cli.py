"""Command-line entry point for the isolated event layer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run_pipeline


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("input", type=Path, help="WhoScored event CSV exported by the collector")
    value.add_argument("output", type=Path, help="Separate directory for derived tables")
    value.add_argument("--format", choices=("auto", "csv", "parquet"), default="auto")
    value.add_argument("--limit-matches", type=int, help="Process only the first N matches for a trial")
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    manifest = run_pipeline(args.input, args.output, file_format=args.format, limit_matches=args.limit_matches)
    print(json.dumps(manifest["row_counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
