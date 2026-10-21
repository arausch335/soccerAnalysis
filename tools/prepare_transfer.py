"""Prepare a verified raw-data bundle for SD-card/ensemble import."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from filelock import FileLock
from collection.storageAgent import season_files
from collection.storageTransfer import digest,safe_path,check_hash
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
    root=Path(root).resolve();manifest=json.loads((root/MANIFEST).read_text(encoding='utf-8'))
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


def prepare(root,output,report=print):
    root=Path(root).expanduser().resolve();output=Path(output).expanduser()
    if output.is_symlink():raise ValueError('Choose a regular output folder')
    output=output.resolve()
    if output.exists():raise ValueError('Output already exists; choose a new folder')
    if not output.parent.is_dir():raise ValueError('Output parent must exist; check that the SD card is mounted')
    if root==output or root in output.parents or output in root.parents:raise ValueError('Bundle and source data must be separate folders')
    if json.loads((root/'.soccer-scraper.json').read_text()).get('format')!=1:raise ValueError('Source must be a marked scraper data folder')
    with FileLock(str(system_path(root,'.collector.lock')),timeout=0):
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
                for key,path in season_files(archive,relative).items():
                    if key!='.soccer-scraper.json':files[(prefix+'/' if prefix else '')+key]=path
                seasons.add(relative)
        if not seasons:raise ValueError('No saved collection seasons found')
        for relative in STATE:
            path=root/relative
            if path.exists():files[relative]=path
        size=sum(path.stat().st_size for path in files.values())
        if shutil.disk_usage(output.parent).free<size+100*1024**2:raise OSError('Not enough free space for the portable bundle')
        with tempfile.TemporaryDirectory(prefix='soccer-transfer-',dir=output.parent) as temporary:
            stage=Path(temporary)/'data';stage.mkdir();rows=[]
            for index,(relative,source) in enumerate(sorted(files.items()),1):
                checked_path(root,relative)
                target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True)
                sha=digest(source);shutil.copyfile(source,target)
                if digest(target)!=sha:raise OSError(f'Source changed during transfer: {relative}')
                rows.append({'path':relative,'size':target.stat().st_size,'sha256':sha})
                if index%1000==0:report(f'Copied and verified {index}/{len(files)} files',flush=True)
            manifest={'format':FORMAT,'worker':read_worker_id(root),'seasons':len(seasons),'files':rows}
            atomic_json(stage/MANIFEST,manifest);verify_bundle(stage)
            if output.exists():raise ValueError('Output appeared while copying; existing files preserved')
            os.rename(stage,output)
    report(f'Portable bundle ready: {output} ({len(seasons)} seasons, {len(rows)} verified files)',flush=True)
    return output


def main(argv=None):
    import config
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--verify',type=Path,help='Check a copied bundle before import')
    args=parser.parse_args(argv)
    try:
        if args.verify:
            value=verify_bundle(args.verify);print(f"Verified {len(value['files'])} files from {value['worker']}")
        elif args.output:prepare(args.data_root,args.output)
        else:parser.error('Choose --output or --verify')
    except (OSError,ValueError,KeyError) as exc:parser.exit(2,f'Transfer preparation failed: {exc}\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
