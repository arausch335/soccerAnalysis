"""Explicit --package: immutable inputs, incremental build, review, then publish."""
from pathlib import Path
import tempfile

from filelock import FileLock
from .storageTransfer import FORMAT,digest,identifier,link_or_copy


def package_saved(root,output,names=None,*,references_only=False,mode='incremental',concordance_format=None,report=print):
    from .workerIdentity import read_worker_id
    from .backgroundImports import snapshot_data
    from .datasetBuildCache import seed,commit
    from .storageAgent import dataset_files,install_dataset
    from .paths import discover_collections
    from concordance import package_state as tracking
    from dataset_builder import build_dataset
    from dataset_review import create_review,print_review,load_review
    root,output=Path(root).resolve(),Path(output).resolve()
    if root==output or root in output.parents or output in root.parents:raise ValueError('Data and dataset roots must be separate directories')
    directory=root/'_system/storage';directory.mkdir(parents=True,exist_ok=True)
    worker=read_worker_id(root);archive=None
    if worker=='msi':
        from .storageServer import Archive
        archive=Archive(root)
    revisions=dict(archive.build_jobs()) if archive else {}
    with FileLock(str(directory/'package.lock'),timeout=0):
        with tempfile.TemporaryDirectory(prefix='p-',dir=directory) as temporary:
            source=Path(temporary)/'s';staged=Path(temporary)/'d';staged.mkdir()
            report('Preparing saved inputs for explicit dataset packaging…',flush=True)
            snapshot_data(root,source);seed(output,staged,archive=archive)
            def acknowledge_selected():
                if not archive or references_only:return
                selected={p.relative_to(source).as_posix() for p in discover_collections(source,'job.json')}
                if names:
                    from .paths import collection_directory
                    selected={collection_directory(source,name).relative_to(source).as_posix() for name in names}
                for season,revision in revisions.items():
                    if season in selected:archive.published(season,revision)
            state_path=staged/'concordance/metadata/package-state.json'
            before=state_path.read_bytes() if state_path.exists() else None
            with tracking.snapshot_inputs(root,source):
                code=build_dataset(source,staged,names,references_only=references_only,mode=mode,concordance_format=concordance_format)
                if code:return code
                review=load_review(staged)
                current=archive.publication() if archive else None
                confirmed=archive is None or bool(current and tracking.load(state_path).get('archive_publication')==identifier(current))
                if before is not None and state_path.read_bytes()==before and review['state']=='ready' and confirmed:
                    print_review(review,report);report('Dataset is unchanged; existing package and publication retained.',flush=True)
                    acknowledge_selected()
                    return 0
                review=create_review(staged)
                print_review(review,report)
                files=dataset_files(staged)
                manifest={'format':FORMAT,'kind':'dataset','worker':worker,'files':[
                    {'path':name,'size':path.stat().st_size,'sha256':digest(path)} for name,path in sorted(files.items())]}
                by_hash={item['sha256']:files[item['path']] for item in manifest['files']}
                class Objects:
                    def download(self,sha,target):
                        link_or_copy(by_hash[sha],target)
                install_dataset(Objects(),manifest,output,Path(temporary)/'install')
                from concordance.layout import retire_legacy
                retire_legacy(output)
                mid=identifier(manifest)
                if archive:
                    archive.publish(manifest,files)
                    acknowledge_selected()
                    report('Dataset published for Main. Main will download it through its normal receiver.',flush=True)
                commit(source,staged,output,publication_id=mid if archive else None)
                report('Dataset package complete. Coverage review: '+str(output/'coverage.json'),flush=True)
    return 0
