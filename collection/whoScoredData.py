"""WhoScored's provider files inside the shared data/<league>/<season> tree."""
import json
from pathlib import Path
import re
import shutil

from providers.cache import ResponseCache
from .selection import competition_by_name, season_key, season_label


def validate_match(payload, mid, event):
    from review_data import who_valid
    if not isinstance(payload,dict) or not who_valid(payload,mid,event):
        raise ValueError('WhoScored match identity, finished status, date, teams or events differ from its fixture')
    return payload


def legacy_directory(directory):
    directory=Path(directory)
    return directory.parent.parent.name=='whoscored_events'


def metadata_path(directory,name):
    directory=Path(directory)
    return directory/(name if legacy_directory(directory) else 'whoscored_'+name)


def standalone_directory(root, competition, season):
    slug=re.sub(r'[^a-z0-9]+','-',competition_by_name(competition).name.lower()).strip('-')
    return Path(root)/slug/season_label(season)


def standalone_fixtures(directory):
    directory=Path(directory)
    old=directory.parent.parent/'whoscored_events'/directory.parent.name/directory.name
    source=directory if metadata_path(directory,'schedule.json').exists() else old
    schedule=metadata_path(source,'schedule.json');job=metadata_path(source,'job.json')
    if not schedule.exists() or not job.exists():return []
    settings=json.loads(job.read_text(encoding='utf-8'))
    if (settings.get('provider')!='whoscored' or
            standalone_directory(directory.parent.parent,settings['competition'],settings['season'])!=directory):
        raise ValueError('WhoScored collection selection mismatch')
    saved=json.loads(schedule.read_text(encoding='utf-8'))
    if saved.get('provider')!='whoscored':raise ValueError('WhoScored schedule namespace mismatch')
    return saved['events']


def reuse_match(directory, mid, event, *, standalone=False):
    directory=Path(directory)
    root=directory.parents[2] if legacy_directory(directory) else directory.parent.parent
    canonical=root/directory.parent.name/directory.name
    old=root/'whoscored_events'/directory.parent.name/directory.name
    target=ResponseCache(directory/'raw').path('whoscored',f'match/{mid}')
    for source in (canonical,old):
        path=ResponseCache(source/'raw').path('whoscored',f'match/{mid}')
        if path==target or not path.exists():continue
        payload=ResponseCache(source/'raw').fetch('whoscored',f'match/{mid}',lambda:None)
        validate_match(payload,mid,event)
        from .ensemble import atomic_copy
        atomic_copy(path,target)
        return payload
    return None


def migrate_legacy(root,report=print):
    """Copy/verify legacy collections before removing their old location.

    Called under the collector lock. Existing baseline schedules/jobs are never
    overwritten; WhoScored's metadata and native CSVs have explicit filenames.
    """
    from .ensemble import atomic_copy, digest, merge_value
    from storage.pandasStore import atomic_json
    root=Path(root)
    for job in sorted((root/'whoscored_events').glob('*/*/job.json')):
        source=job.parent;settings=json.loads(job.read_text(encoding='utf-8'))
        destination=standalone_directory(root,settings['competition'],settings['season'])
        if (settings.get('provider')!='whoscored' or source.parent.name!=destination.parent.name
                or source.name!=destination.name):raise ValueError(f'WhoScored legacy selection mismatch: {source}')
        files=[]
        for path in sorted(source.rglob('*')):
            if path.is_symlink():raise ValueError('Legacy WhoScored collection contains a symbolic link')
            if path.is_file():files.append((path,path.relative_to(source),digest(path)))
        # Validate every raw match before any move; only exact responses survive.
        events={row['id']:row for row in json.loads((source/'schedule.json').read_text(encoding='utf-8'))['events']} if (source/'schedule.json').exists() else {}
        for path,relative,_ in files:
            if relative.parts[:2]==('raw','whoscored'):
                saved=json.loads(path.read_text(encoding='utf-8'));mid=saved.get('payload',{}).get('match_id')
                if saved.get('provider')!='whoscored' or saved.get('key')!=f'match/{mid}' or mid not in events:
                    raise ValueError(f'Invalid legacy WhoScored raw response: {path}')
                validate_match(saved['payload'],mid,events[mid])
        archive=root/'ensemble_imports'/'whoscored-layout'/source.parent.name/source.name
        for path,relative,_ in files:
            target=(destination/relative if relative.parts[0]=='raw' else destination/('whoscored_'+relative.name))
            if len(relative.parts)>1 and relative.parts[0]!='raw':target=destination/'whoscored_metadata'/relative
            if target.exists() and digest(target)!=digest(path):
                atomic_copy(path,archive/'incoming'/relative)
                if relative.parts[:2]==('raw','whoscored'):
                    from review_data import who_valid
                    try:previous=json.loads(target.read_text(encoding='utf-8'))
                    except (ValueError,UnicodeError):previous={}
                    incoming=json.loads(path.read_text(encoding='utf-8'))
                    old_payload=previous.get('payload',{});new_payload=incoming['payload']
                    if (previous.get('provider')!='whoscored' or previous.get('key')!=incoming['key']
                            or not who_valid(old_payload,new_payload['match_id'])
                            or len(new_payload['matchCentreData']['events'])>len(old_payload['matchCentreData']['events'])):
                        atomic_copy(target,archive/'existing'/relative)
                        atomic_copy(path,target)
                elif relative.name=='schedule.json':
                    atomic_json(target,merge_value(json.loads(target.read_text(encoding='utf-8')),json.loads(path.read_text(encoding='utf-8'))))
                elif relative.name=='job.json':
                    previous=json.loads(target.read_text(encoding='utf-8'))
                    if (previous.get('provider')!='whoscored' or
                            competition_by_name(previous['competition']).name!=competition_by_name(settings['competition']).name or
                            season_key(previous['season'])!=season_key(settings['season'])):
                        raise ValueError('WhoScored migration job identity conflict')
                # Preserve both conflicting originals; the existing validated
                # response remains active until an explicit refresh/export.
                continue
            if not target.exists():atomic_copy(path,target)
        if not all(path.exists() and digest(path)==checksum for path,_,checksum in files):
            raise OSError('Legacy WhoScored data changed during migration; retained source')
        atomic_json(archive/'migration.json',{'state':'complete','destination':str(destination),
            'files':[{'path':str(relative),'sha256':checksum} for _,relative,checksum in files]})
        shutil.rmtree(source)
        report(f'WhoScored: migrated {source.parent.name} {source.name} into {destination}',flush=True)
    old=root/'whoscored_events'
    if old.exists():
        for child in sorted(old.glob('*')):
            if child.is_dir() and not any(child.iterdir()):child.rmdir()
        if not any(old.iterdir()):old.rmdir()
