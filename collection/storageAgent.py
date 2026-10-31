"""Worker lifecycle for background raw delivery and season dataset publication."""
from storage.layout import full_path
from storage.sharedFiles import SharedFileUnavailable
from contextlib import closing
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from uuid import uuid4

from filelock import FileLock,Timeout
from storage.pandasStore import PandasStore, SCHEMA, atomic_json,atomic_replace
from .paths import collection_name
from .storageConfig import path_for
from .storageTransfer import (Outbox, Peer, FORMAT, digest, identifier, safe_path,
                              validate_manifest, link_or_copy)
from .workerRuntime import now


def season_for(job):
    import re
    from .selection import season_label
    return re.sub('[^a-z0-9]+','-',job.competition.lower()).strip('-')+'/'+season_label(job.season)


def season_files(root,season,match_id='*',*,snapshot=None):
    """Only active checkpoint generations; no machine state or retired caches."""
    root=Path(root);directory=root/season;files={'.soccer-scraper.json':root/'.soccer-scraper.json'}
    def pin(relative,path):
        if snapshot is None:return path
        from .maintenance import require_space
        target=Path(snapshot)/relative
        require_space(Path(snapshot),4096)
        if not target.exists():link_or_copy(path,target)
        return target
    for path in directory.glob('*'):
        if path.is_file() and path.suffix in ('.csv','.json'):files[path.relative_to(root).as_posix()]=path
    stores=directory/'match_checkpoints'
    markers=sorted(stores.glob('*/CURRENT.json')) if match_id=='*' else [stores/str(match_id)/'CURRENT.json']
    for marker in markers:
        if not marker.is_file():continue
        with FileLock(str(marker.parent/'.checkpoint.lock')):
            store=PandasStore(marker.parent);folder=store.current_directory
            for path in [marker,*[folder/(name+'.csv') for name in SCHEMA]]:files[path.relative_to(root).as_posix()]=pin(path.relative_to(root).as_posix(),path)
            if match_id!='*':
                with (folder/'matches.csv').open(encoding='utf-8',newline='') as stream:
                    for row in csv.DictReader(stream):
                        metadata=json.loads(row['metadata'])
                        def paths(value):
                            if isinstance(value,dict):
                                for item in value.values():yield from paths(item)
                            elif isinstance(value,list):
                                for item in value:yield from paths(item)
                            elif isinstance(value,str):yield value
                        for relative in paths({k:metadata.get(k,{}) for k in ('raw_response_files','sofascore_spatial_files')}):
                            relative=relative.replace('\\','/');target=season+'/'+relative;safe_path(target,'raw')
                            path=root/target
                            if path.is_file():files[target]=path
    if match_id=='*':
        for path in (directory/'raw').rglob('*'):
            if path.is_file() and path.suffix in ('.csv','.json'):files[path.relative_to(root).as_posix()]=path
    if len(files)==1:raise ValueError('No saved season files yet')
    if snapshot is not None:
        for relative,path in list(files.items()):
            if path.exists() and not path.is_relative_to(Path(snapshot)):files[relative]=pin(relative,path)
    return files


def dataset_files(root):
    files={}
    for path in Path(root).rglob('*'):
        if not path.is_file():continue
        relative=path.relative_to(root).as_posix()
        try:safe_path(relative,'dataset')
        except ValueError:continue
        if path.is_symlink():raise ValueError('Dataset contains symbolic links')
        files[relative]=path
    return files


def install_dataset(peer,manifest,output,staging_root,*,exclude=()):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with FileLock(str(output/'.dataset-install.lock'),timeout=25):
        return _install_dataset(peer,manifest,output,staging_root,exclude=exclude)


