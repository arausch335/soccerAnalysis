"""Unique collection units, grouped by competition and aligned season columns."""
from collections import defaultdict, Counter

CATEGORIES=[
    ('players','Player match stats',[
        ('sofa','Sofascore',['sofascore_stats']),
        ('fotmob','FotMob',['fotmob_enrichment','fotmob_stats']),
        ('who','WhoScored',['whoscored_stats'])]),
    ('events','Events',[('events','WhoScored',['whoscored_events'])]),
    ('spatial','Spatial maps',[('maps','Sofascore',['average_positions','heatmaps','shotmaps','action_maps'])]),
    ('others','Others',[
        ('team','Team match stats',['match_statistics']),('incidents','Incidents',['incidents']),
        ('momentum','Momentum',['momentum'])])]
SPATIAL=[('positions','Average positions',['average_positions']),('heatmaps','Heatmaps',['heatmaps']),
         ('shots','Shot maps',['shotmaps']),('actions','Action maps',['action_maps'])]


def build_grid(rows,plan,schedule,workers):
    aliases={}
    for row in rows:
        if row['namespace']=='sofascore':
            for provider,mid in row.get('provider_match_ids',{}).items():
                aliases[(row['competition'],row['season'],provider,str(mid))]='sofascore:'+row['match_id']
    def identity(row,resource):
        provider={'sofascore':'sofascore','whoscored_events':'whoscored','fotmob_players':'fotmob'}[row['namespace']]
        return aliases.get((row['competition'],row['season'],provider,row['match_id']),provider+':'+row['match_id'])
    combined_fot={(r['competition'],r['season']) for r in rows if r['namespace']=='sofascore' and r['resources'].get('fotmob_enrichment')=='complete'}
    observed=defaultdict(dict)
    for row in rows:
        for resource,state in row['resources'].items():
            # FotMob native/enrichment are two ways to save the same player match statistics.
            normalized='fotmob_enrichment' if resource=='fotmob_stats' else resource
            group=observed[(row['competition'],row['season'],normalized)]
            key=identity(row,resource)
            proof={'state':state,'owners':row.get('resource_origins',{}).get(resource) or row.get('origins',[])}
            if (resource=='fotmob_stats' and (row['competition'],row['season']) in combined_fot
                    and (row['competition'],row['season'],'fotmob',row['match_id']) not in aliases):
                proof={**proof,'state':'unknown','unlinked':True}
            state=proof['state']
            current=group.get(key)
            rank={'complete':6,'unavailable':5,'partial':4,'visible_only':4,'failed':3,'deferred':2,'not_attempted':1,'unknown':0}
            if current is None or rank.get(state,0)>rank.get(current['state'],0):group[key]=proof
            elif state=='complete' and current['state']=='complete':current['owners']=sorted(set(current['owners'])|set(proof['owners']))
    assignments={}
    for job in plan.jobs:
        if not plan.workers[job.worker]['enabled']:continue
        for resource in job.resources:
            resource='fotmob_enrichment' if resource=='fotmob_stats' else resource
            assignments[(job.competition,job.as_dict()['season'],resource)]=job.worker
    # Runtime schedule counts let the monitor know targets without receiving raw match files.
    remote_counts={}
    for worker in workers.values():
        for saved in (worker.get('runtime') or {}).get('schedules',{}).values():
            key=(saved.get('competition'),saved.get('season'),saved.get('kind'))
            remote_counts[key]=max(remote_counts.get(key,0),len(set(saved.get('match_ids',[]))))
    def target(comp,season,resource):
        native={'whoscored_events':'whoscored_events','fotmob_stats':'fotmob_players'}.get(resource,'sofascore')
        ids=schedule(comp,season,native)
        if ids is not None:return len(ids),'saved schedule'
        kind=native if native!='sofascore' else 'combined'
        counts=[remote_counts[(comp,season,k)] for k in (kind,'sofascore' if native=='sofascore' else kind) if (comp,season,k) in remote_counts]
        if counts:return max(counts),'worker schedule'
        # Combined jobs target the baseline fixture list for all enrichments/events.
        combined=any(j.competition==comp and j.as_dict()['season']==season and 'combined' in j.collection_types for j in plan.jobs)
        if resource=='whoscored_events' and combined:
            ids=schedule(comp,season,'sofascore')
            if ids is not None:return len(ids),'combined fixture target'
            if (comp,season,'combined') in remote_counts:return remote_counts[(comp,season,'combined')],'combined worker target'
        if resource=='fotmob_enrichment':
            ids=schedule(comp,season,'fotmob_players')
            if ids is not None:return len(ids),'FotMob schedule'
            if (comp,season,'fotmob_players') in remote_counts:return remote_counts[(comp,season,'fotmob_players')],'FotMob worker schedule'
        return None,'target not yet discovered'
    seasons={row['season'] for row in rows}|{j.as_dict()['season'] for j in plan.jobs}
    # Include intervening seasons even when a competition has no records for them.
    if seasons:
        starts=[int(s[:4]) for s in seasons]
        seasons=[f'{year}-{year+1}' for year in range(min(starts),max(starts)+1)]
    else:seasons=[]
    competitions=sorted({r['competition'] for r in rows}|{j.competition for j in plan.jobs})
    def cell(comp,season,resources):
        complete=Counter();planned=Counter();states=Counter();unplanned=0;total=0;uncertain=0;missing_totals=0
        sources=set();unknown_attribution=0;allocation=Counter();unassigned_types=0
        for resource in dict.fromkeys('fotmob_enrichment' if r=='fotmob_stats' else r for r in resources):
            group=observed.get((comp,season,resource),{})
            expected,source=target(comp,season,resource)
            if any(p.get('unlinked') for p in group.values()):
                missing_totals+=1;source+='; native FotMob IDs need linking'
            sources.add(source)
            if expected is None:missing_totals+=1
            expected=max(expected or 0,len(group))
            for proof in group.values():
                states[proof['state']]+=1
                if proof['state']=='complete':
                    owners=proof['owners']
                    owner=owners[0] if len(owners)==1 and owners[0] in workers else '_mixed'
                    complete[owner]+=1
                    if owner=='_mixed':unknown_attribution+=1
                elif proof['state']=='unknown':uncertain+=1
            saved=sum(p['state']=='complete' for p in group.values())
            unknown=sum(p['state']=='unknown' for p in group.values())
            unavailable=sum(p['state']=='unavailable' for p in group.values())
            remaining=max(0,expected-saved-unknown-unavailable)
            owner=assignments.get((comp,season,resource))
            if owner:
                planned[owner]+=remaining;allocation[owner]+=1
            else:
                unplanned+=remaining;unassigned_types+=1
            total+=expected
        unavailable=states['unavailable']
        return {'total':total,'complete':dict(complete),'planned':dict(planned),'unplanned':unplanned,
                'unavailable':unavailable,'verification':uncertain,'unknown_target':bool(missing_totals),
                'allocation':dict(allocation),'unassigned_types':unassigned_types,
                'mixed_attribution':unknown_attribution,'states':dict(states),'target_source':', '.join(sorted(sources))}
    result=[]
    for comp in competitions:
        categories=[]
        for category,label,definitions in CATEGORIES:
            series=[]
            for key,title,resources in definitions:
                series.append({'id':key,'label':title,'resources':resources,
                    'unit':'map sets' if key=='maps' else 'matches',
                    'cells':{s:cell(comp,s,resources) for s in seasons},
                    'details':[{'id':k,'label':t,'resources':rs,'cells':{s:cell(comp,s,rs) for s in seasons}} for k,t,rs in SPATIAL] if key=='maps' else []})
            categories.append({'id':category,'label':label,'rows':series})
        result.append({'competition':comp,'categories':categories})
    return {'seasons':seasons,'competitions':result}
