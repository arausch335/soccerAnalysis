"""Repair canonical IDs in an existing local dataset, independently of packaging."""
from contextlib import ExitStack
from pathlib import Path
import argparse
import tempfile

from filelock import FileLock,Timeout


def _namespace_inputs(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in sorted(root.glob('*/*/config.json'))}


def resolve_dataset(dataset_root, data_root=None, *, report=print):
    from concordance.publication import folder,validate
    from concordance import package_state as tracking
    from dataset_identity import annotate
    from dataset_review import create_review,load_review,print_review
    from dataset_handoff import current as handoff_current,create as create_handoff
    from collection.datasetBuildCache import seed
    from collection.storageAgent import dataset_files
    from collection.storageTransfer import digest,link_or_copy
    from collection.paths import collection_parts
    from dataset_builder import SEASON_TABLES
    from collection.ensemble import atomic_copy
    from storage.pandasStore import atomic_replace

    dataset=Path(dataset_root).expanduser().resolve()
    source_root=Path(data_root).expanduser().resolve() if data_root is not None else None
    if not dataset.is_dir():raise ValueError('No local dataset found; run python main.py --package first')
    if source_root is not None and (source_root==dataset or source_root in dataset.parents or dataset in source_root.parents):
        raise ValueError('Data and dataset roots must be separate directories')
    with ExitStack() as locks:
        # Match --package's lock order, and exclude Main's receiver while taking
        # and installing the dataset snapshot. No raw input files are modified.
        if source_root is not None and source_root.is_dir():
            storage=source_root/'_system/storage';storage.mkdir(parents=True,exist_ok=True)
            locks.enter_context(FileLock(str(storage/'package.lock'),timeout=0))
        locks.enter_context(FileLock(str(dataset/'.dataset-install.lock'),timeout=0))
        validate(folder(dataset))
        pinned=_namespace_inputs(source_root) if source_root is not None else {}
        with tempfile.TemporaryDirectory(prefix='.resolve-ids-',dir=dataset.parent) as temporary:
            temporary=Path(temporary);staged=temporary/'dataset';staged.mkdir()
            metadata=temporary/'source';metadata.mkdir()
            for name,content in pinned.items():
                path=metadata/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
            report('Resolving IDs in the existing local dataset…',flush=True)
            seed(dataset,staged)
            previous_review=load_review(staged)
            audit_path=staged/'id-resolution.json'
            before=audit_path.read_bytes() if audit_path.is_file() else None
            identities=annotate(staged,metadata,report=report)
            report('Python canonical ID resolution: '+str(identities['summary']),flush=True)
            if source_root is not None and pinned!=_namespace_inputs(source_root):
                raise ValueError('Saved namespace metadata changed during ID resolution; local dataset retained; retry')
            if (before is not None and audit_path.read_bytes()==before and previous_review['state']=='ready'
                    and handoff_current(staged)):
                print_review(previous_review,report)
                report('ID resolution complete. Dataset IDs and R handoff are already up to date.',flush=True)
                return 0
            review=create_review(staged,deferred_seasons=previous_review.get('deferred_seasons',()))
            create_handoff(staged,metadata)
            # The concordance itself is unchanged. Install only derived CSVs,
            # audits and R handoff files; retain original files for rollback.
            updates={name:path for name,path in dataset_files(staged).items() if not name.startswith('concordance/')}
            for name in updates:
                target=dataset/name
                if any(p.is_symlink() for p in (target,*list(target.parents)[:len(target.relative_to(dataset).parts)])):
                    raise ValueError('Dataset destination contains symbolic links')
            state_path=dataset/'concordance/metadata/package-state.json'
            backups=[]
            def preserve(target):
                backup=None
                if target.exists():
                    backup=temporary/'previous'/target.relative_to(dataset)
                    backup.parent.mkdir(parents=True,exist_ok=True);link_or_copy(target,backup)
                backups.append((target,backup))
            try:
                for name,path in sorted(updates.items(),key=lambda item:(item[0]=='dataset-manifest.json',item[0])):
                    target=dataset/name
                    if target.is_file() and target.stat().st_size==path.stat().st_size and digest(target)==digest(path):continue
                    preserve(target);atomic_copy(path,target)
                state=tracking.load(state_path)
                if state:
                    for name,season in state['seasons'].items():
                        competition,year=collection_parts(name)
                        for table in season.get('outputs',{}):
                            if table in SEASON_TABLES:
                                season['outputs'][table]=tracking.file_stamp(dataset/'raw_data'/competition/year/SEASON_TABLES[table][1])
                    # The local repair has not been published. A later --package
                    # must publish it even when saved collection inputs are unchanged.
                    state['archive_publication']=None
                    preserve(state_path);tracking.save(state_path,state)
            except BaseException:
                for target,backup in reversed(backups):
                    if backup is None:target.unlink(missing_ok=True)
                    else:atomic_replace(backup,target)
                raise
            print_review(review,report)
            report('ID resolution complete. Updated local CSVs, id-resolution.json, coverage and R handoff.',flush=True)
            report('Run python main.py --package when you want MSI to publish the dataset for Main.',flush=True)
    return 0


def main(argv=None):
    import config
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-root',type=Path,default=config.DATASET_ROOT,help='Existing local dataset folder')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT,help='Saved metadata for native ID namespaces; raw collections are not rebuilt')
    args=parser.parse_args(argv)
    try:return resolve_dataset(args.dataset_root,args.data_root)
    except Timeout:parser.exit(2,'Dataset or package is busy. Wait for the other operation to finish, then retry.\n')
    except (OSError,ValueError,KeyError) as exc:parser.exit(2,'ID resolution failed: '+str(exc)+'\n')
