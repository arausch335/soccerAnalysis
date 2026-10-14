"""Read-only consumer of collector exports and enriched events."""
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib,importlib.metadata,json
from pathlib import Path
import numpy as np
import pandas as pd
from .config import Config,FAMILIES,ZONES
from .events import aggregate_events
from .positions import position_usage
from .profiles import metric_registry,contributions,summarize
from .comparison import compare


def fingerprint(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    stat=path.stat();return {'path':str(path.resolve()),'bytes':stat.st_size,'modified_ns':stat.st_mtime_ns,'sha256':h.hexdigest()}


def run(project_root,data_dir,event_dir,output,config=None,competition='Premier League'):
    config=config or Config();project_root=Path(project_root);data_dir=Path(data_dir);event_dir=Path(event_dir);output=Path(output)
    paths=[data_dir/'player_match_stats.csv',data_dir/'identities.csv',event_dir/'events_enriched.csv',project_root/'analysis/features.py']
    before={str(p):(p.stat().st_size,p.stat().st_mtime_ns) for p in paths}
    print('Reading combined player-match features and identity bridge.',flush=True)
    raw=pd.read_csv(paths[0],low_memory=False)
    if raw.duplicated(['match_id','player_id']).any():raise ValueError('Duplicate canonical player-match rows')
    df=raw[raw.minutes_played.gt(0)].copy().reset_index(drop=True)
    df['competition']=competition
    identities=pd.read_csv(paths[1]);ids=identities[identities.provider.eq('whoscored')].copy()
    for col in ['match_id','player_id','provider_player_id','provider_match_id']:ids[col]=ids[col].astype('int64')
    ids=ids.drop_duplicates(['match_id','player_id','provider_player_id','provider_match_id'])
    if ids.duplicated(['match_id','player_id']).any() or ids.duplicated(['match_id','provider_player_id']).any():
        raise ValueError('Identity bridge is not one-to-one inside each match; resolve before role analysis')
    bridge=ids[['match_id','player_id','provider_player_id','provider_match_id','identity_method']]
    df=df.merge(bridge,on=['match_id','player_id'],how='left',validate='one_to_one')
    print('Resolving detailed positions from formation stints.',flush=True)
    pos,stints,slot_map=position_usage(data_dir/'raw/whoscored',ids)
    df=df.merge(pos,on=['match_id','provider_player_id'],how='left',validate='many_to_one')
    df['detailed_position']=df.detailed_position.fillna('Unknown')
    df['dominant_position_share']=df.dominant_position_share.fillna(0)
    df['comparison_position']=df.detailed_position.where(df.dominant_position_share.ge(config.min_position_share),'Mixed / uncertain')
    print('Aggregating event actions, regions and passing geometry.',flush=True)
    ev,zones,match_audit,event_specs,team_context,event_audit=aggregate_events(paths[2])
    unlinked=ev.merge(bridge,on=['match_id','provider_player_id'],how='left',validate='one_to_one')
    unlinked=unlinked[unlinked.player_id.isna()][['match_id','provider_player_id','ev_actions']]
    df=df.merge(ev,on=['match_id','provider_player_id'],how='left',validate='many_to_one')
    df=df.merge(match_audit,on='match_id',how='left',validate='many_to_one')
    df['event_covered']=df.provider_player_id.notna()&df.event_stream_usable.fillna(False).astype(bool)&df.provider_team_id.notna()
    ev_columns=[c for c in ev if c not in ['match_id','provider_player_id']]
    # Zero only when a full provider event stream and the mapped player roster exist.
    df[ev_columns]=df[ev_columns].where(df.event_covered,np.nan)
    df.loc[df.event_covered,ev_columns]=df.loc[df.event_covered,ev_columns].fillna(0)
    df=df.merge(team_context.rename(columns={'team_id':'provider_team_id'}),on=['match_id','provider_team_id'],how='left',validate='many_to_one')
    df=df.copy()
    df['team_pass_share_full_match_context']=df.ev_open_play_passes/df.team_open_play_passes.replace(0,np.nan)
    registry=metric_registry(project_root,df.columns,event_specs)
    num,den,covered,values=contributions(df,registry)
    # Join without shadowing raw provider values: only the derived output names are appended.
    match_output=df.drop(columns=[c for c in values if c in df],errors='ignore').join(values)
    print('Building season and position-conditioned player profiles.',flush=True)
    profiles,coverage,contrib=summarize(df,num,den,covered,config)
    print('Comparing within position; evaluating K-means, Ward and match-resampling stability.',flush=True)
    profiles,comparisons,models=compare(profiles,coverage,registry,contrib,config)
    metric_audit=[]
    for s in registry:
        f=s['feature'];totalmins=df.minutes_played.sum();cm=covered[f].sum()
        metric_audit.append({**s,'appearances_observed':int(covered[f].gt(0).sum()),'minutes_observed':cm,
             'minute_coverage':cm/totalmins,'nonmissing_match_values':int(values[f].notna().sum()),
             'availability':'unavailable in current export' if cm==0 else 'partial' if cm<totalmins else 'available'})
    event_compare=df.loc[df.event_covered,['match_id','player_id','player_name','passes_attempted','ev_passes','dribbles_attempted','ev_take_ons','shots','ev_shots']].copy()
    event_compare['pass_count_difference']=event_compare.ev_passes-event_compare.passes_attempted
    sources=[fingerprint(p) for p in paths]
    for p in paths:
        if before[str(p)]!=(p.stat().st_size,p.stat().st_mtime_ns):raise RuntimeError(f'Input changed during analysis: {p}. Rerun after the export completes.')
    expected=None
    schedule=data_dir/'schedule.json'
    if schedule.exists():
        sched=json.loads(schedule.read_text());events=sched.get('events',[]) if isinstance(sched,dict) else sched
        if isinstance(events,list):expected=len({e['id'] for e in events if isinstance(e,dict) and 'id' in e})
    summary={'created_at':datetime.now(timezone.utc).isoformat(),'config':asdict(config),'sources':sources,
      'competition':competition,'seasons':sorted(df.season.unique().tolist()),'observed_matches':int(df.match_id.nunique()),
      'scheduled_matches':expected,'season_complete':df.match_id.nunique()==expected if expected else False,
      'observed_date_from':str(df.date.min()),'observed_date_to':str(df.date.max()),
      'appearances':len(df),'excluded_unused_or_unknown_minutes':len(raw)-len(df),'unique_players':int(df.player_id.nunique()),
      'event_covered_appearances':int(df.event_covered.sum()),'identity_unmatched_appearances':int(df.provider_player_id.isna().sum()),
      'event_unlinked_player_matches':len(unlinked),'position_unresolved_appearances':int(df.detailed_position.eq('Unknown').sum()),
      'mixed_position_appearances':int(df.comparison_position.eq('Mixed / uncertain').sum()),
      'season_profiles':int(profiles.position_group.eq('All positions').sum()),
      'position_profiles':int(profiles.position_group.ne('All positions').sum()),
      'eligible_comparison_profiles':int(profiles.eligible_comparison.sum()),'modeled_profiles':int(profiles.model_eligible.sum()),
      'role_metrics':sum(s['scope']=='role' for s in registry),'performance_context_metrics':sum(s['scope']=='performance' for s in registry),
      'versions':{k:importlib.metadata.version(k) for k in ['numpy','pandas','scipy','scikit-learn']},**event_audit}
    output.mkdir(parents=True,exist_ok=True)
    tables={'player_match_roles':match_output,'player_season_roles':profiles[profiles.position_group.eq('All positions')],
       'player_position_roles':profiles[profiles.position_group.ne('All positions')],'season_metric_coverage':coverage,
       'metric_catalog':pd.DataFrame(metric_audit),'match_event_coverage':match_audit,'identity_unlinked_event_players':unlinked,
       'position_stints':stints,'formation_slot_dictionary':pd.DataFrame(slot_map),'provider_count_comparison':event_compare,
       'event_regions_player_match':zones.merge(bridge,on=['match_id','provider_player_id'],how='left',validate='many_to_one'),**comparisons}
    for name,table in tables.items():
        temporary=output/(name+'.csv.tmp');table.to_csv(temporary,index=False);temporary.replace(output/(name+'.csv'))
    (output/'manifest.json').write_text(json.dumps(summary,indent=2))
    (output/'models.json').write_text(json.dumps(models,indent=2,allow_nan=False))
    from .report import write_report
    write_report(output,summary,profiles,registry,comparisons,models,metric_audit,match_output)
    from .validate import validate
    validate(output)
    print(json.dumps(summary,indent=2),flush=True)
    return summary
