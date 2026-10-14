"""Run the standalone event-enrichment pipeline from the project root.

Press Run in PyCharm for a full run.  Add ``--trial`` to the run
configuration's parameters to process one match first.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]

from analysis.event_layer import run_pipeline  # noqa: E402


def find_default_input() -> Path:
    candidates = (
        PROJECT_ROOT / "data" / "premier-league" / "2024-2025" / "events.csv",
    )
    for path in candidates:
        if path.is_file():
            return path
    choices = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        "No WhoScored event export was found. Expected one of:\n"
        f"{choices}\nRun dataset_builder.py once after an export checkpoint is available, then rerun this file."
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", action="store_true", help="Process only the first match")
    parser.add_argument("--input", type=Path, help="Override the WhoScored event CSV")
    parser.add_argument("--output", type=Path, help="Override the derived-output directory")
    parser.add_argument("--format", choices=("auto", "csv", "parquet"), default="csv")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    input_path = (args.input or find_default_input()).resolve()
    output_path = (args.output or (
        PROJECT_ROOT / "outputs" / ("event-layer-trial" if args.trial else "event-layer-full")
    )).resolve()

    print(f"Reading events from: {input_path}")
    print(f"Writing derived data to: {output_path}")
    if args.trial:
        print("Trial mode: processing the first match only.")
    else:
        print("Full mode: processing every match in the current export.")

    manifest = run_pipeline(
        input_path,
        output_path,
        file_format=args.format,
        limit_matches=1 if args.trial else None,
    )
    print("\nComplete:")
    print(json.dumps(manifest["row_counts"], indent=2))
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
