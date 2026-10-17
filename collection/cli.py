"""Portable CLI. New collections use dedicated, marked data directories."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path

import config

from filelock import FileLock, Timeout
from providers.httpClient import ProviderError
from providers.safeClient import SafeSofaScoreClient, SafeFotMobClient, SafeWhoScoredClient
from providers.sofaScoreTransport import create_sofascore_client
from providers.sofaScoreProbe import DEFAULT_MATCH_ID, probe_match
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner, SPATIAL_ENDPOINTS
from storage.pandasStore import atomic_json


SPATIAL_CAPTURE_MODES = {
    'fast': ('average_positions',),
    'all': SPATIAL_ENDPOINTS,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'resume', 'export', 'status', 'probe'])
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--name', default='premier-league-2024-25')
    parser.add_argument('--competition', default='Premier League')
    parser.add_argument('--season', default='2024/25')
    parser.add_argument('--providers', nargs='+', choices=['sofascore','fotmob','whoscored'],
                        default=None)
    parser.add_argument('--limit', type=int, help='First N finished matches; omit for the season')
    spatial_group = parser.add_mutually_exclusive_group()
    spatial_group.add_argument('--sofascore-spatial-capture', choices=SPATIAL_CAPTURE_MODES)
    spatial_group.add_argument('--no-spatial', action='store_true')
    parser.add_argument('--build-csvs', action=argparse.BooleanOptionalAction, default=True,
                        help='Build combined CSV exports after collection (default: enabled)')
    parser.add_argument('--compile-events', action=argparse.BooleanOptionalAction, default=True,
                        help='Compile the WhoScored event stream CSV during export (default: enabled)')
    parser.add_argument('--sofascore-transport',choices=('http','browser','auto'),default=config.TRANSPORT['sofascore_mode'])
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--match-id',type=int,default=DEFAULT_MATCH_ID,
                        help='Sofascore match ID for the single-request probe')
    args = parser.parse_args(argv)
    if args.match_id < 1:
        parser.error('match ID must be positive')
    if args.command == 'probe' and args.offline:
        parser.error('probe requires a live request; omit --offline')
    if not args.name or Path(args.name).name != args.name or args.name in ('.','..'):
        parser.error('name must be a single directory name')
    try:
        directory = collection_directory(args.data_root.resolve(), args.name)
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit is not None and args.limit < 1:
        parser.error('limit must be positive')
    root = args.data_root.resolve()
    marker = root / '.soccer-scraper.json'
    if not marker.exists():
        if root.exists() and any(root.iterdir()):
            parser.error('Refusing an existing unmarked data directory; choose a NEW data-root')
        if args.command not in ('start','probe'):
            parser.error('No collection here; use start in a new directory')
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(marker, {'format':1})
    # Active collections use data/<competition>/<YYYY-YYYY>. Historical
    # collections belong under outputs/old_data and remain outside this root.
    try:
        with FileLock(str(root / '.collector.lock'), timeout=0), ExitStack() as stack:
            if args.command == 'probe':
                with SafeSofaScoreClient(root, 'sofascore', offline=args.offline) as client:
                    ok,message = probe_match(client,args.match_id)
                    print(message)
                return 0 if ok else 2
            settings_path = directory / 'job.json'
            if args.command == 'start':
                if directory.exists():
                    parser.error('Collection already exists; use resume')
                capture = 'none' if args.no_spatial else (args.sofascore_spatial_capture or 'fast')
                spatial = [] if capture == 'none' else list(SPATIAL_CAPTURE_MODES[capture])
                settings = dict(competition=args.competition, season=args.season,
                                providers=list(dict.fromkeys(['sofascore', *(args.providers or ['sofascore','fotmob','whoscored'])])),
                                sofascore_spatial_capture=capture, spatial=spatial)
                directory.mkdir(parents=True)
                atomic_json(settings_path, settings)
            elif not settings_path.exists():
                parser.error('No saved collection with that name')
            else:
                settings = json.loads(settings_path.read_text(encoding='utf-8'))
                if args.command=='resume' and args.providers is not None:
                    settings['providers']=list(dict.fromkeys(['sofascore',*args.providers]))
                    atomic_json(settings_path,settings)
                if args.command == 'resume' and (args.no_spatial or args.sofascore_spatial_capture):
                    capture = 'none' if args.no_spatial else args.sofascore_spatial_capture
                    spatial = [] if capture == 'none' else list(SPATIAL_CAPTURE_MODES[capture])
                    if (settings.get('sofascore_spatial_capture') != capture or
                            settings.get('spatial') != spatial):
                        settings['sofascore_spatial_capture'] = capture
                        settings['spatial'] = spatial
                        atomic_json(settings_path,settings)
            if args.command == 'status':
                for name in ('job.json','run_summary.json','capabilities.json'):
                    path = directory / name
                    if path.exists(): print(name + '\n' + path.read_text(encoding='utf-8'))
                for path in (root / 'safety').glob('*.json'):
                    print(path.name + '\n' + path.read_text(encoding='utf-8'))
                return 0
            classes = dict(sofascore=create_sofascore_client, fotmob=SafeFotMobClient, whoscored=SafeWhoScoredClient)
            clients = {
                p: stack.enter_context(
                    classes[p](root, p, offline=args.offline or args.command == 'export',
                               **({'mode':args.sofascore_transport} if p=='sofascore' else {}))
                )
                for p in settings['providers']
            }
            runner = SeasonRunner(clients,directory,providers=settings['providers'],spatial=settings['spatial'])
            if args.command == 'export':
                events = json.loads((directory/'schedule.json').read_text(encoding='utf-8'))['events']
                runner._export(events,compile_events=args.compile_events)
                print('Rebuilt available CSVs from checkpoints without network requests.')
                return 0
            try:
                result = runner.run(settings['competition'],settings['season'],limit=args.limit,
                                    build_csvs=args.build_csvs,
                                    compile_events=args.compile_events)
            except KeyboardInterrupt:
                print('Interrupted. Responses already cached are safe; use resume to continue.')
                return 130
            counts=result.status.value_counts().to_dict()
            print(f"Collection result: {len(result)} matches; " +
                  ', '.join(f'{name}={count}' for name,count in sorted(counts.items())))
            return 0 if len(result) and result.status.eq('complete').all() else 2
    except Timeout:
        print('Another collector is using this data root; no requests were made.')
        return 2
    except (ProviderError, ValueError, FileNotFoundError) as exc:
        print(f'Collection paused: {exc}. Keep the data directory and use resume later.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
