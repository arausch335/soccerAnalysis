"""Coverage-aware match and season aggregation with explicit denominators."""
import importlib.util
import json
import numpy as np
import pandas as pd
from .config import BASE_CANDIDATES,EXTRA_CONTEXT,HEAT_CONTEXT,ZONES,FAMILIES

KEYS=['competition','season','player_id']

def metric_registry(project_root,columns,event_specs):
    path=project_root/'features.py';spec=importlib.util.spec_from_file_location('project_role_contract',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    registry=[];seen=set()
    for category,metrics in module.ROLE.items():
        for name in metrics:
            if name in seen:continue
            seen.add(name)
            registry.append(dict(name=name,kind='count',group=BASE_CANDIDATES.get(name,{'dribbling':'carrying','shooting':'attacking','defense':'defending'}.get(category,category)),candidate=name in BASE_CANDIDATES,
               label=name.replace('_',' ').capitalize()+' / 90',source='combined player-match statistics',scope='role'))
    for name in EXTRA_CONTEXT:
        if name not in seen:
            registry.append(dict(name=name,kind='count',group='physical context',candidate=False,label=name.replace('_',' '),source='combined statistics; units require verification',scope='role'));seen.add(name)
    # Execution measures are published as context, never role-model features.
    performance=set()
    for category,metrics in module.PERFORMANCE.items():
        for name in metrics:
            if name in seen or name.endswith('_rate') or name in performance:continue
            performance.add(name)
            registry.append(dict(name=name,kind='mean' if ('rating' in name or 'accuracy' in name) else 'count',group='performance',candidate=False,label=name.replace('_',' ').capitalize(),source='combined player-match statistics',scope='performance'))
    for family in ['passes','long_balls','crosses','through_balls','dribbles','aerial_duels']:
        registry.append(dict(name=family+'_success_rate',kind='ratio',numerator=family+'_completed',denominator=family+'_attempted',group='performance',candidate=False,label=family.replace('_',' ').title()+' completion',source='compatible collected count pair',scope='performance'))
    for name in ['long_balls','crosses','through_balls']:
        registry.append(dict(name=name+'_attempt_share',kind='ratio',numerator=name+'_attempted',denominator='passes_attempted',group='passing',candidate=True,label=name.replace('_',' ').title()+' attempt share',source='combined player-match statistics',scope='role'))
    for name in HEAT_CONTEXT:
        registry.append(dict(name='heatmap_'+name,column=name,kind='mean',weight='touch_count',group='provider heatmap context',candidate=False,label='Provider heatmap '+name.replace('_',' '),source='sofascore heatmap summaries',scope='role'))
    registry.extend({**s,'scope':'role'} for s in event_specs)
    for s in registry:
        s['feature']=s['name']+'_per90' if s['kind']=='count' else s['name']
        s.setdefault('column',s['name']);s.setdefault('scope','role')
        s['model_exclusion']='' if s['candidate'] else ('execution/context only' if s['scope']=='performance' else 'context, inferred, redundant or unverified; not in role distance')
    return registry

def contributions(df,registry):
    n={};d={};coverage={};match_values={}
    minutes=df.minutes_played.astype(float)
    def col(name):return pd.to_numeric(df[name],errors='coerce') if name in df else pd.Series(np.nan,index=df.index)
    for s in registry:
        feature=s['feature'];kind=s['kind']
        if kind=='count':
            v=col(s['column']);valid=v.notna()&v.ge(0)&minutes.gt(0)
            num=v*90;den=minutes
        elif kind=='mean':
            v=col(s['column']);weight=col(s['weight']) if s.get('weight') else minutes
            valid=v.notna()&weight.notna()&weight.gt(0);num=v*weight;den=weight
        else:
            num=col(s['numerator']);den=col(s['denominator']);valid=num.notna()&den.notna()&den.ge(0)
        n[feature]=num.where(valid,0).fillna(0);d[feature]=den.where(valid,0).fillna(0)
        coverage[feature]=minutes.where(valid,0);match_values[feature]=num.div(den.replace(0,np.nan)).where(valid)
    return pd.DataFrame(n),pd.DataFrame(d),pd.DataFrame(coverage),pd.DataFrame(match_values)

def summarize(df,numerators,denominators,coverage,config):
    parts=[];contexts=[];contrib={}
    # One all-positions season profile plus disjoint sets of matches in each detailed position.
    for dimension in ['All positions','detailed']:
        work=df.copy();work['comparison_position']='All positions' if dimension=='All positions' else work['comparison_position']
        grouped=work.groupby(KEYS+['comparison_position'],sort=True)
        for key,g in grouped:
            idx=g.index;num=numerators.loc[idx].sum();den=denominators.loc[idx].sum();covered=coverage.loc[idx].sum();values=num/den.replace(0,np.nan)
            comp,season,pid,pos=key;profile_id='|'.join(map(str,key));mins=g.minutes_played.sum();emins=g.loc[g.event_covered,'minutes_played'].sum()
            position_minutes={}
            for _,row in g.iterrows():
                u=json.loads(row.position_usage_json) if pd.notna(row.position_usage_json) else {}
                total=sum(u.values())
                if total:
                    for label,v in u.items():position_minutes[label]=position_minutes.get(label,0)+row.minutes_played*v/total
                else:position_minutes[row.detailed_position]=position_minutes.get(row.detailed_position,0)+row.minutes_played
            dominant=max(position_minutes,key=position_minutes.get) if position_minutes else 'Unknown'
            eligible=pos not in ['All positions','Mixed / uncertain','Unknown'] and mins>=config.min_minutes and len(g)>=config.min_matches and emins/mins>=config.min_event_coverage
            reasons=[]
            if mins<config.min_minutes:reasons.append('minutes below threshold')
            if len(g)<config.min_matches:reasons.append('matches below threshold')
            if emins/mins<config.min_event_coverage:reasons.append('event coverage below threshold')
            if pos in ['Mixed / uncertain','Unknown']:reasons.append('position insufficiently resolved')
            row={'profile_id':profile_id,'competition':comp,'season':season,'player_id':int(pid),'player_name':g.sort_values('date').iloc[-1].player_name,
                 'position_group':pos,'dominant_season_position':dominant,'teams':' / '.join(sorted(g.team.dropna().unique())),
                 'matches':len(g),'minutes':mins,'event_matches':int(g.event_covered.sum()),'event_minutes':emins,
                 'event_minute_coverage':emins/mins,'first_match':str(g.date.min()),'last_match':str(g.date.max()),
                 'position_minutes_json':json.dumps(position_minutes,sort_keys=True),'eligible_comparison':eligible,
                 'eligibility_reason':'; '.join(reasons) if reasons else ('season overview' if pos=='All positions' else 'eligible'),
                 **values.to_dict()}
            # Preserve the full unfurled region map in every season/profile and per action family.
            for family in FAMILIES:
                counts=g[['zone_'+family+'_'+z for z in ZONES]].sum(min_count=1).to_numpy(float)
                total=np.nansum(counts);row['zone_'+family+'_n']=total if g.event_covered.any() else np.nan
                for z,value in zip(ZONES,counts):row['zone_'+family+'_'+z+'_share']=value/total if total>0 else np.nan
            season_z=np.array([row['zone_on_ball_'+z+'_share'] for z in ZONES],float)
            diversities=[];weights=[]
            for _,match in g.iterrows():
                z=match[['zone_on_ball_'+z for z in ZONES]].to_numpy(float);total=np.nansum(z)
                if total>=10 and np.isfinite(season_z).all():
                    p=z/total;mid=(p+season_z)/2
                    def kl(a,b):
                        mask=a>0;return np.sum(a[mask]*np.log2(a[mask]/b[mask]))
                    diversities.append((kl(p,mid)+kl(season_z,mid))/2);weights.append(match.minutes_played)
            row['spatial_match_variation_jsd']=np.average(diversities,weights=weights) if weights else np.nan
            row['spatial_variation_matches']=len(weights)
            parts.append(row)
            contexts.extend({'profile_id':profile_id,'feature':f,'observed_minutes':covered[f],'minute_coverage':covered[f]/mins,'numerator':num[f],'denominator':den[f]} for f in values.index)
            contrib[profile_id]={'indices':idx.to_numpy(),'num':numerators.loc[idx].to_numpy(float),'den':denominators.loc[idx].to_numpy(float)}
    return pd.DataFrame(parts),pd.DataFrame(contexts),contrib
