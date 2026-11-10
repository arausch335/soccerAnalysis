"""Package saved WhoScored match JSON for the R report."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from providers.cache import ResponseCache
from storage.pandasStore import atomic_json, atomic_replace
from .selection import competition_by_name, season_label
from .whoScoredData import LEGACY_SUBTREES


def package_raw_events(data_root, output, *, competitions=(), seasons=()):
    from review_data import who_valid

    root = Path(data_root).expanduser().resolve(strict=True)
    output = Path(output).expanduser().absolute()
    destination = output.resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Choose an output directory separate from the scraping cache.')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'{output} already exists. Choose a new --output directory.')
    output.parent.mkdir(parents=True, exist_ok=True)

    files = []
    counts = Counter()
    exported = set()
    invalid = 0
    with TemporaryDirectory(prefix='.raw-events-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'bundle'
        # Prefer current season files, using the older layouts only for missing matches.
        for source_root in (root, *(root / name for name in LEGACY_SUBTREES)):
            paths = sorted(source_root.glob('*/*/raw/whoscored/*.json'))
            if source_root != root:
                paths.extend(sorted(source_root.glob('*/*/raw/*.json')))
            for source in paths:
                competition, season, *_ = source.relative_to(source_root).parts
                if not re.fullmatch(r'\d{4}-\d{4}', season):
                    continue
                if (competitions and competition not in competitions) or (seasons and season not in seasons):
                    continue
                if not source.resolve().is_relative_to(root):
                    raise ValueError(f'Raw event file is outside the scraping cache: {source}')
                raw = source.read_bytes()
                try:
                    response = json.loads(raw)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f'Invalid JSON in {source}') from exc
                if not isinstance(response, dict) or response.get('provider') != 'whoscored':
                    continue
                key = response.get('key')
                native_id = re.fullmatch(r'match/([1-9]\d*)', key) if isinstance(key, str) else None
                if not native_id:
                    continue
                match = (competition, season, key)
                if match in exported:
                    continue
                payload = response.get('payload')
                centre = payload.get('matchCentreData') if isinstance(payload, dict) else None
                home = centre.get('home') if isinstance(centre, dict) else None
                if (not isinstance(home, dict) or not home.get('teamId')
                        or not who_valid(payload, int(native_id[1]))
                        or str(centre.get('matchId', native_id[1])) != native_id[1]):
                    invalid += 1
                    continue
                target = ResponseCache(stage / competition / season / 'raw').path('whoscored', key)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                files.append({'path': target.relative_to(stage).as_posix(),
                              'size': len(raw), 'sha256': sha256(raw).hexdigest()})
                counts[competition, season] += 1
                exported.add(match)

        if not files:
            raise ValueError('No saved WhoScored match event files matched this selection.')
        manifest = {
            'format': 'soccer-raw-events-dataset-v1',
            'provider': 'whoscored',
            'seasons': [{'competition': competition, 'season': season, 'matches': count}
                        for (competition, season), count in sorted(counts.items())],
            'skipped_invalid_responses': invalid,
            'files': files,
        }
        atomic_json(stage / 'raw-events-manifest.json', manifest, indent=2)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f'{output} already exists. Choose a new --output directory.')
        atomic_replace(stage, output)
    return manifest


def main(argv=None):
    import config

    parser = argparse.ArgumentParser(prog='main.py --package-raw-events', description=__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--output', type=Path,
                        default=config.DATASET_ROOT.with_name('raw_events_dataset'))
    parser.add_argument('--competition', action='append', default=[],
                        help='Competition to include. Repeat to select several.')
    parser.add_argument('--season', action='append', default=[], type=season_label,
                        help='Season to include. Repeat to select several.')
    args = parser.parse_args(argv)
    try:
        competitions = {re.sub(r'[^a-z0-9]+', '-', competition_by_name(name).name.lower()).strip('-')
                        for name in args.competition}
        manifest = package_raw_events(args.data_root, args.output, competitions=competitions,
                                      seasons=set(args.season))
    except (OSError, ValueError) as exc:
        parser.exit(2, f'Raw events package failed: {exc}\n')
    for row in manifest['seasons']:
        print(f"{row['competition']} {row['season']}: {row['matches']:,} matches")
    size = sum(file['size'] for file in manifest['files'])
    print(f"Wrote {len(manifest['files']):,} WhoScored matches ({size / 1024**3:.2f} GiB) to "
          f"{args.output.expanduser().resolve()}")
    if manifest['skipped_invalid_responses']:
        print(f"Skipped {manifest['skipped_invalid_responses']:,} incomplete or invalid match responses.")
    return 0
