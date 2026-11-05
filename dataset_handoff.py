"""A self-contained, verified R handoff inside the dataset directory."""
from pathlib import Path
import csv
import json

from collection.storageTransfer import digest,safe_path
from storage.pandasStore import atomic_json

FORMAT='soccer-r-dataset-v1'
IDENTITY_PRODUCER=Path(__file__).with_name('dataset_identity.py')
ASSETS={'read_concordances.R':'analysis/read_concordances.R',
        'read_dataset.R':'analysis/read_dataset.R','README.md':'docs/R_DATASET.md'}


def _assets():
    root=Path(__file__).resolve().parent
    return {name:(root/source).read_bytes() for name,source in ASSETS.items()}


def current(dataset):
    """Cheap eligibility check; build checkpoints detect changed public outputs."""
    dataset=Path(dataset)
    try:
        value=json.loads((dataset/'dataset-manifest.json').read_text())
        if (value.get('format')!=FORMAT or value.get('producer_digest')!=digest(Path(__file__))
                or value.get('identity_producer_digest')!=digest(IDENTITY_PRODUCER)):return False
        if any(not (dataset/name).is_file() or (dataset/name).read_bytes()!=content for name,content in _assets().items()):return False
        identities=json.loads((dataset/'id-resolution.json').read_text())
        if identities.get('producer_digest')!=digest(IDENTITY_PRODUCER) or identities.get('concordance_digest')!=digest(dataset/'concordance/metadata/manifest.json'):return False
        known={row['path']:row for row in value['files']}
        for name in ('coverage.json','concordance/metadata/manifest.json'):
            if known[name]['sha256']!=digest(dataset/name):return False
        verify(dataset)
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def create(dataset,source_root=None):
    from concordance.publication import folder,validate,publication_files,read_table
    from dataset_review import load_review
    dataset=Path(dataset);base=folder(dataset);validation=validate(base)
    review=load_review(dataset)
    if review['state']!='ready':raise ValueError('A current coverage review is required for the R handoff')
    for name,content in _assets().items():
        # Assets can be hard linked to the previous package. Replace, never overwrite.
        target=dataset/name;temporary=target.with_suffix(target.suffix+'.tmp')
        temporary.write_bytes(content);temporary.replace(target)
    core=read_table(base,'fixtures/fixtures').fillna('')
    metadata=read_table(base,'fixtures/metadata').fillna('')
    scope={r['match_entity_id']:(r['competition'],r['season']) for r in core.to_dict('records')}
    namespaces={};competition_labels={}
    from dataset_builder import _slug
    for row in metadata.to_dict('records'):
        if row['match_entity_id'] not in scope:continue
        competition,season=scope[row['match_entity_id']]
        league=_slug(competition).replace('-qualification','-qualifiers')
        competition_labels[league,season]=competition
        if row.get('baseline_provider'):namespaces.setdefault((league,season),set()).add(row['baseline_provider'])
    tables=[]
    for item in review['files']:
        path=dataset/item['path']
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream);columns=reader.fieldnames;first=next(reader,{})
        if not columns or 'match_id' not in columns or len(columns)!=len(set(columns)):
            raise ValueError('Invalid R season table schema: '+item['path'])
        namespace=None;settings={}
        if source_root is not None:
            config=Path(source_root)/Path(item['path']).relative_to('raw_data').parent/'config.json'
            if config.is_file():
                settings=json.loads(config.read_text());namespace=settings.get('id_namespace',settings.get('baseline_provider','sofascore'))
        if not namespace:
            possible=namespaces.get((path.parent.parent.name,path.parent.name),set())
            if len(possible)==1:namespace=next(iter(possible))
        if namespace is not None and namespace not in ('sofascore','fotmob','whoscored'):
            raise ValueError('Unknown native ID namespace: '+str(namespace))
        tables.append({'path':item['path'],'table':item['table'],'rows':item['rows'],
                       'competition':first.get('competition') or settings.get('competition') or competition_labels.get((path.parent.parent.name,path.parent.name),path.parent.parent.name),
                       'season':first.get('season') or path.parent.name,'id_namespace':namespace,'columns':columns})
    paths=[*publication_files(base),*(dataset/item['path'] for item in tables),dataset/'coverage.json',*(dataset/name for name in ASSETS)]
    if (dataset/'id-resolution.json').is_file():paths.append(dataset/'id-resolution.json')
    files=[]
    for path in sorted(paths):
        relative=path.relative_to(dataset).as_posix();safe_path(relative,'dataset')
        if path.is_symlink():raise ValueError('R handoff cannot contain symbolic links')
        files.append({'path':relative,'size':path.stat().st_size,'sha256':digest(path)})
    manifest={'format':FORMAT,'producer_digest':digest(Path(__file__)),'identity_producer_digest':digest(IDENTITY_PRODUCER),'created_at':review['generated_at'],'structurally_valid':True,
              'integration_confirmed':validation['ready_for_R'],'files':files,'tables':tables,
              'r_packages':['arrow','jsonlite','digest'],'coverage':review['summary']}
    atomic_json(dataset/'dataset-manifest.json',manifest,indent=2)
    verify(dataset)
    return manifest


def verify(dataset):
    from concordance.publication import folder,validate
    dataset=Path(dataset);value=json.loads((dataset/'dataset-manifest.json').read_text())
    if value.get('format')!=FORMAT:raise ValueError('Unknown R dataset manifest')
    paths=set()
    for row in value['files']:
        relative=safe_path(row['path'],'dataset')
        if relative in paths:raise ValueError('Duplicate R handoff path')
        paths.add(relative);path=dataset/relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['size'] or digest(path)!=row['sha256']:
            raise ValueError('R dataset checksum/missing file: '+relative)
    actual={'raw_data/'+p.relative_to(dataset/'raw_data').as_posix() for p in (dataset/'raw_data').glob('*/*/*.csv')}
    expected={row['path'] for row in value['tables']}
    if expected!={p for p in paths if p.startswith('raw_data/')} or actual!=expected:
        raise ValueError('R dataset season inventory differs from manifest')
    for name in (*ASSETS,'coverage.json','concordance/metadata/manifest.json'):
        if name not in paths:raise ValueError('Incomplete R handoff: '+name)
    validation=validate(folder(dataset))
    if value['integration_confirmed']!=validation['ready_for_R']:raise ValueError('R integration status differs from concordance')
    return value