def _install_dataset(peer,manifest,output,staging_root,*,exclude=()):
    """Verify an entire publication before publishing any file; preserve other seasons."""
    validate_manifest(manifest)
    if manifest['kind']!='dataset':raise ValueError('Raw data cannot be installed as a dataset')
    files=[item for item in manifest['files'] if item['path'] not in exclude or item['path'].startswith('concordances/')]
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    staging_root=Path(staging_root);staging_root.mkdir(parents=True,exist_ok=True)
    from .ensemble import atomic_copy
    from .maintenance import require_space
    require_space(staging_root,2*sum(item['size'] for item in files))
    require_space(output,sum(item['size'] for item in files))
    with tempfile.TemporaryDirectory(prefix='dataset-',dir=staging_root) as temporary:
        stage=Path(temporary)
        for item in files:
            relative=item['path'];target=output/relative
            if any(p.is_symlink() for p in [target,*list(target.parents)[:len(target.relative_to(output).parts)]]):raise ValueError('Dataset destination contains symbolic links')
            incoming=stage/relative;incoming.parent.mkdir(parents=True,exist_ok=True)
            if target.is_file() and target.stat().st_size==item['size'] and digest(target)==item['sha256']:link_or_copy(target,incoming)
            else:peer.download(item['sha256'],incoming)
            if incoming.stat().st_size!=item['size'] or digest(incoming)!=item['sha256']:raise ValueError('Dataset file differs from manifest')
        parquet=stage/'concordances'
        if (parquet/'manifest.json').is_file():
            from concordance.parquet import validate
            validate(parquet)
        # The receipt is updated only after every publication file is installed.
        # An interrupted installation is retried from the same verified manifest.
        for item in files:
            if not item['path'].startswith('concordances/'):atomic_copy(stage/item['path'],output/item['path'])
        if (parquet/'manifest.json').is_file():
            previous=output/('.concordances-previous-'+uuid4().hex);target=output/'concordances'
            if target.exists():atomic_replace(target,previous)
            try:atomic_replace(parquet,target)
            except BaseException:
                if previous.exists():atomic_replace(previous,target)
                raise
            if previous.exists():shutil.rmtree(previous)
            from concordance.parquet import read_registry
            atomic_json(output/'fixture_concordance/alex_registry.json',read_registry(target),sort_keys=True)
        bundle=stage/'concordance/bundle.json'
        if bundle.is_file() and json.loads(bundle.read_text(encoding='utf-8')).get('format')=='soccer-concordance-bundle-v3':
            (output/'concordance/scraped/matches.csv').unlink(missing_ok=True)
    return identifier(manifest)


