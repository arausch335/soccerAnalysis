"""Shared concordance schemas and table projections."""
import json

import pandas as pd

CHUNK_ROWS=50_000
ID_COLUMNS=('entity_id','alex_id','reep_id','sofascore_id','fotmob_id','whoscored_id')
NAME_COLUMNS=('entity_name','reep_name','sofascore_name','fotmob_name','whoscored_name')
CORE_COLUMNS=('match_entity_id','date','kickoff_utc','season','competition','matchweek','round_name','status','awarded',
    'home_team_entity_id','home_team','away_team_entity_id','away_team','home_score','away_score','winner_code','has_player_stats')
EXTRA_COLUMNS=('match_entity_id','venue','city','competition_phase','competition_note','competition_type','competition_country','is_scheduled_for_scraping')
RENAMES={'match_entity_id':'entity_id','home_team_entity_id':'home_entity_id','away_team_entity_id':'away_entity_id'}
LOOKUP={**RENAMES,'source_home_team':'home_team','source_away_team':'away_team'}
TABLES=('fixtures/fixtures','fixtures/extra','fixtures/metadata','match_ids','team_ids','player_ids',
        'metadata/match_ids','metadata/team_ids','metadata/player_ids','metadata/registry','entities')


def text(value):
    if value is None or value is pd.NA:return ''
    if isinstance(value,(dict,list,tuple)):return json.dumps(value,ensure_ascii=False,sort_keys=True)
    if isinstance(value,float) and pd.isna(value):return ''
    return str(value)


def flag(value,*,nullable=False):
    value=text(value).lower()
    if not value:return None if nullable else False
    if value not in ('true','false'):raise ValueError('Invalid boolean fixture field: '+value)
    return value=='true'


def fixture_rows(base, read_table):
    """Reconstruct the internal linkage view without exposing it as the fixture table."""
    core=read_table(base,'fixtures/fixtures').rename(columns=RENAMES)
    extra=read_table(base,'fixtures/extra').rename(columns={'match_entity_id':'entity_id'})
    metadata=read_table(base,'fixtures/metadata').rename(columns={'match_entity_id':'entity_id'})
    result=core.merge(extra,on='entity_id',validate='one_to_one').merge(metadata,on='entity_id',validate='one_to_one')
    for side in ('home','away'):
        key='source_'+side+'_team'
        if key in result:result[side+'_team']=result.pop(key)
    for column in result:
        if column=='date':result[column]=result[column].map(lambda v:v.isoformat() if pd.notna(v) else '')
        elif column=='kickoff_utc':result[column]=result[column].map(lambda v:v.isoformat() if pd.notna(v) else '')
        else:result[column]=result[column].map(text)
    return result.to_dict('records')


def identity_rows(base, kind, read_table):
    ids=read_table(base,kind+'_ids');details=read_table(base,'metadata/'+kind+'_ids')
    return ids.merge(details,on='entity_id',how='left',validate='one_to_one').map(text).to_dict('records')
