"""Reviewed club profile groups, scoped to provider IDs rather than names.

The first ID is preferred for new references. Existing native IDs are retained.
"""
import hashlib
import json
from pathlib import Path

GROUPS=json.loads((Path(__file__).parent/'concordance/team_profiles.json').read_text())['groups']
_BY_ID={(provider,native):group for group in GROUPS for provider in ('sofascore','fotmob','whoscored') for native in group.get(provider,[])}
_BY_NAME={name.casefold():group['name'] for group in GROUPS for name in group.get('names',[group['name']])}


def _native(value):
    text='' if value is None else str(value)
    if text.casefold() in ('','nan','none','<na>'):return ''
    return text[:-2] if text.endswith('.0') and text[:-2].isdigit() else text


def canonical_team_id(provider,native):
    native=_native(native)
    group=_BY_ID.get((provider,native))
    return group[provider][0] if group else native


def reviewed_team(provider,native):
    return (provider,_native(native)) in _BY_ID


def canonical_team_name(provider,native,fallback):
    group=_BY_ID.get((provider,_native(native)))
    if group:return group['name']
    # Only explicitly approved display variants; no fuzzy/global city aliases.
    return _BY_NAME.get(str(fallback).casefold(),fallback)


def canonical_record(row,baseline='sofascore',*,reference=False):
    """Normalize project fields; provider-prefixed fixture IDs stay native."""
    row=dict(row)
    supplied=str(row.get('provider',''))
    provider=supplied if supplied in ('sofascore','fotmob','whoscored') else baseline
    for id_key,name_key in (('team_id','team'),('opponent_id','opponent'),('home_team_id','home_team'),('away_team_id','away_team')):
        native=row.get(id_key)
        if _native(native):
            name=row.get(name_key)
            if name is not None:row[name_key]=canonical_team_name(provider,native,name)
            canonical=canonical_team_id(provider,native)
            if canonical!=_native(native):row[id_key]=int(canonical) if isinstance(native,(int,float)) else canonical
    if reference:
        for p in ('sofascore','fotmob','whoscored'):
            native=row.get(p+'_id')
            if _native(native):
                if row.get('team_name') is not None:row['team_name']=canonical_team_name(p,native,row['team_name'])
                row[p+'_id']=canonical_team_id(p,native)
    for key in ('team_name','team','opponent','home_team','away_team','home_team_name','away_team_name'):
        if key in row:row[key]=canonical_team_name('',None,row[key])
    return row


def canonical_frame(frame,baseline='sofascore',*,reference=False):
    if frame.empty:return frame
    fields={'team_id','opponent_id','home_team_id','away_team_id','team_name','team','opponent','home_team','away_team','home_team_name','away_team_name'}
    if reference:fields.update({'sofascore_id','fotmob_id','whoscored_id'})
    tokens={native for _,native in _BY_ID}|set(_BY_NAME)
    if not any(tokens.intersection(_native(value).casefold() for value in frame[c].dropna().unique()) for c in fields.intersection(frame.columns)):
        return frame
    import pandas as pd
    return pd.DataFrame([canonical_record(row,baseline,reference=reference) for row in frame.to_dict('records')],columns=frame.columns)


def same_reviewed_team(left_provider,left,right_provider,right):
    group=_BY_ID.get((left_provider,_native(left)))
    return group is not None and group is _BY_ID.get((right_provider,_native(right)))


def version():
    return hashlib.sha256(json.dumps(GROUPS,sort_keys=True).encode()).hexdigest()
