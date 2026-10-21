"""Offline, journaled ingestion of another computer's complete data folder."""
from storage.layout import full_path
from storage.layout import system_path
from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from filelock import FileLock
import pandas as pd
from storage.pandasStore import PandasStore, SCHEMA, KEYS, atomic_json, atomic_replace, clean
from .ensembleProgress import PortableProgress, progress_session
from .selection import season_key, competition_by_name
from .workerIdentity import ensure_identity, read_worker_id, read_origins, save_origins

MACHINE_STATE={'_system','safety','browser_profiles','http_sessions','.ensemble-computer.json','identity.json','worker_runtime','reporting.json'}
STATUS={'not_attempted':0,'pending':0,'elsewhere':0,'failed':1,'partial':2,'visible_only':2,
        'deferred':3,'unavailable':3,'complete':4}


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def merge_value(old, new):
    if isinstance(old,dict) and isinstance(new,dict):
        if (isinstance(old.get('status',{}),dict) and isinstance(new.get('status',{}),dict) and
                old.get('status',{}).get('type')!='finished' and
                new.get('status',{}).get('type')=='finished'):
            return merge_value(new,old)
        merged={key:merge_value(old[key],value) if key in old else value for key,value in new.items()}
        merged={**old,**merged}
        if 'coverage' in merged and 'errors' in merged:
            unfinished=any(value in ('failed','visible_only','not_attempted') for value in merged['coverage'].values())
            merged['errors']=list(dict.fromkeys(merged.get('errors',[]))) if unfinished else []
            if 'complete' in merged:merged['complete']=not unfinished
            if 'blocked' in merged and not unfinished:merged['blocked']=False
        return merged
    if isinstance(old,list) and isinstance(new,list):
        if all(isinstance(item,dict) and 'id' in item for item in old+new):
            rows={str(item['id']):item for item in old}
            for item in new:rows[str(item['id'])]=merge_value(rows.get(str(item['id']),{}),item)
            return list(rows.values())
        rows={json.dumps(item,sort_keys=True):item for item in old+new}
        return list(rows.values())
    if isinstance(old,str) and isinstance(new,str) and old in STATUS and new in STATUS:
        return new if STATUS[new]>STATUS[old] else old
    if isinstance(old,bool) and isinstance(new,bool):return old or new
    return new if old is None or isinstance(old,str) and old=='' else old


def normalize_paths(value, paths=False):
    if isinstance(value,dict):
        return {key:normalize_paths(item,paths or key in ('raw_response_files','sofascore_spatial_files','cache_directory'))
                for key,item in value.items()}
    if isinstance(value,list):return [normalize_paths(item,paths) for item in value]
    return value.replace('\\','/') if paths and isinstance(value,str) else value


def atomic_copy(source, target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.'+uuid.uuid4().hex[:8]+'.tmp')
    try:
        shutil.copyfile(source,temporary)
        if digest(source)!=digest(temporary):raise OSError('Imported file failed its copy checksum')
        atomic_replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)


def merged_checkpoint(source, target, staged):
    incoming=PandasStore(source).read_frames()
    PandasStore.from_frames(incoming)
    if not PandasStore(target).exists:
        frames=incoming
    else:
        local=PandasStore(target).read_frames();frames={}
        for table,schema in SCHEMA.items():
            rows={}
            for frame in (local[table],incoming[table]):
                for row in frame.to_dict('records'):
                    row={key:normalize_paths(clean(value)) for key,value in row.items()}
                    key=tuple(row[column] for column in KEYS[table])
                    if table=='matches' and key in rows:
                        for column in ('competition_id','season_id','home_team_id','away_team_id','kickoff'):
                            if rows[key][column]!=row[column]:raise ValueError('Checkpoint match identity conflict')
                    rows[key]=merge_value(rows[key],row) if key in rows else row
            frames[table]=pd.DataFrame(rows.values(),columns=list(schema))
            for column,dtype in schema.items():
                if dtype!='json':frames[table][column]=frames[table][column].astype(dtype)
    dataset=PandasStore.from_frames(frames)
    from providers.statistics import synthesize
    for match in dataset.matches.values():
        match.metadata=normalize_paths(match.metadata)
        for lineup in match.lineups:
            if lineup is None:continue
            for appearance in lineup.players:
                combined=synthesize(appearance.stats.raw,appearance.stats.raw.get('_providers',{}),
                                    appearance.starter or appearance.did_play)
                appearance.stats.raw['_combined']=combined['metrics']
                appearance.stats.raw['_combined_sources']=combined['sources']
                appearance.stats.actions=combined['actions']
    PandasStore(staged).to_csv(dataset)


