"""Run the collection workflow selected in config/settings.yaml."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

import config

from collection.cli import main as collect
from collection.paths import collection_directory
from collection.selection import competition_by_name, season_key


# Values loaded from config/settings.yaml; edit the YAML file, not this module.
ACTION = config.ACTION
COMPETITIONS = config.COMPETITIONS
SEASONS = config.SEASONS
SOFASCORE_SPATIAL_CAPTURE = config.SOFASCORE_SPATIAL_CAPTURE
BUILD_DATASET = config.BUILD_DATASET


def _collection_name(competition, season):
    competition = competition_by_name(competition).name
    season = season_key(season)
    start_year, end_year = season.split('/')
    slug = re.sub(r'[^a-z0-9]+', '-', competition.lower()).strip('-')
    return f'{slug}-{start_year}-{end_year[-2:]}'


def _selection(args):
    """Read selection flags without consuming arguments owned by collect.py."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--competition', default=COMPETITIONS[0])
    parser.add_argument('--season', default=SEASONS[0])
    parser.add_argument('--name')
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    selected, _ = parser.parse_known_args(args)
    selected.name = selected.name or _collection_name(selected.competition, selected.season)
    return selected


def _capture_args(args):
    if SOFASCORE_SPATIAL_CAPTURE not in ('fast','all'):
        raise ValueError("SOFASCORE_SPATIAL_CAPTURE must be 'fast' or 'all'")
    if ('--sofascore-spatial-capture' not in args and
            not any(arg.startswith('--sofascore-spatial-capture=') for arg in args) and
            '--no-spatial' not in args):
        args.extend(['--sofascore-spatial-capture',SOFASCORE_SPATIAL_CAPTURE])
    args.append('--no-build-csvs')  # Dataset building owns the export step.


def _run_one(command,competition,season,name,forwarded):
    print(f'\n=== {command.upper()}: {name} ===',flush=True)
    args=[command,'--name',name,*forwarded]
    if command=='start':
        args.extend(['--competition',competition,'--season',season])
    if command in ('start','resume'):_capture_args(args)
    result=collect(args)
    print(f'=== FINISHED: {name} (exit {result}) ===',flush=True)
    return result


def _collection_complete(directory):
    """Use checkpoint status, not the post-export summary, as the authority."""
    try:
        events=json.loads((directory/'schedule.json').read_text())['events']
        expected={str(event['id']) for event in events}
        with (directory/'collection.csv').open(newline='') as handle:
            statuses={row['match_id']:row['status'] for row in csv.DictReader(handle)}
        return bool(expected) and set(statuses)==expected and all(
            statuses[match_id]=='complete' for match_id in expected)
    except (FileNotFoundError,KeyError,TypeError,ValueError,json.JSONDecodeError,csv.Error):
        return False


def _single(args):
    commands={'start','resume','export','status','probe'}
    selection = _selection(args)
    explicit_command = bool(args and args[0] in commands)

    if not explicit_command:
        settings = collection_directory(selection.data_root.resolve(), selection.name) / 'job.json'
        if ACTION not in commands | {'auto'}:
            raise ValueError(f'Unknown ACTION {ACTION!r}')
        command = ('resume' if settings.exists() else 'start') if ACTION == 'auto' else ACTION
        args.insert(0, command)

    if '--name' not in args and not any(arg.startswith('--name=') for arg in args):
        args[1:1] = ['--name', selection.name]

    if args[0] == 'start':
        if '--competition' not in args and not any(arg.startswith('--competition=') for arg in args):
            args.extend(['--competition', selection.competition])
        if '--season' not in args and not any(arg.startswith('--season=') for arg in args):
            args.extend(['--season', selection.season])

    if args[0] in ('start','resume'):
        _capture_args(args)

    # Paths are anchored to this clone, regardless of the IDE working directory.
    return collect(args)


def _batch(forwarded):
    if ACTION not in {'auto','start','resume','export','status','probe'}:
        raise ValueError(f'Unknown ACTION {ACTION!r}')
    if ACTION=='probe':return collect(['probe',*forwarded])
    root=_selection(forwarded).data_root.resolve()
    targets=[]
    for competition in COMPETITIONS:
        canonical_competition=competition_by_name(competition).name
        for season in SEASONS:
            canonical_season=season_key(season)
            name=_collection_name(canonical_competition,canonical_season)
            directory=collection_directory(root,name)
            targets.append((canonical_competition,canonical_season,name,directory,
                            (directory/'job.json').exists()))
    existing=[target for target in targets if target[4]]
    new=[target for target in targets if not target[4]]
    results=[]
    def run(command,competition,season,name):
        try:
            code=_run_one(command,competition,season,name,forwarded)
        except SystemExit as exc:
            code=exc.code if isinstance(exc.code,int) else 2
            print(f'=== PAUSED: {name} (exit {code}); continuing batch ===',flush=True)
        results.append(code)
        return code
    if ACTION in ('auto','resume'):
        for competition,season,name,directory,_ in existing:
            if _collection_complete(directory):
                print(f'Skipping complete collection: {name}')
                continue
            if run('resume',competition,season,name)==130:return 130
    if ACTION in ('auto','start'):
        for competition,season,name,_,_ in new:
            if run('start',competition,season,name)==130:return 130
    if ACTION in ('export','status'):
        for competition,season,name,_,_ in existing:
            if run(ACTION,competition,season,name)==130:return 130
    return max(results,default=0)


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    commands={'start','resume','export','status','probe'}
    parser = argparse.ArgumentParser(description=__doc__, epilog='Select a season with --competition/--season/--name. Use python -m collection --help for collection options.')
    parser.add_argument('--build-dataset', action=argparse.BooleanOptionalAction, default=BUILD_DATASET)
    parser.add_argument('--dataset-root', type=Path, default=config.DATASET_ROOT)
    build_settings, args = parser.parse_known_args(args)
    explicit_command=bool(args and args[0] in commands)
    explicit_selection=any(arg in ('--competition','--season','--name') or
                           arg.startswith(('--competition=','--season=','--name=')) for arg in args)
    # A main.py export is always a full dataset build, rather than a partial CSV export.
    action = args[0] if explicit_command else ACTION
    build_requested = build_settings.build_dataset or action == 'export'
    if any(arg in ('--build-csvs', '--no-build-csvs') for arg in args):
        parser.error('Use --build-dataset or --no-build-dataset instead of CSV flags')
    result = 0 if action == 'export' else (_single(args) if explicit_command or explicit_selection else _batch(args))
    if build_requested and action in ('auto', 'start', 'resume', 'export') and result != 130:
        from dataset_builder import build_dataset
        selection = _selection(args)
        # Batch builds include completed seasons skipped during collection.
        names = ([selection.name] if explicit_command or explicit_selection else
                 [_collection_name(c, s) for c in COMPETITIONS for s in SEASONS
                  if (collection_directory(selection.data_root, _collection_name(c, s)) / 'job.json').is_file()])
        if not names and action == 'export':
            parser.error('No saved seasons match COMPETITIONS and SEASONS')
        if names:
            try:
                build_result = build_dataset(selection.data_root, build_settings.dataset_root, names)
            except (OSError, ValueError, KeyError) as exc:
                print(f'Dataset build failed: {exc}', flush=True)
                build_result = 2
            result = max(result, build_result)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
