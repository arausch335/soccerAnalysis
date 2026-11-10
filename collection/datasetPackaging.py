"""Explicit --package: immutable inputs, incremental build, review, then publish."""
from pathlib import Path
import tempfile
import shutil
import json

from filelock import FileLock
from .storageTransfer import FORMAT,digest,identifier,link_or_copy


def package_saved(root,output,names=None,*,references_only=False,mode='incremental',concordance_format=None,report=print):
    from .workerIdentity import read_worker_id
    from .backgroundImports import snapshot_data
    from .datasetBuildCache import seed,commit
    from .storageAgent import dataset_files,install_dataset
    from concordance import package_state as tracking
    from dataset_pipeline.build import build_dataset
    from dataset_pipeline.review import create_review,print_review,load_review,print_package_diagnostic
    from dataset_pipeline.handoff import create as create_handoff,current as current_handoff
    from dataset_pipeline.identity import annotate
    root,output=Path(root).resolve(),Path(output).resolve()
    if root==output or root in output.parents or output in root.parents:raise ValueError('Data and dataset roots must be separate directories')
    directory=root/'_system/storage';directory.mkdir(parents=True,exist_ok=True)
    worker=read_worker_id(root);archive=None
    if worker=='msi':
        from .storageServer import Archive
        archive=Archive(root)
        archive.refresh_build_inputs()
    revisions=dict(archive.build_jobs()) if archive else {}
    with FileLock(str(directory/'package.lock'),timeout=0):
        with tempfile.TemporaryDirectory(prefix='p-',dir=directory) as temporary:
            source=Path(temporary)/'s';staged=Path(temporary)/'d';staged.mkdir()
            report('Preparing saved inputs for explicit dataset packaging…',flush=True)
            snapshot_data(root,source);seed(output,staged,archive=archive,repair=True)
            # Hard-link the previous season exports before touching candidates.
            # An ID-index failure can happen after an export has succeeded.
            previous=Path(temporary)/'previous'
            if (staged/'raw_data').is_dir():
                shutil.copytree(staged/'raw_data',previous,copy_function=link_or_copy)
            diagnostic={}
            def acknowledge_selected():
                if not archive or references_only:return
                selected={item['season_path'] for item in diagnostic['completed']}
                for season,revision in revisions.items():
                    if season in selected:archive.published(season,revision)
            state_path=staged/'concordance/metadata/package-state.json'
            before=state_path.read_bytes() if state_path.exists() else None
            with tracking.snapshot_inputs(root,source):
                code=build_dataset(source,staged,names,references_only=references_only,mode=mode,concordance_format=concordance_format,
                                   allow_partial=not references_only,build_report=diagnostic)
                if code:
                    report('Dataset package did not complete; no new dataset was installed. Completed season exports are cached for the next --package attempt.',flush=True)
                    print_package_diagnostic(diagnostic,report,published=False)
                    return code
                for item in [*diagnostic['failed'],*diagnostic['deferred']]:
                    # Match the export destination, including reviewed competition names.
                    from dataset_pipeline.build import _slug,_season_directory
                    saved=source/item['season_path']/'config.json'
                    try:
                        settings=json.loads(saved.read_text())
                        relative=Path(_slug(settings['competition']))/_season_directory(settings['season'])
                    except (OSError,ValueError,KeyError):relative=Path(item['season_path'])
                    target=staged/'raw_data'/relative;old=previous/relative
                    item['retained_previous']=old.is_dir()
                    if target.is_dir():shutil.rmtree(target)
                    if old.is_dir():shutil.copytree(old,target,copy_function=link_or_copy)
                deferred=diagnostic['deferred']
                diagnostic['status']='partial' if diagnostic['failed'] or deferred else 'complete'
                review=load_review(staged)
                current=archive.publication() if archive else None
                confirmed=archive is None or bool(current and tracking.load(state_path).get('archive_publication')==identifier(current))
                if before is not None and state_path.read_bytes()==before and review['state']=='ready' and confirmed and current_handoff(staged) and review.get('deferred_seasons',[])==deferred and (review.get('package_diagnostic') or {}).get('failed',[])==diagnostic['failed']:
                    print_review(review,report);report('Dataset is unchanged; existing package and publication retained.',flush=True)
                    acknowledge_selected()
                    print_package_diagnostic(diagnostic,report)
                    return 0
                metadata=Path(temporary)/'namespaces'
                _namespace_snapshot(source,staged,metadata,[*diagnostic['failed'],*deferred])
                identities=annotate(staged,metadata,report=report)
                report('Python canonical ID resolution: '+str(identities['summary']),flush=True)
                review=create_review(staged,deferred_seasons=deferred,package_diagnostic=diagnostic)
                handoff=create_handoff(staged,metadata)
                report('R handoff verified: copy only the dataset folder, then source its read_dataset.R. Archive integration audited: '+str(handoff['integration_confirmed']),flush=True)
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
                print_review(review,report)
                print_package_diagnostic(diagnostic,report)
                report('Dataset package complete. Coverage review: '+str(output/'coverage.json'),flush=True)
    return 0


def _namespace_snapshot(source,dataset,output,excluded):
    """Retained exports must use their published namespace, not broken new config."""
    ignored={row['season_path'] for row in excluded}
    for path in source.glob('*/*/config.json'):
        if path.parent.relative_to(source).as_posix() in ignored:continue
        target=output/path.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True)
        link_or_copy(path,target)
    manifest=dataset/'dataset-manifest.json'
    if not manifest.is_file():return
    previous=json.loads(manifest.read_text())
    for row in previous.get('tables',[]):
        relative=Path(row['path']).relative_to('raw_data').parent
        if relative.as_posix() not in ignored or not row.get('id_namespace'):continue
        target=output/relative/'config.json';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps({'competition':row['competition'],'season':row['season'],
                                      'id_namespace':row['id_namespace']}))