def merge_csv(source, target, output):
    def read(path):
        with path.open(encoding='utf-8-sig',newline='') as handle:
            reader=csv.DictReader(handle);rows=list(reader)
            if None in (reader.fieldnames or []) or any(None in row for row in rows):raise ValueError('Invalid imported CSV')
            return reader.fieldnames or [],rows
    columns,old=read(target);incoming,new=read(source)
    columns=list(dict.fromkeys(columns+incoming))
    if source.name in ('events.csv','whoscored_events.csv') and {'match_id','event_index'}<=set(columns):
        keys=['match_id','event_index']
    elif source.name in ('events.csv','whoscored_events.csv') and {'wsc_match_id','event_index'}<=set(columns):
        keys=['wsc_match_id','event_index']
    elif {'wsc_match_id','wsc_player_id'}<=set(columns):keys=['wsc_match_id','wsc_player_id']
    elif {'wsc_match_id','wsc_team_id'}<=set(columns):keys=['wsc_match_id','wsc_team_id']
    elif 'wsc_match_id' in columns:keys=['wsc_match_id']
    elif 'resource' in columns and 'match_id' in columns:keys=['match_id','resource']
    elif 'match_id' in columns and 'player_id' in columns:keys=['match_id','player_id']
    elif 'fmb_match_id' in columns and 'fmb_player_id' in columns:keys=['fmb_match_id','fmb_player_id']
    elif source.name=='collection.csv' and 'match_id' in columns:keys=['match_id']
    else:keys=columns
    rows={}
    for row in old+new:
        key=tuple(row.get(column,'') for column in keys)
        rows[key]=merge_value(rows[key],row) if key in rows else row
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=columns);writer.writeheader();writer.writerows(rows.values())


