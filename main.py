"""Run shared worker assignments, manual collection commands and the dashboard."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

import config

from collection.cli import main as collect
from collection.paths import collection_directory
from collection.selection import competition_by_name, season_key, season_label
from providers.extensionBridge import extension_scope
from collection.progress import CollectionProgress, progress_scope, prime_job, get_job
from collection.whoScoredEvents import main as collect_whoscored
from collection.fotmobOnly import main as collect_fotmob
from collection.ensemble import ensemble_session


# Values loaded from config/settings.yaml; edit the YAML file, not this module.
COLLECTION_TYPES = config.COLLECTION_TYPES
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



def _normal_requirements(directory,forwarded):
    """A changed type or spatial selection must revisit saved checkpoints."""
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--providers',nargs='+',default=['sofascore','fotmob','whoscored'])
    parser.add_argument('--no-spatial',action='store_true')
    parser.add_argument('--sofascore-spatial-capture',default=SOFASCORE_SPATIAL_CAPTURE)
    requested,_=parser.parse_known_args(forwarded)
    desired=list(dict.fromkeys(['sofascore',*requested.providers]))
    capture='none' if requested.no_spatial else requested.sofascore_spatial_capture
    try:saved=json.loads((directory/'job.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return False,None
    changed=('providers' in saved and set(saved['providers'])!=set(desired) or
             'sofascore_spatial_capture' in saved and saved['sofascore_spatial_capture']!=capture)
    upgrade=desired if 'providers' in saved and set(saved['providers'])!=set(desired) else None
    return changed,upgrade


def _run_one(command,competition,season,name,forwarded):
    print(f'\n=== {command.upper()}: {name} ===',flush=True)
    args=[command,'--name',name,*forwarded]
    if command=='start':
        args.extend(['--competition',competition,'--season',season])
    if command in ('start','resume'):_capture_args(args)
    if command=='resume':
        _,upgrade=_normal_requirements(collection_directory(_selection(forwarded).data_root,name),forwarded)
        if upgrade is not None and not any(a=='--providers' or a.startswith('--providers=') for a in args):
            args.extend(['--providers',*upgrade])
    try:
        result=collect(args)
    except BaseException:
        get_job(collection_directory(_selection(forwarded).data_root,name),'combined',competition,season).end(2)
        raise
    get_job(collection_directory(_selection(forwarded).data_root,name),'combined',competition,season).end(result)
    print(f'=== FINISHED: {name} (exit {result}) ===',flush=True)
    return result


def _collection_complete(directory):
    """Use checkpoint status, not the post-export summary, as the authority."""
    try:
        events=json.loads((directory/'schedule.json').read_text(encoding='utf-8'))['events']
        expected={str(event['id']) for event in events}
        with (directory/'collection.csv').open(newline='',encoding='utf-8') as handle:
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
    if args[0]=='resume':
        _,upgrade=_normal_requirements(collection_directory(selection.data_root,selection.name),args)
        if upgrade is not None and not any(a=='--providers' or a.startswith('--providers=') for a in args):
            args.extend(['--providers',*upgrade])

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
    transport_parser=argparse.ArgumentParser(add_help=False)
    transport_parser.add_argument('--sofascore-transport',default=config.TRANSPORT['sofascore_mode'])
    transport,_=transport_parser.parse_known_args(forwarded)
    browser_requested=transport.sofascore_transport in ('browser','extension')
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
            if not browser_requested and not _normal_requirements(directory,forwarded)[0] and _collection_complete(directory):
                print(f'Skipping complete collection: {name}')
                get_job(directory,'combined',competition,season).end()
                continue
            if run('resume',competition,season,name)==130:return 130
    if ACTION in ('auto','start'):
        for competition,season,name,_,_ in new:
            if run('start',competition,season,name)==130:return 130
    if ACTION in ('export','status'):
        for competition,season,name,_,_ in existing:
            if run(ACTION,competition,season,name)==130:return 130
    return max(results,default=0)


def _normal_main(argv=None):
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


KINDS=('combined','sofascore','whoscored_events','fotmob_players')


def _job_directory(root,kind,competition,season,name=None):
    if kind in ('combined','sofascore'):
        return collection_directory(root,name or _collection_name(competition,season))
    slug=re.sub(r'[^a-z0-9]+','-',competition_by_name(competition).name.lower()).strip('-')
    if kind=='whoscored_events':return Path(root)/slug/season_label(season)
    return Path(root)/'fotmob_player_stats'/slug/season_label(season)


def _standalone_options(kind,args,action):
    parser=argparse.ArgumentParser(description=f'{kind} collection through main.py')
    parser.add_argument('--competition')
    parser.add_argument('--season')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--refresh-schedule',action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument('--build-dataset',action=argparse.BooleanOptionalAction)
    parser.add_argument('--dataset-root')
    parser.add_argument('--match-id',type=int)
    # These apply only to the combined/Sofascore jobs in a mixed run.
    parser.add_argument('--providers',nargs='+')
    parser.add_argument('--sofascore-transport')
    parser.add_argument('--whoscored-transport',choices=('http','auto','browser','extension'))
    parser.add_argument('--sofascore-spatial-capture')
    parser.add_argument('--no-spatial',action='store_true')
    parser.add_argument('--compile-events',action=argparse.BooleanOptionalAction,default=config.BUILD_EVENTS)
    options=parser.parse_args(args[1:] if args and args[0] in ('start','resume','export','status','probe') else args)
    if options.limit is not None and options.limit<1:parser.error('limit must be positive')
    if options.offline and options.refresh_schedule:parser.error('Cannot refresh offline')
    if action=='export' and options.refresh_schedule:parser.error('Cannot refresh during offline export')
    return options


def _standalone(kind,options,targets,action):
    if action=='probe':
        from api_test import main as probe
        provider='whoscored' if kind=='whoscored_events' else 'fotmob'
        forwarded=['--providers',provider,'--data-root',str(options.data_root)]
        if options.match_id is not None:forwarded+=['--match-id',str(options.match_id)]
        if kind=='whoscored_events' and options.whoscored_transport:
            forwarded+=['--whoscored-transport',options.whoscored_transport]
        return probe(forwarded)
    collect_standalone=collect_whoscored if kind=='whoscored_events' else collect_fotmob
    result=0
    for job,directory,competition,season in targets:
        exists=(directory/('whoscored_job.json' if kind=='whoscored_events' else 'job.json')).is_file()
        if action=='status':
            print(f'{kind} {competition} {season_label(season)} | '
                  +(f'{job.total} scheduled; {job.remaining} remaining (cache presence)' if job.total is not None else 'schedule not yet discovered'))
            job.end();continue
        if (action=='start' and exists) or (action in ('resume','export') and not exists):
            print(f'Skipping {kind} {competition} {season_label(season)}: '
                  +('already started; use auto/resume' if exists else 'no saved collection'))
            job.end();continue
        forwarded=['--competition',competition,'--season',season,'--data-root',str(options.data_root)]
        if options.limit is not None:forwarded+=['--limit',str(options.limit)]
        if kind=='whoscored_events' and options.whoscored_transport:
            forwarded+=['--whoscored-transport',options.whoscored_transport]
        if kind=='whoscored_events' and (action=='export' or options.build_dataset or options.build_dataset is None and config.BUILD_DATASET):
            forwarded+=['--export-csvs','--compile-events' if options.compile_events else '--no-compile-events']
            if options.build_dataset or options.build_dataset is None and config.BUILD_DATASET:
                forwarded+=['--dataset-root',str(options.dataset_root or config.DATASET_ROOT)]
        if options.offline or action=='export':forwarded+=['--offline']
        if options.refresh_schedule is not None:
            if options.refresh_schedule:forwarded+=['--refresh-schedule']
            elif kind=='whoscored_events':forwarded+=['--no-refresh-schedule']
        elif action=='export' and kind=='whoscored_events':forwarded+=['--no-refresh-schedule']
        print(f'\n=== {action.upper()}: {kind} {competition} {season_label(season)} ===',flush=True)
        try:
            code=collect_standalone(forwarded)
        except SystemExit as exc:
            code=exc.code if isinstance(exc.code,int) else 2
        job.end(code)
        result=max(result,code)
        if code==130:return code
    return result


def _main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--collection-types',nargs='+',choices=KINDS,default=COLLECTION_TYPES)
    selected,args=parser.parse_known_args(args)
    kinds=list(dict.fromkeys(selected.collection_types))
    if 'combined' in kinds and 'sofascore' in kinds:
        parser.error('Choose combined OR sofascore; they share match checkpoints')
    if '--help' in args or '-h' in args:
        print('main.py --collection-types '+ ' '.join(KINDS))
        print('Choose any combination of collection types, except combined + sofascore. Defaults: collection.types in config/settings.yaml.')
        print('Standalone export validates/rebuilds saved responses offline; status reads saved counts.')
        return _normal_main(args)
    selection=_selection(args)
    action_parser=argparse.ArgumentParser(add_help=False)
    action_parser.add_argument('--build-dataset',action=argparse.BooleanOptionalAction)
    action_parser.add_argument('--dataset-root')
    _,command_args=action_parser.parse_known_args(args)
    explicit=bool(command_args and command_args[0] in ('start','resume','export','status','probe'))
    single=explicit or any(a in ('--competition','--season','--name') or
        a.startswith(('--competition=','--season=','--name=')) for a in args)
    action=command_args[0] if explicit else ACTION
    if action not in ('auto','start','resume','export','status','probe'):parser.error('Unknown collection action')
    if any(k not in ('combined','sofascore') for k in kinds) and any(a=='--name' or a.startswith('--name=') for a in args):
        parser.error('--name applies only to combined/Sofascore; select standalone jobs with competition/season')
    if 'sofascore' in kinds and any(a=='--providers' or a.startswith('--providers=') for a in args):
        provider_parser=argparse.ArgumentParser(add_help=False)
        provider_parser.add_argument('--providers',nargs='+')
        providers,_=provider_parser.parse_known_args(args)
        if providers.providers!=['sofascore']:parser.error('sofascore collection type requires --providers sofascore')
    progress=CollectionProgress(enabled=config.SHOW_TIMING and action not in ('status','probe'))
    standalone_options={k:_standalone_options(k,args,action) for k in kinds if k not in ('combined','sofascore')}
    targets={}
    for kind in kinds:
        targets[kind]=[]
        for competition in ([selection.competition] if single else COMPETITIONS):
            for season in ([selection.season] if single else SEASONS):
                competition=competition_by_name(competition).name
                season=season_key(season)
                directory=_job_directory(selection.data_root,kind,competition,season,selection.name if single else None)
                job=progress.job(directory,kind,competition,season_label(season))
                limit_parser=argparse.ArgumentParser(add_help=False)
                limit_parser.add_argument('--limit',type=int)
                limits,_=limit_parser.parse_known_args(args)
                if kind in ('combined','sofascore'):
                    requirements=args+(['--providers','sofascore'] if kind=='sofascore' else [])
                    job.revisit_complete=_normal_requirements(directory,requirements)[0]
                prime_job(job,directory,limit=limits.limit)
                targets[kind].append((job,directory,competition,season))
    result=0
    progress.emit('[Timing] Scrape ETAs update after each game; extension verification waits and final CSV/dataset builds are excluded.')
    with progress_scope(progress), extension_scope():
        try:
            for kind in kinds:
                if kind in ('combined','sofascore'):
                    forwarded=[a for a in args if not standalone_options or a not in ('--refresh-schedule','--no-refresh-schedule')]
                    if kind=='sofascore' and not any(a=='--providers' or a.startswith('--providers=') for a in forwarded):
                        forwarded+=['--providers','sofascore']
                    code=_normal_main(forwarded)
                else:code=_standalone(kind,standalone_options[kind],targets[kind],action)
                for job,_,_,_ in targets[kind]:
                    if not job.finished:job.end(code)
                result=max(result,code)
                if code==130:break
        except BaseException:
            for job in progress.jobs.values():
                if not job.finished:job.end(2)
            raise
        finally:
            progress.summary()
    return result


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ensemble-root',type=Path,default=config.PROJECT_ROOT/'ensemble_data')
    parser.add_argument('--import-only',action='store_true')
    parser.add_argument('--ignore-provenance',action='store_true')
    parser.add_argument('--inventory',action='store_true',help='Validate local data and rebuild this worker inventory without scraping')
    parser.add_argument('--worker-id',help='Unique name for this computer, such as main-mac or laptop')
    transfer,args=parser.parse_known_args(args)
    if '--help' in args or '-h' in args:
        print('Shared worker: run without arguments; --worker --help; --dashboard for a read-only view.')
        print('Worker inventory: --inventory --worker-id main-mac; transfers: --ensemble-root PATH --import-only --ignore-provenance')
        return _main(args)
    try:
        with ensemble_session(_selection(args).data_root,transfer.ensemble_root,skip_remote=not transfer.ignore_provenance,
                              worker_id=transfer.worker_id) as progress:
            if transfer.inventory:
                if progress is None:raise ValueError('Inventory requires a marked collection data root')
                progress.inventory_summary()
                return 0
            return 0 if transfer.import_only else _main(args)
    except (OSError,ValueError) as exc:
        print(f'Ensemble collection paused: {exc}. Staged copies are retained on an incomplete import.',flush=True)
        return 2


def entrypoint(argv=None):
    """No-argument launches use the shared plan; explicit scrape commands stay compatible."""
    args=list(sys.argv[1:] if argv is None else argv)
    shared_flags=('--worker','--monitor','--with-dashboard','--dashboard','--no-dashboard','--once','--retry','--plan')
    explicit=any(a in ('start','resume','export','status','probe','--help','-h','--inventory',
                      '--import-only','--ignore-provenance','--competition','--season','--name',
                      '--collection-types','--limit','--offline') or
                 a.startswith(('--competition=','--season=','--name=','--collection-types=','--limit=')) for a in args)
    if not explicit or any(a.split('=')[0] in shared_flags for a in args):
        from collection.worker import entrypoint as worker_entrypoint
        return worker_entrypoint(args)
    return main(args)


if __name__ == '__main__':
    raise SystemExit(entrypoint())
