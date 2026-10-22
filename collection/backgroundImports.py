"""Resumable, space-bounded ingestion without holding the collector lock."""
from storage.layout import full_path
from storage.sqlitePaths import readonly_uri
from contextlib import closing
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
from .maintenance import available, require_space, LowSpace
from .storageTransfer import digest, identifier, link_or_copy
from .workerIdentity import ensure_identity, save_origins
from storage.pandasStore import SCHEMA, atomic_json

MAX_BATCH=128*1024**2


def import_parts(root, files, *, owner, batch, origins=None, check=lambda:None, report=print):
    """Commit one checkpoint (including its raw dependencies) at a time.

    Receipts survive interruptions. Other files are batched to the available
    staging budget. Originals stay put until the caller acknowledges all parts.
    The caller owns a cooperative season lease for the affected season(s).
    """
    from .ensemble import import_folder
    root=full_path(root);directory=root/'_system/storage';directory.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(directory/'parts.sqlite',timeout=30)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS parts(batch TEXT,path TEXT,sha TEXT,PRIMARY KEY(batch,path))');db.commit()
        completed=dict(db.execute('SELECT path,sha FROM parts WHERE batch=?',(batch,)))
        descriptions={}
        for relative,path in files.items():
            check();path=Path(path)
            descriptions[relative]={'path':relative,'size':path.stat().st_size,'sha256':digest(path)}
        remaining={key for key,row in descriptions.items() if completed.get(key)!=row['sha256']}
        common={key for key in files if key=='.soccer-scraper.json' or
                ('match_checkpoints' not in Path(key).parts and 'raw' not in Path(key).parts and len(Path(key).parts)<=4)}
        checkpoints=[]
        for key in sorted(files):
            if not key.endswith('/CURRENT.json'):continue
            parent=key.rsplit('/',1)[0];generation=json.loads(Path(files[key]).read_text(encoding='utf-8'))['generation']
            members={key}|{parent+'/snapshots/'+generation+'/'+name+'.csv' for name in SCHEMA}
            if not members<=files.keys():raise ValueError('Incomplete checkpoint transfer')
            season=parent.split('/match_checkpoints/')[0]
            with Path(files[parent+'/snapshots/'+generation+'/matches.csv']).open(encoding='utf-8',newline='') as stream:
                for row in csv.DictReader(stream):
                    metadata=json.loads(row['metadata'])
                    def paths(value):
                        if isinstance(value,dict):
                            for item in value.values():yield from paths(item)
                        elif isinstance(value,list):
                            for item in value:yield from paths(item)
                        elif isinstance(value,str):yield value.replace('\\','/')
                    for value in paths({k:metadata.get(k,{}) for k in ('raw_response_files','sofascore_spatial_files')}):
                        relative=season+'/'+value
                        if relative in files:members.add(relative)
            if members & remaining:checkpoints.append(members)
        def commit(members):
            check();members=set(members)|common
            size=sum(descriptions[key]['size'] for key in members)
            # Source staging, merged frames, preserved incoming bytes and atomic
            # destination copies can coexist. Hard links reduce actual usage.
            require_space(root,size*4+1024**2)
            with tempfile.TemporaryDirectory(prefix='i-',dir=directory) as temporary:
                source=Path(temporary)/'s';source.mkdir()
                for key in sorted(members):
                    check();require_space(root,descriptions[key]['size']*4)
                    link_or_copy(files[key],source/key)
                identity=ensure_identity(source,owner)
                atomic_json(source/'_system/config/identity.json',{
                    **identity,'instance_id':uuid.uuid5(uuid.NAMESPACE_URL,'soccer-import:'+owner).hex})
                save_origins(source,origins or {})
                import_folder(source,root,report,background=True,check=check)
            db.executemany('INSERT OR REPLACE INTO parts VALUES(?,?,?)',
                           [(batch,key,descriptions[key]['sha256']) for key in members]);db.commit()
            remaining.difference_update(members)
        for members in checkpoints:commit(members)
        # Orphan/standalone provider raw files still belong in the archive.
        while remaining:
            check();budget=min(MAX_BATCH,available(root)//4)-1024**2
            group=set();size=sum(descriptions[key]['size'] for key in common)
            for key in sorted(remaining):
                amount=descriptions[key]['size']
                if size+amount<=budget:group.add(key);size+=amount
                elif not group:raise LowSpace('Background import waiting for enough space for its next verified file')
                else:break
            if not group:raise LowSpace('Background import staging budget exhausted')
            commit(group)
    return True


def import_staged(root, ensemble, *, stop=None, report=print,on_season=None):
    """Ingest completed portable folders in the background, preserving originals on error."""
    from .maintenance import background, Checkpoint
    from .workerIdentity import read_worker_id, read_origins
    from tools.prepare_transfer import verify_bundle
    root=full_path(root);ensemble=full_path(ensemble)
    if root==ensemble or root in ensemble.parents or ensemble in root.parents:
        raise ValueError('Staged imports must be separate from the data root')
    ensemble.mkdir(parents=True,exist_ok=True);changed=False
    candidates=[]
    for marker in sorted(ensemble.rglob('.soccer-scraper.json')):
        source=marker.parent
        if source==ensemble or any(parent in source.parents for parent in candidates):continue
        candidates.append(source)
    for source in candidates:
        check=Checkpoint(stop);check()
        # A marker copied at the start of a long drive transfer is not a
        # completion signal. Only complete, verified portable bundles are ready.
        if not (source/'_system/transfer-manifest.json').is_file():continue
        manifest=verify_bundle(source)
        files={row['path']:source/row['path'] for row in manifest['files']
               if row['path']=='.soccer-scraper.json' or not row['path'].startswith('_system/')}
        owner=manifest.get('worker') or read_worker_id(source);batch=identifier(manifest);origins=read_origins(source)
        def selected_season(key):
            parts=Path(key).parts
            return '/'.join(parts[1:3] if parts[0] in ('whoscored','whoscored_events','fotmob_player_stats') else parts[:2])
        seasons=sorted({selected_season(key) for key in files if key!='.soccer-scraper.json'})
        # A folder may span several seasons; each season can advance independently.
        pending=False
        from filelock import Timeout
        from .maintenance import DeferredWork
        from .inventoryCache import InventoryInterrupted
        for season in seasons:
            check()
            try:
                with background(root,season,stop) as permit:
                    selected={key:path for key,path in files.items() if key=='.soccer-scraper.json' or selected_season(key)==season}
                    import_parts(root,selected,owner=owner,batch=batch,origins=origins,check=permit,report=report)
                    changed=True
            except LowSpace:raise
            except (Timeout,DeferredWork,InventoryInterrupted):pending=True
        if pending:continue
        check()
        for row in manifest['files']:
            path=source/row['path']
            if not path.is_file() or path.stat().st_size!=row['size'] or digest(path)!=row['sha256']:
                raise ValueError('Staged source changed during background import; source retained')
        verify_bundle(source)
        shutil.rmtree(source)
        if on_season and not manifest.get('partial'):
            for season in seasons:on_season(season)
        report('[Storage] Staged data incorporated; original collector data is unchanged.',flush=True)
    return changed


def snapshot_data(root, output, *, check=lambda:None):
    """Pin immutable checkpoint generations briefly; never hold a season lock.

    Atomic raw responses can be hard-linked too. A later scraper replacement or
    checkpoint cleanup cannot change the pinned files used by this build.
    """
    from filelock import FileLock
    root=Path(root);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    def pin(source):
        check();target=output/source.relative_to(root)
        require_space(output,source.stat().st_size if source.stat().st_dev!=output.stat().st_dev else 4096)
        link_or_copy(source,target)
    pin(root/'.soccer-scraper.json')
    for directory in sorted(root.glob('*/*')):
        if directory.relative_to(root).parts[0]=='_system' or not (directory/'job.json').is_file():continue
        for source in directory.glob('*'):
            if source.is_file() and source.suffix in ('.csv','.json'):pin(source)
        stores=[*(directory/'match_checkpoints').glob('*'),directory/'dataset']
        for store in stores:
            check()
            with FileLock(str(store/'.checkpoint.lock')):
                from storage.pandasStore import PandasStore
                saved=PandasStore(store)
                if not saved.exists:continue
                current=saved.current_directory
                for source in [store/'CURRENT.json',*[current/(name+'.csv') for name in SCHEMA]]:pin(source)
        for source in (directory/'raw').rglob('*'):
            if source.is_file() and source.suffix in ('.csv','.json'):pin(source)
    # Review database: use SQLite's online snapshot, rather than copying an open
    # WAL database. Transfer secrets/runtime state never enter a build snapshot.
    identity=root/'_system/identity'
    for source in identity.glob('*'):
        if source.name=='review.sqlite':
            target=output/source.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(readonly_uri(source),uri=True)) as incoming,closing(sqlite3.connect(target)) as outgoing:
                incoming.backup(outgoing,pages=64,progress=lambda *args:check())
        elif source.suffix=='.json':pin(source)
