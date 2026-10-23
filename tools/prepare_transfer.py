"""Prepare a verified raw-data bundle for SD-card/ensemble import."""
from storage.layout import full_path
import argparse
import json
import os
from pathlib import Path
import shutil
import hashlib
import math
from contextlib import contextmanager

from filelock import FileLock
from collection.storageAgent import season_files
from collection.storageTransfer import digest,safe_path,check_hash,link_or_copy
from collection.workerIdentity import read_worker_id
from collection.maintenance import LowSpace
from storage.pandasStore import atomic_json

FORMAT='soccer-portable-transfer-v1'
MANIFEST=Path('_system/transfer-manifest.json')
LEGACY={'fotmob_player_stats','whoscored_events','whoscored'}
STATE={'.soccer-scraper.json','_system/config/identity.json','_system/provenance.json'}


def checked_path(root,relative):
    if not isinstance(relative,str):raise ValueError('Invalid transfer path')
    if relative not in STATE:
        first,_,remainder=relative.partition('/')
        safe_path(remainder if first in LEGACY else relative,'raw')
    path=Path(root)/relative
    if path.is_symlink() or Path(root).resolve() not in path.resolve().parents:raise ValueError('Transfer path leaves the data folder')
    parent=path.parent
    while parent!=Path(root):
        if parent.is_symlink():raise ValueError('Symbolic links cannot be transferred')
        parent=parent.parent
    return path


def verify_bundle(root, *, check=lambda:None, progress=None):
    root=full_path(root);manifest=json.loads((root/MANIFEST).read_text(encoding='utf-8'))
    if not isinstance(manifest,dict):raise ValueError('Invalid portable transfer manifest')
    files=manifest.get('files')
    if manifest.get('format')!=FORMAT or not isinstance(files,list) or not files or len(files)>500000:raise ValueError('Invalid portable transfer manifest')
    seen=set();check()
    interval=max(1,len(files)//10)
    if progress:progress(0,len(files))
    for index,row in enumerate(files):
        if index%128==0:check()
        if not isinstance(row,dict) or not {'path','size','sha256'}<=row.keys() or type(row['size']) is not int or row['size']<0:raise ValueError('Invalid portable file entry')
        relative=row['path'];path=checked_path(root,relative)
        if relative in seen:raise ValueError('Duplicate portable transfer path')
        seen.add(relative);check_hash(row['sha256'])
        if not path.is_file() or path.stat().st_size!=row['size'] or digest(path)!=row['sha256']:raise ValueError(f'Portable transfer checksum differs: {relative}')
        if progress and ((index+1)%interval==0 or index+1==len(files)):progress(index+1,len(files))
    if '.soccer-scraper.json' not in seen:raise ValueError('Portable transfer marker missing')
    # Every collection artifact must be covered, including additions made after export.
    for index,path in enumerate(root.rglob('*')):
        if index%128==0:check()
        if path.is_symlink():raise ValueError('Portable bundle contains a symbolic link')
        if not path.is_file() or path.relative_to(root)==MANIFEST or path.name=='.DS_Store' or path.name.startswith('._'):continue
        if path.relative_to(root).as_posix() not in seen:raise ValueError('Portable bundle contains an unverified extra file')
    check()
    return manifest


RESERVE=8*1024**3


def remove_metadata_sidecar(path):
    """Remove only an AppleDouble companion, never a data file or a symlink."""
    sidecar=path.with_name('._'+path.name)
    if sidecar.is_symlink():return 0
    try:
        with sidecar.open('rb') as stream:
            if stream.read(4)!=b'\x00\x05\x16\x07':return 0
        sidecar.unlink()
        return 1
    except FileNotFoundError:return 0


def clean_transfer_metadata(stage,rows):
    """Restrict cleanup to companions of planned files and their directories."""
    directories=set();removed=0
    for row in rows:
        target=checked_path(stage,row['path'])
        removed+=remove_metadata_sidecar(target)
        parent=target.parent
        while parent!=stage.parent and parent not in directories:
            directories.add(parent);removed+=remove_metadata_sidecar(parent)
            parent=parent.parent
    return removed


def allocation_unit(path):
    """The destination's allocation unit, not the source's file byte count."""
    if hasattr(os,'statvfs'):
        info=os.statvfs(path)
        return max(4096,info.f_frsize or info.f_bsize)
    # Windows (including external exFAT cards and network drive letters).
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetVolumePathNameW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]
    kernel.GetVolumePathNameW.restype=wintypes.BOOL
    kernel.GetDiskFreeSpaceW.argtypes=[wintypes.LPCWSTR,*([ctypes.POINTER(wintypes.DWORD)]*4)]
    kernel.GetDiskFreeSpaceW.restype=wintypes.BOOL
    volume=ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(path),volume,len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    sectors=wintypes.DWORD();size=wintypes.DWORD();free=wintypes.DWORD();total=wintypes.DWORD()
    if not kernel.GetDiskFreeSpaceW(volume,ctypes.byref(sectors),ctypes.byref(size),ctypes.byref(free),ctypes.byref(total)):
        raise ctypes.WinError(ctypes.get_last_error())
    return max(4096,sectors.value*size.value)


