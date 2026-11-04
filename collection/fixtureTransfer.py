"""Verified fixture-only ensemble transfers; no raw data or machine settings."""
from pathlib import Path
import csv
import json
import shutil
import tempfile

from filelock import FileLock
from storage.pandasStore import atomic_json, atomic_replace
from .storageTransfer import digest, identifier, safe_path

FORMAT='soccer-fixture-transfer-v1'
MARKER='fixture-transfer.json'


def verify(source):
    from concordance.publication import is_public_bundle,validate,folder
    source=Path(source)
    if source.is_symlink():raise ValueError('Fixture transfer contains symbolic links')
    value=json.loads((source/MARKER).read_text(encoding='utf-8'))
    if value.get('format')!=FORMAT:raise ValueError('Invalid fixture transfer format')
    rows=value.get('files',[]);paths=[r['path'] for r in rows]
    if len(paths)!=len(set(paths)) or not is_public_bundle(paths):
        raise ValueError('Fixture transfer must contain one complete public bundle')
    for item in rows:
        safe_path(item['path'],'dataset');path=source/item['path']
        if any(p.is_symlink() for p in [path,*list(path.parents)[:len(path.relative_to(source).parts)]]):
            raise ValueError('Fixture transfer contains symbolic links')
        if not path.is_file() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:
            raise ValueError('Fixture transfer checksum differs: '+item['path'])
    actual=set()
    for path in source.rglob('*'):
        if path.is_symlink():raise ValueError('Fixture transfer contains symbolic links')
        if path.is_file():actual.add(path.relative_to(source).as_posix())
    if actual!=set(paths)|{MARKER}:raise ValueError('Fixture transfer contains unverified extra files')
    result=validate(folder(source))
    if result['rows']!=value.get('fixtures'):raise ValueError('Fixture transfer row count differs')
    return value


def export_bundle(dataset,output):
    from concordance.publication import folder,validate,publication_files
    dataset,output=Path(dataset),Path(output)
    if output.exists():raise ValueError('Choose a new fixture transfer folder')
    if dataset.resolve()==output.resolve() or dataset.resolve() in output.resolve().parents:
        raise ValueError('Export outside the source dataset')
    result=validate(folder(dataset));output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.fixture-export-',dir=output.parent) as temporary:
        stage=Path(temporary)/'bundle';stage.mkdir();files=[]
        for source in publication_files(folder(dataset)):
            relative='concordance/'+source.relative_to(folder(dataset)).as_posix()
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            files.append({'path':relative,'size':target.stat().st_size,'sha256':digest(target)})
        atomic_json(stage/MARKER,{'format':FORMAT,'fixtures':result['rows'],'files':files})
        verify(stage);atomic_replace(stage,output)
    return result['rows']


def install_bundle(source,dataset):
    """Retain an immutable baseline; the next build merges it under its own lock."""
    source,dataset=Path(source),Path(dataset)
    value=verify(source);mid=identifier(value)
    target=dataset/'concordance/metadata/fixture_baselines'/mid
    dataset.mkdir(parents=True,exist_ok=True)
    with FileLock(str(dataset/'.dataset-install.lock'),timeout=25):
        if target.exists():verify(target);return False
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.fixture-install-',dir=target.parent) as temporary:
            stage=Path(temporary)/'bundle';shutil.copytree(source,stage)
            verify(stage);atomic_replace(stage,target)
    return True


def apply_baselines(root,dataset,*,report=print):
    """Merge baseline fixtures with saved local observations, preserving both."""
    import pandas as pd
    from concordance.layout import public_root,compiled_root
    from concordance.publication import wide_rows,reference_rows,read_registry,folder
    from fixtures.sharedIds import assign_ids
    from collection.files import atomic_csv
    root,public=Path(root),public_root(dataset);compiled=compiled_root(public)
    receipt=compiled/'fixture-baselines.json'
    done=json.loads(receipt.read_text()) if receipt.exists() else {}
    changed=False
    def read(path):
        if not path.is_file():return []
        with path.open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))
    def anchor(row):
        return next(((p,str(row[p+'_match_id'])) for p in ('sofascore','fotmob','whoscored') if row.get(p+'_match_id')),None)
    for marker in sorted((public/'concordance/metadata/fixture_baselines').glob('*/'+MARKER)):
        mid=marker.parent.name
        if mid in done:continue
        value=verify(marker.parent);incoming=wide_rows(marker.parent)
        rows=read(compiled/'fixtures.csv');known={anchor(r):r for r in rows}
        for row in incoming:
            key=anchor(row)
            if key is None:raise ValueError('Fixture baseline has no provider match ID')
            if key in known:
                old=known[key]
                for k,v in row.items():
                    if not old.get(k):old[k]=v
                old['has_player_stats']=any(str(r.get('has_player_stats','')).lower()=='true' for r in (old,row))
            else:rows.append(row);known[key]=row
        references={kind:read(compiled/(kind+'_ids.csv'))+reference_rows(marker.parent,kind)
                    for kind in ('match','team','player')}
        # assign_ids merges registry seeds with receiving authority. It never
        # replaces the receiver's reviewed identity database with sender state.
        with tempfile.TemporaryDirectory(prefix='.fixture-ids-',dir=compiled) as temporary:
            seed=Path(temporary)
            atomic_json(seed/'alex_registry.json',read_registry(folder(marker.parent)))
            assign_ids(root,rows,references['match'],references['team'],references['player'],folder=seed)
        merged={}
        for row in rows:
            key=row['entity_id']
            if key not in merged:merged[key]=row
            else:
                old=merged[key]
                for k,v in row.items():
                    if not old.get(k):old[k]=v
                old['has_player_stats']=any(str(r.get('has_player_stats','')).lower()=='true' for r in (old,row))
        rows=list(merged.values())
        if not {anchor(r) for r in incoming}<={anchor(r) for r in rows}:
            raise ValueError('Fixture baseline would lose provider match keys; review identities')
        atomic_csv(pd.DataFrame(rows),compiled/'fixtures.csv')
        for kind,items in references.items():
            unique={json.dumps(row,sort_keys=True):row for row in items}
            if unique:atomic_csv(pd.DataFrame(unique.values()),compiled/(kind+'_ids.csv'))
        done[mid]={'fixtures':value['fixtures']};atomic_json(receipt,done)
        changed=True;report(f'Fixture baseline merged: {value["fixtures"]} incoming; {len(rows)} retained fixtures',flush=True)
    return changed


def import_staged(ensemble,dataset,*,check=lambda:None,report=print):
    changed=False
    for marker in sorted(Path(ensemble).rglob(MARKER)):
        check();value=json.loads(marker.read_text());mid=identifier(value)
        if (Path(dataset)/'concordance/metadata/fixture_baselines'/mid/MARKER).is_file():continue
        if install_bundle(marker.parent,dataset):
            changed=True;report(f'[Storage] Fixture baseline received: {value["fixtures"]} fixtures; staged source retained',flush=True)
    return changed
