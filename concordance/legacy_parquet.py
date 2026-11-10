"""Read-only support for migrating older Parquet publications to the CSV layout."""
import json
import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from storage.checksums import file_digest as digest
from providers.reep import PROVIDERS

from .tables import (
    CHUNK_ROWS, CORE_COLUMNS, EXTRA_COLUMNS, ID_COLUMNS, LOOKUP, NAME_COLUMNS,
    RENAMES, TABLES, flag, fixture_rows, identity_rows, text,
)

FORMAT='soccer-concordances-parquet-v1'


def folder(dataset):return Path(dataset)/'concordances'


def exists(dataset):return (folder(dataset)/'manifest.json').is_file()


def read_table(base,name):
    manifest=json.loads((Path(base)/'manifest.json').read_text(encoding='utf-8'))
    files=manifest['tables'][name]['files']
    return pa.concat_tables([pq.read_table(Path(base)/item['path']) for item in files]).to_pandas()


def wide_rows(dataset):
    return fixture_rows(folder(dataset), read_table)


def reference_rows(dataset,kind):
    return identity_rows(folder(dataset), kind, read_table) if exists(dataset) else []


def read_registry(base):
    manifest=json.loads((Path(base)/'manifest.json').read_text(encoding='utf-8'))
    value={'format':manifest['registry_format'],'aliases':{},'published':{},'redirects':{},'anchors':{}}
    for row in read_table(base,'metadata/registry').to_dict('records'):
        value.setdefault(row['section'],{})[row['key']]=json.loads(row['value'])
    return value


def validate(base,*,require_integration=False):
    base=Path(base);manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=FORMAT:raise ValueError('Unknown Parquet concordance format')
    if require_integration and not manifest.get('ready_for_R'):raise ValueError('MSI integration is unconfirmed; audit and rebuild first')
    expected=set()
    for name,info in manifest['tables'].items():
        if name not in TABLES:raise ValueError('Unknown concordance table')
        count=0
        for item in info['files']:
            path=Path(item['path'])
            if path.parent.as_posix()!=name or not re.fullmatch(r'part-\d{5}\.parquet',path.name):raise ValueError('Unsafe Parquet shard path')
            target=base/path;expected.add(path.as_posix())
            if digest(target)!=item['sha256']:raise ValueError('Parquet checksum mismatch: '+str(path))
            count+=pq.read_metadata(target).num_rows
        if count!=info['rows']:raise ValueError('Parquet row count differs')
    if set(manifest['tables'])!=set(TABLES):raise ValueError('Incomplete Parquet table set')
    if {p.relative_to(base).as_posix() for p in base.rglob('*.parquet')}!=expected:raise ValueError('Stale or unverified Parquet shards')
    core=read_table(base,'fixtures/fixtures');extra=read_table(base,'fixtures/extra');meta=read_table(base,'fixtures/metadata')
    if tuple(core.columns)!=CORE_COLUMNS or tuple(extra.columns)!=EXTRA_COLUMNS:raise ValueError('Incorrect fixture schema')
    keys=set(core.match_entity_id)
    for frame in (core,extra,meta):
        if frame.match_entity_id.isna().any() or frame.match_entity_id.duplicated().any() or set(frame.match_entity_id)!=keys:raise ValueError('Fixture partitions do not have the same unique match keys')
    if (core.has_player_stats & extra.set_index('match_entity_id').loc[core.match_entity_id].is_scheduled_for_scraping.to_numpy()).any():raise ValueError('Scraped fixture cannot also be pending')
    from .registry import Registry
    registry=Registry(base/'unused-state',value=read_registry(base))
    mappings={}
    for kind in ('match','team','player'):
        frame=read_table(base,kind+'_ids')
        if tuple(frame.columns)!=(*ID_COLUMNS,*NAME_COLUMNS):raise ValueError('ID table must contain six IDs and five names')
        if frame.entity_id.isna().any() or frame.entity_id.duplicated().any():raise ValueError('Missing or duplicate concordance identity')
        canonical=frame.reep_id.fillna(frame.alex_id)
        if canonical.isna().any() or not frame.entity_id.equals(canonical):raise ValueError('Canonical ID differs from Reep/Alex identity')
        if frame[[p+'_id' for p in PROVIDERS]].isna().all(axis=1).any():raise ValueError('Identity has no real provider ID')
        for p in PROVIDERS:
            known=frame.dropna(subset=[p+'_id'])
            if known[p+'_id'].duplicated().any():raise ValueError('Provider ID maps to different entities')
        chosen=frame[[p+'_name' for p in ('reep',*PROVIDERS)]].bfill(axis=1).iloc[:,0]
        if frame.entity_name.fillna('').tolist()!=chosen.fillna('').tolist():raise ValueError('Entity name does not follow provider priority')
        registry.validate_rows(kind,[{**r,'safe_for_cross_provider_joins':True} for r in frame.map(text).to_dict('records')])
        mappings[kind]=set(frame.entity_id)
    if not keys<=mappings['match']:raise ValueError('Fixture match is absent from match_ids')
    for side in ('home','away'):
        if not set(core[side+'_team_entity_id'].dropna())<=mappings['team']:raise ValueError('Fixture team is absent from team_ids')
    for side in ('home','away'):
        missing=core[side+'_team_entity_id'].isna()
        if missing.any():
            facts=meta.set_index('match_entity_id').reindex(core.match_entity_id)
            allowed=core.has_player_stats.to_numpy() & facts.get('fixture_source',pd.Series('',index=facts.index)).eq('stored_player_stats').to_numpy()
            if (missing.to_numpy() & ~allowed).any():raise ValueError('Fixture is missing a team entity ID')
    return {'format':FORMAT,'rows':len(core),'tables':{k:v['rows'] for k,v in manifest['tables'].items()},'ready_for_R':manifest['ready_for_R']}