class Footprint:
    """Incremental estimate: rounded files, directories and directory entries."""
    def __init__(self,rows,unit):
        self.rows=rows;self.unit=unit;self.paths=set();self.directories=set()
        self.payload=0;self.allocated=0
    def extra(self,paths):
        paths=set(paths)-self.paths;directories=set();payload=allocated=0
        for relative in paths:
            size=self.rows[relative]['size'];payload+=size
            # macOS can create a 4-KiB AppleDouble file for each entry on exFAT;
            # that companion occupies a full allocation unit as well.
            allocated+=max(1,(size+self.unit-1)//self.unit)*self.unit+self.unit+1024
            for parent in Path(relative).parents:
                if parent!=Path('.') and parent.as_posix() not in self.directories:directories.add(parent.as_posix())
        allocated+=len(directories)*(2*self.unit+1024)
        return payload,allocated,directories,paths
    def add(self,paths):
        payload,allocated,directories,paths=self.extra(paths)
        self.payload+=payload;self.allocated+=allocated
        self.directories.update(directories);self.paths.update(paths)
    def fits(self,paths,disk_budget,payload_budget):
        payload,allocated,_,_=self.extra(paths)
        return self.allocated+allocated<=disk_budget and self.payload+payload<=payload_budget


def manifest_space(manifest,unit):
    return math.ceil(len(json.dumps(manifest,ensure_ascii=False).encode())/unit)*unit+4*unit+4096


def artifact_groups(files):
    """A checkpoint and its referenced raw files must travel in the same bundle."""
    common={key for key in files if key in STATE or
            ('raw' not in Path(key).parts and 'match_checkpoints' not in Path(key).parts)}
    groups=[];assigned=set()
    for key in sorted(files):
        if not key.endswith('/CURRENT.json'):continue
        parent=key.rsplit('/',1)[0];generation=json.loads(files[key].read_text())['generation']
        from storage.pandasStore import SCHEMA
        members={key}|{parent+'/snapshots/'+generation+'/'+name+'.csv' for name in SCHEMA}
        matches=files[parent+'/snapshots/'+generation+'/matches.csv']
        import csv
        with matches.open(encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                metadata=json.loads(row['metadata'])
                def paths(value):
                    if isinstance(value,dict):
                        for item in value.values():yield from paths(item)
                    elif isinstance(value,list):
                        for item in value:yield from paths(item)
                    elif isinstance(value,str):yield value.replace('\\','/')
                season=parent.split('/match_checkpoints/')[0]
                for value in paths({k:metadata.get(k,{}) for k in ('raw_response_files','sofascore_spatial_files')}):
                    if season+'/'+value in files:members.add(season+'/'+value)
        groups.append(members);assigned.update(members)
    groups.extend([{key} for key in sorted(set(files)-assigned-common)])
    return common,groups


def export_paths(root,output):
    key=hashlib.sha256(str(output).encode()).hexdigest()[:24]
    local=root/'_system/storage/portable-exports/in-progress'/key
    return local,output.with_name('.'+output.name+'.incomplete')


@contextmanager
def saved_source(local):
    try:yield
    finally:
        # Initial planning can fail without starting a card copy. Only plans
        # that have actually started need to keep their pinned generations.
        if local.exists() and not (local/'plan.json').exists():shutil.rmtree(local)


def complete_copy(root,output,local,incomplete,plan,report):
    manifest=plan['manifest'];rows=manifest['files'];stage=incomplete/'data'
    # A retained plan may predate the larger reserve. Do not silently continue
    # filling the card using its old, smaller limit.
    reserve=max(RESERVE,plan['reserve'])
    control=incomplete/'plan-id.json'
    expected=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    if incomplete.exists():
        if incomplete.is_symlink() or stage.is_symlink():raise ValueError('Incomplete output contains a symbolic link')
        if not control.is_file() or json.loads(control.read_text()).get('id')!=expected:
            if any(incomplete.iterdir()):raise ValueError('Incomplete output belongs to a different transfer; preserved for review')
            atomic_json(control,{'id':expected})
    if output.exists():
        # Recover an interruption after publishing but before saving the receipt.
        if verify_bundle(output)!=manifest:raise ValueError('Existing output differs from the interrupted transfer')
    else:
        if not incomplete.exists():
            incomplete.mkdir();atomic_json(control,{'id':expected})
        retained=stage.exists();stage.mkdir(exist_ok=True)
        if retained:report('Checking macOS metadata companions in the temporary transfer folder; saved collection files are retained.',flush=True)
        removed=clean_transfer_metadata(stage,rows) if retained else 0
        removed+=remove_metadata_sidecar(control)
        if removed:report(f'Removed {removed:,} AppleDouble metadata companions from the temporary transfer.',flush=True)
        leftover=incomplete/'copy.part'
        if leftover.is_symlink():raise ValueError('Incomplete copy contains a symbolic link')
        leftover.unlink(missing_ok=True)
        remove_metadata_sidecar(leftover)
        copied=resumed=0;unit=allocation_unit(output.parent)
        report(f"Destination allocation unit: {unit:,} bytes; estimated bundle allocation: {plan['allocated']:,} bytes; free-space reserve: {reserve/1024**3:g} GiB. Incomplete copies are retained for resume.",flush=True)
        if retained:
            # Check remaining capacity before spending an hour verifying/copying
            # an older plan that selected too much for the new reserve.
            pending=Footprint({row['path']:row for row in rows},unit);missing=[];checked_directories=set()
            for row in rows:
                target=checked_path(stage,row['path'])
                if not target.is_file() or target.stat().st_size!=row['size']:missing.append(row['path'])
                parent=target.parent
                while parent!=stage and parent not in checked_directories:
                    checked_directories.add(parent)
                    if parent.exists():pending.directories.add(parent.relative_to(stage).as_posix())
                    parent=parent.parent
            pending.add(missing)
            shortage=reserve+pending.allocated+manifest_space(manifest,unit)-shutil.disk_usage(output.parent).free
            if shortage>0:
                raise OSError(f'Saved bundle needs about {shortage/1024**3:.2f} GiB more free space to keep {reserve/1024**3:g} GiB free. No collection files were copied or removed; partial copy retained at {incomplete}. Free space and rerun the SAME command')
        for index,row in enumerate(rows,1):
            relative=row['path'];target=checked_path(stage,relative);sha=row['sha256']
            # An existing verified file is already durable on the card; never
            # recopy it just because the previous process was interrupted.
            if target.is_file() and target.stat().st_size==row['size'] and digest(target)==sha:
                resumed+=1
            else:
                incoming=checked_path(local/'source',relative)
                if not incoming.is_file() or incoming.stat().st_size!=row['size'] or digest(incoming)!=sha:
                    raise ValueError(f'Pinned source differs; partial copy retained: {relative}')
                need=max(1,(row['size']+unit-1)//unit)*unit+unit+1024
                new_parents=[]
                parent=target.parent
                while parent!=stage and not parent.exists():
                    new_parents.append(parent);need+=2*unit+1024;parent=parent.parent
                if shutil.disk_usage(output.parent).free<need+reserve:
                    raise OSError(f'Drive reserve reached ({reserve/1024**3:g} GiB kept free). Partial copy retained at {incomplete}. Free space and rerun the SAME command to resume')
                target.parent.mkdir(parents=True,exist_ok=True)
                for parent in new_parents:remove_metadata_sidecar(parent)
                temporary=incomplete/'copy.part'
                if temporary.is_symlink():raise ValueError('Incomplete copy contains a symbolic link')
                try:
                    shutil.copyfile(incoming,temporary)
                    if temporary.stat().st_size!=row['size'] or digest(temporary)!=sha:
                        raise OSError(f'Copy checksum differs: {relative}')
                    os.replace(temporary,target)
                    remove_metadata_sidecar(target);remove_metadata_sidecar(temporary)
                    copied+=1
                except OSError as exc:
                    raise OSError(f'Copy interrupted; verified files retained at {incomplete}. Rerun the SAME command to resume: {exc}') from exc
                finally:
                    try:temporary.unlink(missing_ok=True)
                    except OSError:pass  # Unplugged media is cleaned on the next resume.
            if index%1000==0:report(f'Verified {index}/{len(rows)} files ({resumed} reused; {copied} copied)',flush=True)
        manifest_need=manifest_space(manifest,unit)
        if shutil.disk_usage(output.parent).free<reserve+manifest_need:
            raise OSError(f'Drive reserve reached before publishing ({reserve/1024**3:g} GiB kept free). Partial copy retained at {incomplete}; rerun the SAME command after freeing space')
        atomic_json(stage/MANIFEST,manifest)
        remove_metadata_sidecar(stage/MANIFEST)
        verify_bundle(stage)
        if output.exists():raise ValueError('Output appeared during copy; incomplete copy retained')
        os.rename(stage,output)
    # Receipt only after the complete selected bundle is verified and published.
    session=manifest.get('session')
    if session:
        receipt=root/'_system/storage/portable-exports'/(session+'.json')
        previous=json.loads(receipt.read_text()) if receipt.exists() else {}
        previous.update({row['path']:row['sha256'] for row in rows});atomic_json(receipt,previous)
    # Cleanup only after success. A failure leaves both verified card files and
    # pinned old checkpoint generations available for the next identical run.
    shutil.rmtree(incomplete,ignore_errors=True)
    shutil.rmtree(local)
    report(f"Portable bundle ready: {output} ({manifest['seasons']} seasons, {len(rows)} verified files)",flush=True)
    if manifest['remaining_files']:
        report(f"{manifest['remaining_files']} files remain. Deliver this bundle, then repeat with the same --session and a NEW --output folder.",flush=True)
    return output


def prepare(root,output,report=print,*,session=None,max_bytes=None):
    root=full_path(root);output=Path(output).expanduser()
    if output.is_symlink():raise ValueError('Choose a regular output folder')
    output=full_path(output)
    if not output.parent.is_dir():raise ValueError('Output parent must exist; check that the SD card is mounted')
    if root==output or root in output.parents or output in root.parents:raise ValueError('Bundle and source data must be separate folders')
    if json.loads((root/'.soccer-scraper.json').read_text()).get('format')!=1:raise ValueError('Source must be a marked scraper data folder')
    if max_bytes is not None and (not isinstance(max_bytes,int) or max_bytes<=0):raise ValueError('Bundle byte limit must be positive')
    if session is not None:
        import re
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',session):raise ValueError('Use a simple transfer session name')
    workspace=root/'_system/storage';workspace.mkdir(parents=True,exist_ok=True)
    local,incomplete=export_paths(root,output)
    with FileLock(str(workspace/'portable-export.lock'),timeout=0), saved_source(local):
        plan_path=local/'plan.json'
        if plan_path.exists():
            plan=json.loads(plan_path.read_text())
            if plan['manifest'].get('session')!=session or plan.get('max_bytes')!=max_bytes:
                raise ValueError('Resume using the same --session and --max-gib as the interrupted copy')
            report('Resuming saved transfer plan; completed files will be checked and reused.',flush=True)
            return complete_copy(root,output,local,incomplete,plan,report)
        if output.exists():raise ValueError('Output already exists; choose a new folder')
        if incomplete.exists():raise ValueError('An unrecognized incomplete folder exists; preserved for review')
        # No copy has started without a saved plan. An abandoned initial snapshot
        # is disposable; every pin still has its original collector file.
        if local.exists():shutil.rmtree(local)
        snapshot=local/'source';snapshot.mkdir(parents=True)
        files={};seasons=set()
        for prefix in ('',*sorted(LEGACY)):
            archive=root/prefix
            for directory in sorted(archive.glob('*/*')):
                if not directory.is_dir() or directory.parent.name=='_system':continue
                relative=directory.relative_to(archive).as_posix()
                try:safe_path(relative+'/job.json','raw')
                except ValueError:continue
                if directory.is_symlink() or directory.parent.is_symlink():raise ValueError('Season folder is a symbolic link')
                if not any((directory/name).exists() for name in ('raw','match_checkpoints','job.json','whoscored_job.json')):continue
                for key,path in season_files(archive,relative,snapshot=snapshot/prefix).items():
                    if key!='.soccer-scraper.json':files[(prefix+'/' if prefix else '')+key]=path
                seasons.add(relative)
        if not seasons:raise ValueError('No saved collection seasons found')
        for relative in STATE:
            path=root/relative
            if path.exists():
                target=snapshot/relative
                if not target.exists():link_or_copy(path,target)
                files[relative]=target
        rows_by_path={}
        for relative,path in sorted(files.items()):
            checked_path(root,relative)
            rows_by_path[relative]={'path':relative,'size':path.stat().st_size,'sha256':digest(path)}

        receipt_path=root/'_system/storage/portable-exports'/(session+'.json') if session else None
        previous=json.loads(receipt_path.read_text()) if receipt_path and receipt_path.exists() else {}
        pending={key for key,row in rows_by_path.items() if previous.get(key)!=row['sha256']}
        if not pending:raise ValueError('All current files were already exported in this session; deliver its earlier bundles first')
        unit=allocation_unit(output.parent);free=shutil.disk_usage(output.parent).free
        # Leave room for the bundle manifest, filesystem metadata and unrelated
        # writes. Each file and each directory is also estimated independently.
        reserve=max(RESERVE,math.ceil(free*.02))
        # Reserve the final manifest separately; it also needs allocated blocks
        # after a chunk has filled its planned data budget.
        disk_budget=max(0,free-reserve-manifest_space({'files':list(rows_by_path.values())},unit)-4096)
        payload_budget=max_bytes if max_bytes is not None else float('inf')
        footprint=Footprint(rows_by_path,unit)
        if session is None:
            if not footprint.fits(files,disk_budget,payload_budget):
                raise OSError('Not enough allocated drive space for the full bundle; use --session main-to-msi for verified chunks')
            footprint.add(files)
        else:
            common,groups=artifact_groups(files)
            if not footprint.fits(common,disk_budget,payload_budget):
                raise OSError('Free space is too small even for bundle metadata; source data retained')
            footprint.add(common)
            for group in groups:
                if group & pending and footprint.fits(group,disk_budget,payload_budget):footprint.add(group)
            if not (footprint.paths-common)&pending and pending-common:
                raise OSError('Free space is too small for the next complete checkpoint or raw file; source data retained')
        remaining=pending-footprint.paths
        manifest={'format':FORMAT,'worker':read_worker_id(root),'seasons':len(seasons),
                  'files':[rows_by_path[key] for key in sorted(footprint.paths)],
                  'partial':bool(remaining),'remaining_files':len(remaining),'session':session}
        plan={'manifest':manifest,'allocated':footprint.allocated,'reserve':reserve,'max_bytes':max_bytes}
        atomic_json(plan_path,plan)
        return complete_copy(root,output,local,incomplete,plan,report)


def main(argv=None):
    import config
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--session',help='Remember exported files; split into verified bundles sized to free drive space')
    parser.add_argument('--max-gib',type=float,help='Optional per-bundle size cap, in GiB')
    parser.add_argument('--verify',type=Path,help='Check a copied bundle before import')
    args=parser.parse_args(argv)
    try:
        if args.verify:
            value=verify_bundle(args.verify);print(f"Verified {len(value['files'])} files from {value['worker']}")
        elif args.output:
            if args.max_gib is not None and args.max_gib<=0:raise ValueError('--max-gib must be positive')
            prepare(args.data_root,args.output,session=args.session,max_bytes=int(args.max_gib*1024**3) if args.max_gib else None)
        else:parser.error('Choose --output or --verify')
    except (OSError,ValueError,KeyError,LowSpace) as exc:parser.exit(2,f'Transfer preparation failed: {exc}\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
