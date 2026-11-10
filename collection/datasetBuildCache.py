"""Retain machine-local packaging state across isolated MSI snapshot builds."""
from pathlib import Path
import shutil
import re
from uuid import uuid4

from storage.pandasStore import atomic_replace
from .storageTransfer import link_or_copy


def seed(live, output, season=None, *, check=lambda: None, archive=None, repair=False):
    live, output = Path(live), Path(output)
    def copy(source, target):
        check()
        return link_or_copy(source,target)
    raw=live/'raw_data'/season if season is not None else live/'raw_data'
    destination=output/'raw_data'/season if season is not None else output/'raw_data'
    if raw.is_dir():shutil.copytree(raw,destination,copy_function=copy)
    # Seed previous layouts without keeping their paths in the next public bundle.
    for path in live.glob('*/*/*.csv'):
        if (re.fullmatch(r'\d{4}-\d{4}',path.parent.name) and path.parent.parent.name not in ('raw_data','concordance','concordances')
                and path.name in ('fixtures.csv','player_match_stats.csv','team_match_stats.csv')):
            target=output/'raw_data'/path.relative_to(live)
            if not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True);copy(path,target)
    if (live/'concordance').is_dir():
        # Archived old datasets are not needed to compute a new publication.
        shutil.copytree(live/'concordance',output/'concordance',copy_function=copy,
                        ignore=lambda directory,names: ['legacy'] if Path(directory).name=='metadata' and 'legacy' in names else [])
    for name in ('fixtures.csv','player_ids.csv','team_ids.csv','match_ids.csv','coverage.json','dataset-manifest.json','read_dataset.R','read_concordances.R','README.md','id-resolution.json'):
        if (live/name).is_file():copy(live/name,output/name)
    if (live/'concordances').is_dir():
        shutil.copytree(live/'concordances',output/'concordances',copy_function=copy)
    if (live/'fixture_concordance').is_dir():
        shutil.copytree(live/'fixture_concordance',output/'fixture_concordance',copy_function=copy)
    # The archive is the immutable, acknowledged publication. A partially
    # installed live tree must not seed a manifest without its public tables.
    if archive is not None:
        manifest=archive.publication()
        if manifest and any(f['path']=='concordance/metadata/manifest.json' for f in manifest['files']):
            from .storageTransfer import safe_path,digest
            for item in manifest['files']:
                relative=item['path']
                if not relative.startswith('concordance/'):continue
                safe_path(relative,'dataset');check()
                source=archive.objects/item['sha256'];target=output/relative
                if target.is_file() and target.stat().st_size==item['size'] and digest(target)==item['sha256']:
                    continue  # Retain timestamps of an unchanged, valid live publication.
                if not source.is_file() or source.stat().st_size!=item['size'] or digest(source)!=item['sha256']:
                    raise ValueError('Archived publication is incomplete or corrupt: '+relative)
                target.unlink(missing_ok=True);copy(source,target)
    from concordance.publication import exists,folder,validate
    if exists(output):
        try:validate(folder(output))
        except (OSError,ValueError):
            # Repair only the isolated snapshot, using retained compiler inputs.
            # The live publication remains untouched until the replacement passes.
            if not repair or not (output/'concordance/metadata/compiled/fixtures.csv').is_file():raise
            base=folder(output)
            for path in base.rglob('*'):
                relative=path.relative_to(base).parts
                private=len(relative)>1 and relative[0]=='metadata' and relative[1] in ('compiled','legacy','fixture_baselines')
                if path.is_file() and path.suffix in ('.csv','.parquet') and not private:path.unlink()
            (base/'metadata/manifest.json').unlink(missing_ok=True)


def commit(source, output, live, *, publication_id=None, check=lambda: None):
    """Commit private caches only after publication and public copy-back succeed."""
    from concordance import package_state as tracking
    from concordance.layout import compiled_root
    from collection.paths import collection_directory,discover_collections
    from dataset_pipeline.build import _build_settings
    source,output,live=Path(source),Path(output),Path(live)
    state=tracking.load(output/'concordance/metadata/package-state.json')
    if not state:return
    state['publication']=tracking.publication_outputs(live)
    state['archive_publication']=publication_id
    for name,previous in state['seasons'].items():
        directory=collection_directory(source,name)
        if (directory/'config.json').is_file() and directory.relative_to(source).as_posix() in state.get('source_inputs',{}):
            previous['outputs']=tracking.season_outputs(directory,live,tuple(previous['outputs']))
    state['global']=tracking.global_signature(source,compiled_root(output),
        discover_collections(source,'config.json'),_build_settings()[1],state['recipe'])
    target=compiled_root(live);target.parent.mkdir(parents=True,exist_ok=True)
    stage=target.with_name('.compiled-'+uuid4().hex)
    previous=target.with_name('.compiled-previous-'+uuid4().hex)
    def copy(incoming,outgoing):
        check()
        return link_or_copy(incoming,outgoing)
    installed=False
    try:
        shutil.copytree(compiled_root(output),stage,copy_function=copy)
        check()
        if target.exists():atomic_replace(target,previous)
        try:atomic_replace(stage,target)
        except BaseException:
            if previous.exists():atomic_replace(previous,target)
            raise
        installed=True
        # An interruption before this final checkpoint causes a safe retry.
        check()
        tracking.save(live/'concordance/metadata/package-state.json',state)
    finally:
        if stage.exists():shutil.rmtree(stage)
        if installed and previous.exists():shutil.rmtree(previous)