def import_folder(source, root, report=print, *, background=False, check=lambda:None):
    source=full_path(source);root=full_path(root)
    check()
    if (source/'_system/transfer-manifest.json').exists():
        from tools.prepare_transfer import verify_bundle
        verify_bundle(source)
    report(f'Ensemble: validating copied data folder {source}',flush=True)
    # Never follow staged links into the live data tree or another directory.
    if json.loads((source/'.soccer-scraper.json').read_text(encoding='utf-8')).get('format')!=1:
        raise ValueError('Unsupported source data folder marker')
    files=[]
    for path in sorted(source.rglob('*')):
        check()
        if path.is_symlink():raise ValueError(f'Imported folder contains a symbolic link: {path}')
        if path.name=='.DS_Store' or path.name.startswith('._'):continue
        if path.is_file() and not path.name.endswith('.lock'):
            relative=path.relative_to(source)
            if '.ensemble_work' not in relative.parts:files.append((relative,path,digest(path)))
            if len(files) and len(files)%1000==0:report(f'Ensemble: checked {len(files)} files...',flush=True)
    batch=hashlib.sha256(json.dumps([(str(p),h) for p,_,h in files]).encode()).hexdigest()[:24]
    archive=system_path(root,'ensemble_imports')/batch
    journal=archive/'import.json'
    stores=[]
    for relative,path,_ in files:
        check()
        if path.name=='CURRENT.json' and relative.parts[0] not in MACHINE_STATE|{'ensemble_imports'}:
            PandasStore(path.parent).from_csv();stores.append(relative.parent)
        elif path.suffix=='.json':
            json.loads(path.read_text(encoding='utf-8'))
        elif path.suffix=='.csv':
            with path.open(encoding='utf-8-sig',newline='') as handle:
                for row in csv.DictReader(handle):
                    if None in row or None in row.values():raise ValueError(f'Invalid imported CSV: {relative}')
    source_progress=PortableProgress(None,source)
    source_progress.scan(cache=False,cancel=check if hasattr(check,'is_set') else None)
    local_progress=PortableProgress(None,root)
    if not background:local_progress.scan()
    for relative in stores:
        if any(parent in relative.parents for parent in stores):
            raise ValueError('Nested checkpoint stores are not supported')
    root.mkdir(parents=True,exist_ok=True)
    from .maintenance import require_space
    require_space(root,sum(path.stat().st_size for _,path,_ in files)*3)
    with tempfile.TemporaryDirectory(prefix='e-',dir=root/'_system/storage' if background else root) as temp:
        staging=Path(temp);planned=[];store_manifests=[]
        for relative in stores:
            check()
            merged_checkpoint(source/relative,root/relative,staging/'active'/relative)
            for path in (staging/'active'/relative).rglob('*'):
                if path.is_file():
                    item=(path,root/path.relative_to(staging/'active'))
                    (store_manifests if path.name=='CURRENT.json' else planned).append(item)
        for relative,path,checksum in files:
            check()
            archived=archive/'conflicts'/relative
            if any(relative==store or store in relative.parents for store in stores):
                # Keep the exact incoming checkpoint generation as provenance.
                planned.append((path,archive/'objects'/checksum));continue
            target=root/relative
            machine=relative.parts[0] in MACHINE_STATE
            if machine:
                (archive/'machine_state').mkdir(parents=True,exist_ok=True,mode=0o700)
                planned.append((path,archive/'machine_state'/relative));continue
            if relative==Path('.soccer-scraper.json'):
                planned.append((path,archive/'source-root.json'));continue
            output=staging/'active'/relative
            if target.exists() and digest(target)!=checksum:
                planned.append((path,archived))
                if path.suffix=='.json':
                    old=json.loads(target.read_text(encoding='utf-8'));new=json.loads(path.read_text(encoding='utf-8'))
                    if path.name in ('job.json','config.json'):
                        if not isinstance(old,dict) or not isinstance(new,dict):raise ValueError(f'Invalid collection settings: {relative}')
                        for key in ('competition','season','provider','id_namespace','baseline_provider'):
                            previous=old.get(key);incoming=new.get(key)
                            if key=='season' and previous and incoming:
                                previous,incoming=season_key(previous),season_key(incoming)
                            if key=='competition' and previous and incoming:
                                previous,incoming=competition_by_name(previous).name,competition_by_name(incoming).name
                            if key in old and key in new and previous!=incoming:
                                raise ValueError(f'Imported collection identity differs: {relative}')
                    # A successful cache response supersedes an old negative cache.
                    if isinstance(old,dict) and isinstance(new,dict) and old.get('status')==404 and new.get('status')==200:merged=new
                    elif (isinstance(old,dict) and isinstance(new,dict) and
                          old.get('provider')=='whoscored' and str(old.get('key','')).startswith('match/')):
                        if new.get('provider')!='whoscored' or old['key']!=new.get('key'):
                            raise ValueError('Imported WhoScored cache identity differs')
                        # Event arrays are immutable responses. Merging their
                        # fields or list items can fabricate a stream neither
                        # computer collected. Retain one exact full response.
                        old_events=old.get('payload',{}).get('matchCentreData',{}).get('events',[])
                        new_events=new.get('payload',{}).get('matchCentreData',{}).get('events',[])
                        merged=new if len(new_events)>len(old_events) else old
                    else:merged=merge_value(old,new)
                    atomic_json(output,normalize_paths(merged))
                elif path.suffix=='.csv':merge_csv(path,target,output)
                else:continue # Existing bytes remain active; incoming bytes are archived.
                planned.append((output,target))
            elif not target.exists():
                if path.suffix=='.json':
                    atomic_json(output,normalize_paths(json.loads(path.read_text(encoding='utf-8'))))
                    planned.append((output,target))
                else:planned.append((path,target))
        atomic_json(journal,{'state':'applying','source_name':source.name,'source_group':source.parent.name,
            'source_worker_id':read_worker_id(source),
            'imported_at':datetime.now(timezone.utc).isoformat(),
            'files':[{'path':str(relative),'sha256':checksum} for relative,_,checksum in files],
            'conflicts':'conflicts/','machine_state':'machine_state/',
            'checkpoint_objects':{relative.as_posix():checksum for relative,_,checksum in files
                if any(relative==store or store in relative.parents for store in stores)}})
        # Data tables arrive before CURRENT pointers publish a complete snapshot.
        reports=[item for item in planned if item[1].name=='collection.csv']
        ordinary=[item for item in planned if item[1].name!='collection.csv']
        writes=ordinary+store_manifests+reports
        report(f'Ensemble: merging {len(stores)} checkpoints and incorporating {len(writes)} files...',flush=True)
        for index,(incoming,target) in enumerate(writes,1):
            check()
            from .maintenance import require_space
            require_space(root,incoming.stat().st_size)
            if target.is_symlink() or root not in target.resolve().parents:
                raise ValueError('Import destination leaves the regular data root')
            if not target.exists() or digest(target)!=digest(incoming):atomic_copy(incoming,target)
            if digest(incoming)!=digest(target):raise OSError('Published import checksum differs')
            if index%1000==0:report(f'Ensemble: incorporated {index}/{len(writes)} files...',flush=True)
        origins=read_origins(root)
        for progress in (local_progress,source_progress):
            for key,row in progress.local_records.items():
                origins[key]=sorted(set(origins.get(key,[]))|set(row['origins']))
        save_origins(root,origins)
        if not background:PortableProgress(None,root).scan()
        atomic_json(journal,{**json.loads(journal.read_text(encoding='utf-8')),'state':'complete'})
        if all(path.exists() and digest(path)==checksum for _,path,checksum in files):
            shutil.rmtree(source)
        else:raise OSError('Staged source changed during import; kept it for review')
    report(f'Ensemble: incorporated {len(files)} files from {source.name}; provenance: {journal}')


