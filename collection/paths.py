"""Canonical paths for competition-season collection directories."""
from __future__ import annotations

from pathlib import Path
import re


COLLECTION_NAME = re.compile(
    r"^(?P<competition>.+)-(?P<start>\d{4})-(?P<end>\d{2}|\d{4})$"
)


def collection_parts(name: str) -> tuple[str, str]:
    """Return ``(competition_slug, YYYY-YYYY)`` for a CLI collection name."""
    found = COLLECTION_NAME.fullmatch(str(name))
    if not found:
        raise ValueError(
            "collection name must end in a season such as "
            "premier-league-2024-25 or premier-league-2024-2025"
        )
    competition = found.group("competition").strip("-")
    start = int(found.group("start"))
    raw_end = found.group("end")
    end = int(raw_end)
    if len(raw_end) == 2:
        end += start // 100 * 100
        if end < start:
            end += 100
    if not competition or end != start + 1:
        raise ValueError(f"invalid competition-season collection name: {name!r}")
    return competition, f"{start:04d}-{end:04d}"


def collection_directory(data_root: Path | str, name: str) -> Path:
    competition, season = collection_parts(name)
    return Path(data_root) / competition / season


def collection_name(directory: Path | str) -> str:
    """Return the short CLI name for a canonical competition/season path."""
    directory = Path(directory)
    competition, season = directory.parent.name, directory.name
    found = re.fullmatch(r"(\d{4})-(\d{4})", season)
    if not competition or not found or int(found.group(2)) != int(found.group(1)) + 1:
        raise ValueError(f"not a competition/season directory: {directory}")
    return f"{competition}-{found.group(1)}-{found.group(2)[-2:]}"


def discover_collections(data_root: Path | str, marker: str) -> list[Path]:
    """Find canonical collection folders containing *marker*."""
    return sorted(path.parent for path in Path(data_root).glob(f"*/*/{marker}"))
