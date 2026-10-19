#!/usr/bin/env python3
"""Copy saved raw event JSONs to an SD card using only Python's standard library.

Preview: python3 copy_raw_events.py --dry-run
Copy:    python3 copy_raw_events.py /Volumes/YOUR_SD_CARD

Outputs go into SD_CARD/raw-events/, preserving paths relative to data/.
Original JSON bytes (including the provider envelope) are preserved.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

DEFAULT_SOURCE = Path('/Users/Alexander/PycharmProjects/soccerAnalysis/data')
EVENT_TYPES = {
    'whoscored': {'match'},
    'fotmob': {'match'},
    'sofascore': {'rating_breakdown', 'shotmap'},
}
MATCH_TYPES = {
    **EVENT_TYPES,
    'sofascore': EVENT_TYPES['sofascore'] | {'lineups', 'average_positions', 'heatmap'},
}


def response_metadata(path):
    # The collector writes provider/key before the potentially large payload.
    with path.open(encoding='utf-8') as handle:
        header = handle.read(4096)
        values = {}
        for name in ('provider', 'key'):
            match = re.search(r'"' + name + r'"\s*:\s*("(?:[^"\\]|\\.)*")', header)
            if match:
                values[name] = json.loads(match[1])
        if len(values) == 2:
            return values
        handle.seek(0)
        result = json.load(handle)
        return result if isinstance(result, dict) else {}


def discover(source, all_match_json=False):
    allowed = MATCH_TYPES if all_match_json else EVENT_TYPES
    selected = []
    # Deliberately excludes http_cache/, checkpoints, schedules, and exports.
    for path in sorted(source.glob('*/*/raw/*/*.json')):
        relative = path.relative_to(source)
        if path.is_symlink() or not path.is_file() or path.resolve() != source / relative:
            raise ValueError(f'Refusing linked source: {path}')
        provider = path.parent.name
        if provider not in allowed:
            continue
        metadata = response_metadata(path)
        key = metadata.get('key', '')
        if (metadata.get('provider') == provider and isinstance(key, str)
                and key.split('/')[0] in allowed[provider]):
            selected.append((path, relative, path.stat().st_size))
    return selected


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.digest()


def check_target(root, relative):
    target = root / relative
    current = root
    if current.is_symlink():
        raise ValueError(f'Refusing linked destination: {current}')
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Refusing linked destination: {current}')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('sd_card', nargs='?', type=Path, help='Existing SD card mount folder')
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE, help='Source data folder')
    parser.add_argument('--dry-run', action='store_true', help='Show selection and size; write nothing')
    parser.add_argument('--all-match-json', action='store_true', help='Also include lineups, average positions and heatmaps')
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    if not source.is_dir():
        parser.error(f'Source is not a folder: {source}')
    root = None
    if args.sd_card:
        card = args.sd_card.expanduser().resolve(strict=True)
        if not card.is_dir():
            parser.error('SD card must be an existing folder')
        root = card / 'raw-events'
        if root == source or source in root.parents or root in source.parents:
            parser.error('Source and destination must be separate folders')
    elif not args.dry_run:
        parser.error('Provide the SD card mount folder, or use --dry-run')
    print(f'Scanning {source}', flush=True)
    files = discover(source, args.all_match_json)
    if not files:
        raise ValueError('No matching raw event JSONs found; nothing copied')
    total = sum(size for _, _, size in files)
    counts = Counter(relative.parts[-2] for _, relative, _ in files)
    print(f'Selected {len(files):,} JSON files, {total / 1024**3:.2f} GiB')
    for provider, count in sorted(counts.items()):
        print(f'  {provider}: {count:,}')
    if root:
        print(f'Destination: {root}')
    if args.dry_run:
        print('Preview complete. No files written.')
        return
    pending = []
    skipped = 0
    # Detect conflicts before copying anything. Re-running safely resumes a copy.
    for path, relative, size in files:
        target = check_target(root, relative)
        if target.exists():
            if not target.is_file() or target.stat().st_size != size or digest(path) != digest(target):
                raise ValueError(f'Destination differs; refusing to overwrite: {target}')
            skipped += 1
        else:
            pending.append((path, relative, size))
    needed = sum(size for _, _, size in pending)
    free = shutil.disk_usage(card).free
    if needed + 64 * 1024**2 > free:
        raise ValueError(f'Not enough free space: need {needed / 1024**3:.2f} GiB plus 64 MiB')
    for index, (path, relative, size) in enumerate(pending, 1):
        target = check_target(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.copy-', dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, 'wb') as output, path.open('rb') as original:
                shutil.copyfileobj(original, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size != size or digest(path) != digest(temporary):
                raise ValueError(f'Copy verification failed: {path}')
            if target.exists() or target.is_symlink():
                raise ValueError(f'Destination appeared during copy: {target}')
            temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
        if index % 100 == 0 or index == len(pending):
            print(f'Copied and verified {index:,}/{len(pending):,}', flush=True)
    print(f'Done: {len(pending):,} copied and verified; {skipped:,} identical files skipped.')
    print('Eject the SD card normally before removing it.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f'Error: {error}')
