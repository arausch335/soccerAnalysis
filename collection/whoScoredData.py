"""WhoScored's provider files inside the shared data/<league>/<season> tree."""
from storage.layout import system_path
import hashlib
import json
from pathlib import Path
import re
import shutil

from providers.cache import ResponseCache
from .selection import competition_by_name, season_key, season_label

LEGACY_SUBTREES=('whoscored_events','whoscored')


def validate_match(payload, mid, event):
    from review_data import who_valid
    if not isinstance(payload,dict) or not who_valid(payload,mid,event):
        raise ValueError('WhoScored match identity, finished status, date, teams or events differ from its fixture')
    return payload


def legacy_directory(directory):
    directory=Path(directory)
    return directory.parent.parent.name in LEGACY_SUBTREES


def metadata_path(directory,name):
    directory=Path(directory)
    if legacy_directory(directory):
        old=directory/name;prefixed=directory/('whoscored_'+name)
        return prefixed if prefixed.exists() and not old.exists() else old
    return directory/('whoscored_'+name)


def standalone_directory(root, competition, season):
    slug=re.sub(r'[^a-z0-9]+','-',competition_by_name(competition).name.lower()).strip('-')
    return Path(root)/slug/season_label(season)


def standalone_fixtures(directory):
    directory=Path(directory)
    candidates=[directory,*[directory.parent.parent/subtree/directory.parent.name/directory.name for subtree in LEGACY_SUBTREES]]
    source=next((path for path in candidates if metadata_path(path,'schedule.json').exists()),directory)
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
    old=[root/subtree/directory.parent.name/directory.name for subtree in LEGACY_SUBTREES]
    target=ResponseCache(directory/'raw').path('whoscored',f'match/{mid}')
    for source in (canonical,*old):
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
    root=Path(root).resolve();migrated=0
    sources=[]
    for subtree in LEGACY_SUBTREES:
        legacy=root/subtree
        if legacy.is_symlink():raise ValueError('Legacy WhoScored root is a symbolic link')
        sources.extend(path for path in sorted(legacy.glob('*/*')) if path.is_dir())
    for source in sources:
        if source.is_symlink() or source.parent.is_symlink():raise ValueError('Legacy WhoScored collection contains a symbolic link')
        job=metadata_path(source,'job.json')
        settings=(json.loads(job.read_text(encoding='utf-8')) if job.exists() else
                  {'provider':'whoscored','competition':competition_by_name(source.parent.name).name,'season':season_key(source.name)})
        destination=standalone_directory(root,settings['competition'],settings['season'])
        if (settings.get('provider')!='whoscored' or competition_by_name(source.parent.name).name!=competition_by_name(settings['competition']).name
                or season_key(source.name)!=season_key(settings['season'])):raise ValueError(f'WhoScored legacy selection mismatch: {source}')
        if any(parent.is_symlink() for parent in (destination,*destination.parents)) or root not in destination.resolve().parents:
            raise ValueError('WhoScored migration destination leaves the data root')
        files=[]
        for path in sorted(source.rglob('*')):
            if path.is_symlink():raise ValueError('Legacy WhoScored collection contains a symbolic link')
            if path.is_file():files.append((path,path.relative_to(source),digest(path)))
        # Validate every raw match before any move; only exact responses survive.
        schedule=metadata_path(source,'schedule.json')
        saved_schedule=json.loads(schedule.read_text(encoding='utf-8')) if schedule.exists() else None
        if saved_schedule is not None and saved_schedule.get('provider')!='whoscored':
            raise ValueError('Legacy WhoScored schedule namespace mismatch')
        events={row['id']:row for row in saved_schedule['events']} if saved_schedule else {}
        from review_data import who_valid
        raw_targets={}
        for path,relative,_ in files:
            if relative.parts[:2]==('raw','whoscored') or (path.suffix=='.json' and relative.parts[0]=='raw' and len(relative.parts)==2):
                saved=json.loads(path.read_text(encoding='utf-8'));mid=saved.get('payload',{}).get('match_id')
                if saved.get('provider')!='whoscored' or saved.get('key')!=f'match/{mid}' or not who_valid(saved.get('payload',{}),mid):
                    raise ValueError(f'Invalid legacy WhoScored raw response: {path}')
                if saved_schedule is not None:
                    if mid not in events:raise ValueError(f'Legacy WhoScored response has no fixture: {path}')
                    validate_match(saved['payload'],mid,events[mid])
                raw_targets[relative]=ResponseCache(destination/'raw').path('whoscored',f'match/{mid}')
        batch=hashlib.sha256(json.dumps([(str(relative),checksum) for _,relative,checksum in files]).encode()).hexdigest()[:16]
        archive=system_path(root,'ensemble_imports')/'whoscored-layout'/source.parent.parent.name/source.parent.name/source.name/batch
        if any(parent.is_symlink() for parent in (archive,*archive.parents)) or root not in archive.resolve().parents:
            raise ValueError('WhoScored migration archive leaves the data root')
        for path,relative,_ in files:
            basename=relative.name.removeprefix('whoscored_')
            target=raw_targets.get(relative,destination/relative if relative.parts[0]=='raw' else destination/('whoscored_'+basename))
            if len(relative.parts)>1 and relative.parts[0]!='raw':target=destination/'whoscored_metadata'/relative
            if any(parent.is_symlink() for parent in (target,*target.parents)) or root not in target.resolve().parents:
                raise ValueError('WhoScored migration destination leaves the data root')
            if target.exists() and digest(target)!=digest(path):
                atomic_copy(path,archive/'incoming'/relative)
                if relative in raw_targets:
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
                elif basename=='schedule.json':
                    atomic_json(target,merge_value(json.loads(target.read_text(encoding='utf-8')),json.loads(path.read_text(encoding='utf-8'))))
                elif basename=='job.json':
                    previous=json.loads(target.read_text(encoding='utf-8'))
                    if (previous.get('provider')!='whoscored' or
                            competition_by_name(previous['competition']).name!=competition_by_name(settings['competition']).name or
                            season_key(previous['season'])!=season_key(settings['season'])):
                        raise ValueError('WhoScored migration job identity conflict')
                # Preserve both conflicting originals; the existing validated
                # response remains active until an explicit refresh/export.
                continue
            if not target.exists():atomic_copy(path,target)
        if not job.exists():atomic_json(metadata_path(destination,'job.json'),settings)
        current_paths=list(source.rglob('*'))
        if (any(path.is_symlink() for path in current_paths)
                or {path.relative_to(source) for path in current_paths if path.is_file()}!={relative for _,relative,_ in files}
                or not all(path.exists() and digest(path)==checksum for path,_,checksum in files)):
            raise OSError('Legacy WhoScored data changed during migration; retained source')
        atomic_json(archive/'migration.json',{'state':'complete','destination':str(destination),
            'files':[{'path':str(relative),'sha256':checksum} for _,relative,checksum in files]})
        shutil.rmtree(source);migrated+=1
        report(f'WhoScored: migrated {source.parent.name} {source.name} into {destination}',flush=True)
    for subtree in LEGACY_SUBTREES:
        old=root/subtree
        if old.exists():
            for child in sorted(old.glob('*')):
                if child.is_dir() and not any(child.iterdir()):child.rmdir()
            if not any(old.iterdir()):old.rmdir()
    return migrated
