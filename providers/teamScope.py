"""Men's first-team identity scope; exclusions never apply to player IDs."""
import json
from pathlib import Path
import re

EXCLUSIONS=json.loads((Path(__file__).parent/'concordance/team_scope.json').read_text(encoding='utf-8'))['excluded_profiles']
_EXCLUDED={(r['provider'],r['id']) for r in EXCLUSIONS}


def in_mens_scope(provider,native,team=None):
    native=str(native)
    if native.endswith('.0') and native[:-2].isdigit():native=native[:-2]
    if (provider,native) in _EXCLUDED:return False
    team=team or {}
    gender=str(team.get('gender','')).casefold()
    if gender in ('f','female','women','woman') or team.get('isWomen') is True:return False
    return not re.search(r'(?:\(w\)|\bwomen(?:[’\']s)?\b|\bfeminin[eo]?\b)',str(team.get('name','')),re.I)


def clean_reference(row):
    """Remove an out-of-scope ID without discarding a valid men's team."""
    row=dict(row);removed=False
    if not in_mens_scope('',None,{**row,'name':row.get('team_name',row.get('name',''))}):
        for provider in ('sofascore','fotmob','whoscored'):row[provider+'_id']=''
        row['reep_id']='';row['identity_status']='excluded'
        return row
    for provider in ('sofascore','fotmob','whoscored'):
        key=provider+'_id'
        if not in_mens_scope(provider,row.get(key)):
            row[key]='';removed=True
    if removed:
        row['reep_id']='';row['identity_status']='conflict'
    return row


def keep_reference(row):
    cleaned=clean_reference(row)
    if not in_mens_scope('',None,{'name':row.get('team_name',''),'gender':row.get('gender','')}):return False
    return any(str(cleaned.get(p+'_id','')).casefold() not in ('','none','nan','<na>')
               for p in ('sofascore','fotmob','whoscored'))