class StorageAgent:
    def __init__(self,root,worker,settings,*,dataset_root,log=print,ensemble=None):
        self.ensemble=Path(ensemble) if ensemble is not None else None
        self.root=full_path(root);self.worker=worker;self.settings=settings;self.output=Path(dataset_root);self.log=log
        self.directory=self.root/'_system/storage';self.directory.mkdir(parents=True,exist_ok=True)
        self.database=self.directory/'tasks.sqlite';self.outbox=Outbox(root,worker)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS captures(season TEXT,match_id TEXT,completed INTEGER,revision TEXT,PRIMARY KEY(season,match_id))');db.commit()
        self.stop=threading.Event();self.busy=threading.Event();self.server=None;self.options=None;self.last_error=None;self.removals_applied=False;self.archive_removals_applied=False
        self.status={'state':'setup_pending','message':'Waiting for storage setup from Main'}
        self.thread=threading.Thread(target=self.run,daemon=True,name='storage-transfer')
        self.maintenance_thread=threading.Thread(target=self.maintain,daemon=True,name='storage-maintenance')
        self.maintenance_status={'state':'waiting'}
        self.portable_status={'state':'waiting'}
        self.portable_thread=threading.Thread(target=self.import_portable,daemon=True,name='storage-portable-import')
        self.maintenance_changed=threading.Event()
        receipt=self.directory/'dataset-receipt.json'
        self.receipt=json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}

    def connect(self):return closing(sqlite3.connect(self.database,timeout=30))
    def start(self):
        self.thread.start();self.maintenance_thread.start()
        if self.ensemble is not None:self.portable_thread.start()
        return self
    def close(self):
        self.stop.set()
        if self.thread.is_alive():self.thread.join(timeout=35)
        if self.maintenance_thread.is_alive():self.maintenance_thread.join(timeout=15)
        if self.portable_thread.is_alive():self.portable_thread.join(timeout=15)
        if self.server:self.server.close()

    def capture(self,season,match_id='*',completed=False):
        from .removedCollections import removed_seasons
        if season in removed_seasons():return
        safe_path(season+'/job.json','raw')
        if match_id!='*' and (not str(match_id).isdigit()):raise ValueError('Invalid checkpoint match ID')
        with self.connect() as db:
            db.execute('INSERT INTO captures VALUES(?,?,?,?) ON CONFLICT(season,match_id) DO UPDATE SET completed=max(completed,excluded.completed),revision=excluded.revision',
                       (season,str(match_id),int(completed),uuid4().hex));db.commit()

    def finished(self,job,code):
        if code not in (0,2):return
        season=season_for(job)
        if self.worker==self.settings['hub']:
            if code==0:
                from .storageServer import Archive
                Archive(self.root).schedule_build(season)
        elif self.worker not in self.settings['consumers']:self.capture(season,completed=code==0)

    def backfill(self,jobs,history):
        path=self.directory/'backfilled.json'
        previous=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        for job in jobs:
            saved=history.get(job.id,{})
            if saved.get('state')!='finished' or previous.get(job.id)==saved:continue
            self.finished(job,0);previous[job.id]=saved
        atomic_json(path,previous)

    def tick(self):
        path=path_for(self.root)
        if not path.exists():return
        options=json.loads(path.read_text(encoding='utf-8'))
        if options['worker_id']!=self.worker or options['hub']!=self.settings['hub'] or options['url']!=self.settings['url']:
            raise ValueError('Storage setup does not match shared plan')
        self.options=options;role=options['role']
        if not self.removals_applied:
            from .removedCollections import apply_removals
            apply_removals(self.root,dataset=self.output,stop=self.stop,report=self.log)
            self.removals_applied=True
        if role=='hub':
            if self.server is None:
                from .storageServer import StorageServer
                self.server=StorageServer(self.root,options).start()
                self.log('[Storage] MSI archive receiver listening; raw files remain on MSI.')
            self.status={'state':'listening','role':role,**self.server.archive.status(),'maintenance':self.maintenance_status,'portable_import':self.portable_status}
            return
        peer=Peer(options['url'],options['token'])
        if role=='producer':
            try:
                with FileLock(str(self.directory/'producer.lock'),timeout=0):self._produce(peer)
            except Timeout:
                self.status={'state':'sending_elsewhere','role':'producer','queued':self.outbox.queued()}
        else:
            manifest=peer.request('/v1/dataset')['manifest']
            if manifest and manifest.get('worker')!=self.settings['hub']:raise ValueError('Unexpected dataset publisher')
            if manifest and identifier(manifest)!=self.receipt.get('id'):
                self.status={'state':'downloading','role':role}
                from .compiledDelivery import receipt_path
                from .storageTransfer import FIXTURE_DATASET_PATHS
                # Direct worker fixture publications supersede an MSI copy.
                excluded=FIXTURE_DATASET_PATHS if receipt_path(self.root).exists() else ()
                mid=install_dataset(peer,manifest,self.output,self.directory/'downloads',exclude=excluded)
                from fixtures.identityRefresh import reconcile_exports
                reconcile_exports(self.root,self.output)
                self.receipt={'id':mid,'received_at':now()};atomic_json(self.directory/'dataset-receipt.json',self.receipt)
                self.log('[Storage] Updated datasets and concordance received from MSI.')
            self.status={'state':'up_to_date' if manifest else 'awaiting_dataset','role':role,'last_dataset':self.receipt.get('received_at')}

    def _produce(self,peer):
        role='producer'
        if self.outbox.queued():
            self.outbox.send_one(peer,stop=self.stop)
            with self.connect() as db:pending=db.execute('SELECT count(*) FROM captures').fetchone()[0]
            self.status={'state':'sending','role':role,'queued':self.outbox.queued()+pending,'last_received':now()}
            return
        with self.connect() as db:task=db.execute('SELECT season,match_id,completed,revision FROM captures ORDER BY rowid LIMIT 1').fetchone()
        if task:
            season,match_id,completed,revision=task
            from .removedCollections import removed_seasons
            if season in removed_seasons():
                with self.connect() as db:db.execute('DELETE FROM captures WHERE season=? AND match_id=? AND revision=?',(season,match_id,revision));db.commit()
                self.status={'state':'sending','role':role,'queued':1}
                return
            from .workerIdentity import read_origins
            origins=read_origins(self.root)
            origins={k:v for k,v in origins.items() if '|'+season.replace('/','|')+'|' in k}
            self.outbox.enqueue(season_files(self.root,season,match_id),kind='raw',season=season,completed=bool(completed),origins=origins)
            with self.connect() as db:db.execute('DELETE FROM captures WHERE season=? AND match_id=? AND completed=? AND revision=?',task);db.commit()
        sent=self.outbox.send_one(peer,stop=self.stop)
        with self.connect() as db:pending=db.execute('SELECT count(*) FROM captures').fetchone()[0]
        self.status={'state':'sending' if self.outbox.queued()+pending else 'up_to_date','role':role,'queued':self.outbox.queued()+pending,
                     **({'last_received':now()} if sent else {'last_received':self.status.get('last_received')})}

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick();self.last_error=None
            except Exception as exc:
                # No URL/token/path in public runtime; console logging goes through reporting redaction.
                from .maintenance import LowSpace
                self.status={'state':'waiting_disk_space' if isinstance(exc,LowSpace) else 'retrying','role':(self.options or {}).get('role'),'queued':self.outbox.queued(),
                             'message':'Storage unavailable; saved files remain queued'}
                key=(type(exc).__name__,str(exc))
                if key!=self.last_error:self.log(f'[Storage] {type(exc).__name__}: {exc}');self.last_error=key
            self.stop.wait(1 if self.status.get('queued',0) else 30)

    def boundary(self,on_status=None):
        """Compatibility poll only; maintenance never runs on the scraper loop."""
        changed=self.maintenance_changed.is_set()
        self.maintenance_changed.clear()
        return changed

    def maintain(self):
        from .maintenance import DeferredWork,LowSpace
        from .inventoryCache import InventoryInterrupted
        from filelock import Timeout
        while not self.stop.is_set():
            try:
                changed=self.maintenance_once()
                if changed:self.maintenance_changed.set()
            except LowSpace as exc:
                self.maintenance_status={'state':'waiting_disk_space','message':str(exc)}
                self.stop.wait(30)
            except SharedFileUnavailable:
                self.maintenance_status={'state':'waiting','message':'A shared local file is busy; imports retained; retrying'}
            except (DeferredWork,Timeout,InventoryInterrupted):
                self.maintenance_status={'state':'waiting','message':'Background work yielded; scraping has priority'}
            except Exception as exc:
                self.maintenance_status={'state':'needs_attention','message':'Background work failed; source retained; retry in five minutes'}
                self.log(f'[Storage ERROR] {type(exc).__name__}: {exc}')
                self.stop.wait(300)
            self.stop.wait(10)

    def import_portable(self):
        """Bulk verification must never block the uploaded-match/build queue."""
        from .maintenance import DeferredWork,LowSpace
        from .inventoryCache import InventoryInterrupted
        from filelock import Timeout
        while not self.stop.is_set():
            delay=10
            try:
                if self.portable_once():self.maintenance_changed.set()
            except LowSpace as exc:
                self.portable_status={'state':'waiting_disk_space','message':str(exc)};delay=30
            except SharedFileUnavailable:
                self.portable_status={'state':'waiting','message':'A shared local file is busy; portable source retained; retrying'}
            except (DeferredWork,Timeout,InventoryInterrupted):
                self.portable_status={'state':'waiting','message':'Portable import yielded; scraping has priority'}
            except Exception as exc:
                self.portable_status={'state':'needs_attention','message':'Portable import failed; source retained; retry in five minutes'}
                self.log(f'[Storage ERROR] Portable import {type(exc).__name__}: {exc}');delay=300
            self.stop.wait(delay)

    def portable_once(self):
        from .backgroundImports import import_staged
        # Hub imports must have a receiver ready to persist dataset build jobs.
        if self.ensemble is None or self.worker!=self.settings['hub'] or self.server is None:return False
        self.portable_status={'state':'checking_staged_imports'}
        changed=import_staged(self.root,self.ensemble,stop=self.stop,report=self.log_report,
                              on_season=self.server.archive.schedule_build,on_progress=self.portable_progress)
        if self.portable_status.get('state')!='waiting':self.portable_status={'state':'current'}
        return changed

    def portable_progress(self,status):
        self.portable_status=status
        if status['state'] in ('verifying','verifying_final'):
            self.log(f"[Storage] Portable {status['source']} · {status['state'].replace('_',' ')}: {status['verified']}/{status['total']} files")
        elif status['state']=='importing':
            self.log(f"[Storage] Portable {status['source']} · importing {status['season']}")

    def maintenance_once(self):
        """One independent, resumable maintenance pass; useful for offline tests."""
        from .maintenance import background, Checkpoint, require_space, DeferredWork, LowSpace
        from .inventoryCache import InventoryInterrupted
        from filelock import Timeout
        from .backgroundImports import import_parts, snapshot_data
        from dataset_builder import build_dataset
        check=Checkpoint(self.stop);check();changed=False
        if not self.server:return changed
        archive=self.server.archive
        if not self.archive_removals_applied:
            from .removedCollections import apply_removals
            apply_removals(self.root,dataset=self.output,archive=archive,stop=self.stop,report=self.log)
            self.archive_removals_applied=True
        for mid,manifest in archive.completed():
            check()
            self.maintenance_status={'state':'incorporating','season':manifest['season']}
            self.log('[Storage] Background incorporation: '+manifest['season'])
            try:
                with background(self.root,manifest['season'],self.stop) as permit:
                    files={item['path']:archive.objects/item['sha256'] for item in manifest['files']}
                    import_parts(self.root,files,owner=manifest['worker'],batch=mid,
                                 origins=manifest.get('origins',{}),check=permit,report=self.log_report)
                    archive.incorporated(mid,manifest);changed=True
            except LowSpace:raise
            except (DeferredWork,Timeout,InventoryInterrupted):
                continue
            except (ValueError,KeyError,TypeError,csv.Error) as exc:
                # Invalid source data is isolated by upload. Capacity, locks,
                # cancellation and filesystem failures remain resumable errors.
                archive.quarantine(mid,exc)
                changed=True
                self.log(f'[Storage] Set aside upload {mid} ({manifest["season"]}) for review: {type(exc).__name__}: {exc}')
                continue
        for season,build_revision in archive.build_jobs():
            check()
            self.maintenance_status={'state':'building_dataset','season':season}
            self.log('[Storage] Background dataset build: '+season)
            try:
                if not (self.root/season/'job.json').is_file():
                    raise ValueError('No saved combined job; provider-only metadata is not a combined season export')
                self.output.parent.mkdir(parents=True,exist_ok=True)
                # Pin saved generations into an isolated input folder, so the
                # export command's collector lock belongs only to that snapshot.
                with tempfile.TemporaryDirectory(prefix='b-',dir=self.directory) as temporary:
                    source=Path(temporary)/'s';output=Path(temporary)/'d';output.mkdir()
                    snapshot_data(self.root,source,check=check)
                    require_space(self.directory,sum(path.stat().st_size for path in (source/season).rglob('*') if path.is_file())*3)
                    if (self.output/season).exists():shutil.copytree(self.output/season,output/season)
                    if (self.output/'fixtures.csv').is_file():shutil.copyfile(self.output/'fixtures.csv',output/'fixtures.csv')
                    if build_dataset(source,output,names=[collection_name(source/season)]):raise ValueError('Season dataset build failed')
                    # New statistics must not leave an older ID bundle labelled
                    # ready for R. Publish the invalidation with the new tables.
                    old_bundle=self.output/'concordance/bundle.json'
                    if old_bundle.is_file():
                        target=output/'concordance/bundle.json';target.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(old_bundle,target)
                        from concordance.build import invalidate_bundle
                        invalidate_bundle(output,'New statistics exported; re-audit integration and rebuild concordances')
                    public_manifest=self.output/'concordances/manifest.json'
                    if public_manifest.is_file():
                        destination=output/'concordances/manifest.json';destination.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(public_manifest,destination)
                        from concordance.build import invalidate_bundle
                        invalidate_bundle(output,'New statistics exported; re-audit integration and rebuild concordances')
                    check()
                    updates=dataset_files(output);files=dataset_files(self.output);files.update(updates)
                    manifest={'format':FORMAT,'kind':'dataset','worker':self.worker,'files':[
                        {'path':p,'size':v.stat().st_size,'sha256':digest(v)} for p,v in sorted(files.items())]}
                    archive.publish(manifest,files,check=check)
                    from .ensemble import atomic_copy
                    for relative,path in updates.items():
                        check();require_space(self.output,path.stat().st_size)
                        atomic_copy(path,self.output/relative)
                archive.published(season,build_revision)
                self.log('[Storage] Dataset published for Main: '+season)
                # Recheck new raw arrivals between expensive season exports.
                # An initial backlog must not monopolize incorporation for hours.
                changed=True
                break
            except (ValueError,KeyError,TypeError,csv.Error) as exc:
                archive.block_build(season,build_revision,exc)
                self.log(f'[Storage] Set aside dataset build {season}: {type(exc).__name__}: {exc}; continuing other seasons.')
                continue
            except Exception:
                # Import receipts have already been committed and must remain
                # observable even if a separate dataset build needs attention.
                if changed:self.maintenance_changed.set()
                raise
        archive.collected()
        counts=archive.status();held=counts['quarantined'];blocked=counts['blocked_builds']
        self.maintenance_status=({'state':'review_needed','message':f'{held} upload(s) and {blocked} dataset build(s) set aside; other work continues'}
                                 if held or blocked else {'state':'current'})
        return changed

    def log_report(self,message,**kwargs):self.log(message)
