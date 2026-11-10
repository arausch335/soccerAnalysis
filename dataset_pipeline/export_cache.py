"""Private completed-season exports survive failed or interrupted publication."""
from pathlib import Path,PurePosixPath
import json
import shutil
import tempfile
from uuid import uuid4

from concordance import package_state as tracking
from collection.storageTransfer import digest,link_or_copy
from storage.layout import full_path
from storage.pandasStore import atomic_json,atomic_replace

FORMAT='soccer-season-export-cache-v1'
SOURCE_FILES=('config.json','schedule.json','identities.csv','player_match_stats.csv',
              'team_match_statistics.csv','fotmob_team_statistics.csv','collection.csv','data_quality.csv')
DATASET_FILES=('fixtures.csv','player_match_stats.csv','team_match_stats.csv')


def key(root,source,recipe,tables):
    return tracking.digest({'source':str(tracking.logical_root(Path(source)).resolve()),
        'inputs':tracking.source_signature(source),'identity':tracking.identity_context(root),
        'recipe':recipe,'cache_recipe':digest(Path(__file__)),'tables':list(tables)})


def location(root,source,signature):
    origin=full_path(tracking.logical_root(Path(root).resolve()))
    collection=tracking.digest(str(tracking.logical_root(Path(source)).resolve()))[:20]
    return origin/'_system/cache/dataset-exports'/collection/signature


def _valid_path(name):
    path=PurePosixPath(name)
    return (path.as_posix()==name and len(path.parts)==2 and path.parts[0] in ('source','dataset') and
            path.name in (SOURCE_FILES if path.parts[0]=='source' else DATASET_FILES))


def restore(root,source,output,signature,*,tables):
    from dataset_pipeline.build import SEASON_TABLES
    base=location(root,source,signature)
    try:
        manifest=json.loads((base/'manifest.json').read_text())
        if manifest.get('format')!=FORMAT or manifest.get('signature')!=signature:return False
        paths=[r['path'] for r in manifest['files']]
        if len(paths)!=len(set(paths)) or not all(_valid_path(p) for p in paths):return False
        if not {'dataset/'+SEASON_TABLES[t][1] for t in tables}<=set(paths):return False
        for item in manifest['files']:
            path=base/item['path']
            if path.is_symlink() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:return False
    except (OSError,ValueError,KeyError,TypeError):return False
    for item in manifest['files']:
        area,name=PurePosixPath(item['path']).parts
        target=(Path(source) if area=='source' else Path(output))/name
        # The snapshot may be hard linked to live input/output files. Unlink the
        # snapshot destination before replacement; never write through that link.
        target.parent.mkdir(parents=True,exist_ok=True);target.unlink(missing_ok=True)
        link_or_copy(base/item['path'],target)
    return True


def save(root,source,output,signature,*,tables):
    from dataset_pipeline.build import SEASON_TABLES
    base=location(root,source,signature);base.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.season-export-',dir=base.parent) as temporary:
        stage=Path(temporary)/'cache';stage.mkdir();files=[]
        sources=[('source/'+name,Path(source)/name) for name in SOURCE_FILES if (Path(source)/name).is_file()]
        sources += [('dataset/'+SEASON_TABLES[table][1],Path(output)/SEASON_TABLES[table][1]) for table in tables]
        for relative,path in sources:
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);link_or_copy(path,target)
            files.append({'path':relative,'size':target.stat().st_size,'sha256':digest(target)})
        atomic_json(stage/'manifest.json',{'format':FORMAT,'signature':signature,'files':files})
        previous=base.with_name('.previous-'+uuid4().hex)
        if base.exists():atomic_replace(base,previous)
        try:atomic_replace(stage,base)
        except BaseException:
            if previous.exists():atomic_replace(previous,base)
            raise
        if previous.exists():shutil.rmtree(previous)
    # Retain recent input generations without removing the currently selected one.
    try:
        versions=sorted((p for p in base.parent.iterdir() if (p/'manifest.json').is_file()),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for old in versions[3:]:
            if old!=base:shutil.rmtree(old)
    except OSError:pass
