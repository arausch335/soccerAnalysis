"""Main alone converts reported holdings and schedules into scrape instructions."""
import hashlib
import json
from pathlib import Path
from .ensembleProgress import job_key,merge_records
from .workerRuntime import now
from storage.pandasStore import atomic_json

FORMAT='soccer-main-instructions-v1'


def path_for(root):return Path(root)/'_system/workers/main-instructions.json'


def compile_instructions(plan,worker,reports):
    """Pure Main-side planning. Inventories are evidence, never worker assignments."""
    records={};schedules=[];history={}
    for report in reports:
        inventory=report.get('inventory') or {}
        for key,row in inventory.get('records',{}).items():
            previous=records.get(key,{})
            old_resources=dict(previous.get('resources',{}));old_ids=dict(previous.get('provider_match_ids',{}))
            merge_records(records,{key:row})
            ranks={'complete':6,'unavailable':5,'deferred':4,'partial':3,'failed':2,'not_attempted':1}
            for name,state in row.get('resources',{}).items():
                if ranks.get(state,0)>ranks.get(old_resources.get(name),0):old_resources[name]=state
            records[key]['resources']=old_resources
            records[key]['provider_match_ids']={**old_ids,**row.get('provider_match_ids',{})}
        schedules.extend(inventory.get('schedules',{}).values())
        runtime=report.get('runtime') or {}
        schedules.extend(runtime.get('schedules',{}).values())
        if runtime.get('worker_id')==worker:history.update(runtime.get('jobs',{}))
    jobs={};approved={}
    for job in plan.for_worker(worker):
        season=job.as_dict()['season'];targets=set();remaining={}
        for schedule in schedules:
            if not isinstance(schedule,dict):continue
            if schedule.get('competition')==job.competition and schedule.get('season')==season and schedule.get('kind')=='combined':
                targets.update(str(mid) for mid in schedule.get('match_ids',[]))
        discovered=bool(targets)
        required=['provider:sofascore','provider:fotmob','provider:whoscored','sofascore_match_stats']
        spatial=() if job.spatial_capture=='none' else ('average_positions',) if job.spatial_capture=='fast' else ('average_positions','heatmap','shotmap','rating_breakdown')
        required+=['spatial:'+name for name in spatial]
        for key,row in records.items():
            if row.get('namespace')!='sofascore' or row.get('competition')!=job.competition or row.get('season')!=season:continue
            mid=str(row['match_id']);targets.add(mid)
            resources=dict(row.get('resources',{}));who=row.get('provider_match_ids',{}).get('whoscored')
            if who is not None:
                evidence=records.get(job_key('whoscored_events',job.competition,season,who),{})
                if evidence.get('status')=='complete':resources['whoscored_events']='complete'
            missing=[name for name in job.resources if resources.get(name) not in ('complete','unavailable','deferred')]
            valid=set(required)<=set(row.get('capabilities',[]))
            if not valid and not missing:missing=['checkpoint_validation']
            remaining[mid]=missing
            if not missing and valid:
                approved[key]={'capabilities':required,'holders':row.get('holders',[])}
        for mid in targets:remaining.setdefault(mid,list(job.resources))
        revision=hashlib.sha256(json.dumps([job.as_dict(),sorted(targets),remaining],sort_keys=True).encode()).hexdigest()[:20]
        jobs[job.id]={'season':season,'required_resources':job.resources,'schedule_known':discovered,
                      'remaining':{mid:resources for mid,resources in sorted(remaining.items()) if resources},
                      'known_matches':len(targets),'remaining_matches':sum(bool(value) for value in remaining.values()),
                      'revision':revision,
                      'recheck':bool(discovered and any(remaining.values()) and job.id in history and
                                     history[job.id].get('instruction_revision')!=revision)}
    return {'format':FORMAT,'coordinator':plan.coordinator,'plan_revision':plan.revision,
            'generated_at':now(),'worker_id':worker,'jobs':jobs,'approved':approved}


def install(root,value,*,worker=None,revision=None):
    if (not isinstance(value,dict) or value.get('format')!=FORMAT or
            not isinstance(value.get('jobs'),dict) or not isinstance(value.get('approved'),dict) or
            worker is not None and value.get('worker_id')!=worker or
            revision is not None and value.get('plan_revision')!=revision):
        raise ValueError('Main instructions do not match this worker and plan')
    atomic_json(path_for(root),value)


def approved_by_main(progress,key,required):
    path=path_for(progress.root)
    if not path.exists():return False
    value=json.loads(path.read_text())
    if (value.get('format')!=FORMAT or value.get('worker_id')!=progress.origin or
            value.get('plan_revision')!=getattr(progress,'plan_revision',None)):return False
    decision=value.get('approved',{}).get(key,{})
    holders=set(decision.get('holders',[]))
    return bool(holders-{progress.origin}) and set(required)<=set(decision.get('capabilities',[]))


def requested_again(root,worker,revision,history):
    """A local finished flag cannot veto a new recheck ordered by Main."""
    path=path_for(root)
    if not path.exists():return set()
    value=json.loads(path.read_text())
    if value.get('worker_id')!=worker or value.get('plan_revision')!=revision:return set()
    return {job for job,command in value.get('jobs',{}).items() if command.get('recheck') and
            history.get(job,{}).get('instruction_revision')!=command.get('revision')}