def import_pending(root, ensemble, report=print):
    root=full_path(root);ensemble=full_path(ensemble)
    if root==ensemble or root in ensemble.parents or ensemble in root.parents:
        raise ValueError('ensemble_data and the regular data root must be separate folders')
    ensemble.mkdir(parents=True,exist_ok=True)
    candidates=[]
    for marker in sorted(ensemble.rglob('.soccer-scraper.json')):
        candidate=marker.parent
        if candidate!=ensemble and not any(parent==candidate or parent in candidate.parents for parent in candidates):
            candidates.append(candidate)
    if not candidates:return
    marker=root/'.soccer-scraper.json'
    if root.exists() and any(root.iterdir()) and not marker.exists():
        raise ValueError('Cannot import into an unmarked data root')
    if not marker.exists():atomic_json(marker,{'format':1})
    elif json.loads(marker.read_text(encoding='utf-8')).get('format')!=1:raise ValueError('Unsupported receiving data folder format')
    with FileLock(str(system_path(root,'.worker.lock')),timeout=0), FileLock(str(system_path(root,'.collector.lock')),timeout=0):
        for candidate in candidates:import_folder(candidate,root,report)
        from .whoScoredData import migrate_legacy
        migrate_legacy(root,report)


@contextmanager
def ensemble_session(root, ensemble, report=print, *, skip_remote=True, worker_id=None,full_inventory=False,background=False):
    root=full_path(root);ensemble=full_path(ensemble)
    if root==ensemble or root in ensemble.parents or ensemble in root.parents:
        raise ValueError('ensemble_data and the regular data root must be separate folders')
    marker=root/'.soccer-scraper.json'
    marked=marker.exists()
    if marked or not root.exists() or not any(root.iterdir()):
        if not marked:atomic_json(marker,{'format':1})
        elif json.loads(marker.read_text(encoding='utf-8')).get('format')!=1:raise ValueError('Unsupported data folder format')
        with FileLock(str(system_path(root,'.collector.lock')),timeout=0):ensure_identity(root,worker_id)
    if not background:import_pending(root,ensemble,report)
    # An existing unmarked directory remains subject to the collector's normal
    # refusal. Do not turn an arbitrary folder into a collection implicitly.
    if root.exists() and any(root.iterdir()) and not (root/'.soccer-scraper.json').exists():
        yield None;return
    if not (root/'.soccer-scraper.json').exists():atomic_json(root/'.soccer-scraper.json',{'format':1})
    with FileLock(str(system_path(root,'.collector.lock')),timeout=0):
        from .whoScoredData import migrate_legacy
        if not background:migrate_legacy(root,report)
        progress=PortableProgress(ensemble/'provenance.json',root,worker_id=worker_id)
        progress.skip_remote=skip_remote
        if background:
            progress.local_records=dict(progress.previous_local)
            report('Inventory indexing and staged imports run in the background; collection checks saved matches as needed.')
        else:progress.scan(full=full_inventory)
        if not background:
            report(f'Inventory: {progress.scan_summary.get("reused",0)} unchanged artifacts reused; '
                   f'{progress.scan_summary.get("validated",0)} new/changed artifacts validated.')
    report(f'Portable collection progress: {progress.path}')
    report(f'Worker identity: {progress.origin} ({system_path(root,"identity.json")})')
    report(f'Local worker inventory: {progress.worker_path}')
    with progress_session(progress):
        try:yield progress
        finally:progress.save()
