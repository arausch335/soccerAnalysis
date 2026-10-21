"""Prepare a verified raw-data bundle for SD-card/ensemble import."""
from storage.layout import full_path
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from filelock import FileLock
from collection.storageAgent import season_files
from collection.storageTransfer import digest,safe_path,check_hash,link_or_copy
from collection.workerIdentity import read_worker_id
from storage.layout import system_path
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


def verify_bundle(root):
    root=full_path(root);manifest=json.loads((root/MANIFEST).read_text(encoding='utf-8'))
    if not isinstance(manifest,dict):raise ValueError('Invalid portable transfer manifest')
    files=manifest.get('files')
    if manifest.get('format')!=FORMAT or not isinstance(files,list) or not files or len(files)>500000:raise ValueError('Invalid portable transfer manifest')
    seen=set()
    for row in files:
        if not isinstance(row,dict) or not {'path','size','sha256'}<=row.keys() or type(row['size']) is not int or row['size']<0:raise ValueError('Invalid portable file entry')
        relative=row['path'];path=checked_path(root,relative)
        if relative in seen:raise ValueError('Duplicate portable transfer path')
        seen.add(relative);check_hash(row['sha256'])
        if not path.is_file() or path.stat().st_size!=row['size'] or digest(path)!=row['sha256']:raise ValueError(f'Portable transfer checksum differs: {relative}')
    if '.soccer-scraper.json' not in seen:raise ValueError('Portable transfer marker missing')
    # Every collection artifact must be covered, including additions made after export.
    for path in root.rglob('*'):
        if path.is_symlink():raise ValueError('Portable bundle contains a symbolic link')
        if not path.is_file() or path.relative_to(root)==MANIFEST or path.name=='.DS_Store' or path.name.startswith('._'):continue
        if path.relative_to(root).as_posix() not in seen:raise ValueError('Portable bundle contains an unverified extra file')
    return manifest


def prepare(root,output,report=print,*,session=None,max_bytes=None):
    root=full_path(root);output=Path(output).expanduser()
    if output.is_symlink():raise ValueError('Choose a regular output folder')
    output=full_path(output)
    if output.exists():raise ValueError('Output already exists; choose a new folder')
    if not output.parent.is_dir():raise ValueError('Output parent must exist; check that the SD card is mounted')
    if root==output or root in output.parents or output in root.parents:raise ValueError('Bundle and source data must be separate folders')
    if json.loads((root/'.soccer-scraper.json').read_text()).get('format')!=1:raise ValueError('Source must be a marked scraper data folder')
    workspace=root/'_system/storage';workspace.mkdir(parents=True,exist_ok=True)
    with FileLock(str(workspace/'portable-export.lock'),timeout=0), tempfile.TemporaryDirectory(prefix='x-',dir=workspace) as pinned:
        snapshot=Path(pinned)
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
        receipt_path=None;previous={}
        if session is not None:
            import re
            if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',session):raise ValueError('Use a simple transfer session name')
            receipt_path=root/'_system/storage/portable-exports'/(session+'.json')
            previous=json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        pending={key for key,row in rows_by_path.items() if previous.get(key)!=row['sha256']}
        if not pending:raise ValueError('All current files were already exported in this session; deliver its earlier bundles first')
        budget=max(0,shutil.disk_usage(output.parent).free-100*1024**2)
        if max_bytes is not None:budget=min(budget,max_bytes)
        selected=set(files)
        if session is not None or sum(row['size'] for row in rows_by_path.values())>budget:
            if session is None:raise OSError('Not enough free space for the full bundle; use --session main-to-msi to export in verified chunks')
            # A checkpoint and its referenced raw data travel together. Every
            # partial bundle is independently verifiable and importable.
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
            selected=set(common)
            size=sum(rows_by_path[key]['size'] for key in selected)
            if size>budget:raise OSError('Free space is too small even for bundle metadata; earlier bundles and all source data retained')
            for group in groups:
                if not group & pending:continue
                extra=group-selected;amount=sum(rows_by_path[key]['size'] for key in extra)
                if size+amount<=budget:selected.update(group);size+=amount
            if not (selected-common)&pending and pending-common:
                raise OSError('Free space is too small for the next complete checkpoint or raw file; all source data retained')
        remaining=pending-selected
        files={key:files[key] for key in sorted(selected)}
        with tempfile.TemporaryDirectory(prefix='soccer-transfer-',dir=output.parent) as temporary:
            stage=Path(temporary)/'data';stage.mkdir();rows=[]
            for index,(relative,source) in enumerate(sorted(files.items()),1):
                checked_path(root,relative)
                target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True)
                if shutil.disk_usage(output.parent).free<source.stat().st_size+100*1024**2:
                    raise OSError('Drive space changed during copying; source and earlier bundles retained')
                sha=rows_by_path[relative]['sha256'];shutil.copyfile(source,target)
                if digest(target)!=sha:raise OSError(f'Source changed during transfer: {relative}')
                rows.append({'path':relative,'size':target.stat().st_size,'sha256':sha})
                if index%1000==0:report(f'Copied and verified {index}/{len(files)} files',flush=True)
            manifest={'format':FORMAT,'worker':read_worker_id(root),'seasons':len(seasons),'files':rows,
                      'partial':bool(remaining),'remaining_files':len(remaining),'session':session}
            atomic_json(stage/MANIFEST,manifest);verify_bundle(stage)
            if output.exists():raise ValueError('Output appeared while copying; existing files preserved')
            os.rename(stage,output)
        if receipt_path is not None:
            previous.update({row['path']:row['sha256'] for row in rows});atomic_json(receipt_path,previous)
    report(f'Portable bundle ready: {output} ({len(seasons)} seasons, {len(rows)} verified files)',flush=True)
    if remaining:report(f'{len(remaining)} files remain. Deliver this bundle, then repeat with the same --session and a NEW --output folder.',flush=True)
    return output


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
    except (OSError,ValueError,KeyError) as exc:parser.exit(2,f'Transfer preparation failed: {exc}\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
