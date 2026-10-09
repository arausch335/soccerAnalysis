#!/usr/bin/env python3
"""Copy a local Python player-scraper project into this transferable package."""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

EXCLUDED_DIRECTORIES = {
    ".git", ".idea", ".vscode", ".venv", "venv", "env", "__pycache__",
    "node_modules", "site-packages", "build", "dist", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "data", "datasets", "outputs", "output",
    "results", "logs", "secrets", "credentials",
}
SOURCE_EXTENSIONS = {".py", ".pyi", ".toml", ".ini", ".cfg", ".md", ".rst", ".sh", ".bat", ".ps1"}
SPECIAL_FILES = {"requirements.in", "pipfile", "pipfile.lock", "poetry.lock", "uv.lock", "environment.yml", "environment.yaml", "makefile", "dockerfile", ".python-version"}


def included_file(path):
    name = path.name.lower()
    if name.startswith(".env") or name.startswith(("credentials", "secrets")):
        return False
    return (path.suffix.lower() in SOURCE_EXTENSIONS or name in SPECIAL_FILES or
            (name.startswith("requirements") and name.endswith(".txt")))


def source_files(source):
    for folder, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories
                                if d.lower() not in EXCLUDED_DIRECTORIES
                                and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if not path.is_symlink() and included_file(path):
                yield path


def include_project(source, project):
    source, project = Path(source).resolve(), Path(project).resolve()
    if not source.is_dir():
        raise ValueError(f"Source folder does not exist: {source}")
    if source == project or source in project.parents or project in source.parents:
        raise ValueError("Use a separate original player-project folder, outside this package")
    target = project / "player_match_information"
    if target.exists():
        raise ValueError(f"{target} already exists; existing files will not be overwritten")
    files = list(source_files(source))
    if not any(path.suffix.lower() == ".py" for path in files):
        raise ValueError("No Python source files found in the selected folder")
    # Build the inclusion directory separately so a failed copy is not presented as complete.
    staging = project / ".player_project_staging"
    if staging.exists():
        raise ValueError(f"Remove the previous incomplete staging folder first: {staging}")
    staging.mkdir(parents=True)
    manifest = []
    try:
        for path in files:
            relative = path.relative_to(source)
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            manifest.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(output.read_bytes()).hexdigest()))
        (staging / "TRANSFER_MANIFEST.json").write_text(
            json.dumps(dict(files=manifest, note="Copied source and dependency manifests; original project unchanged."), indent=2) + "\n",
            encoding="utf-8")
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return target, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Original SoccerAnalysis project folder")
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Transfer package root (defaults to the folder containing this helper)")
    args = parser.parse_args(argv)
    try:
        target, manifest = include_project(args.source, args.package)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(f"Copied {len(manifest)} source/dependency files into {target}")
    print("Review the copied files, then follow docs/PRIVATE_GITHUB.md to transfer both scrapers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
