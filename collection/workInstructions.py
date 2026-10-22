"""Main alone converts reported holdings and schedules into scrape instructions."""
import hashlib
import json
from pathlib import Path
from .ensembleProgress import job_key,merge_records
from .workerRuntime import now
from storage.pandasStore import atomic_json

FORMAT='soccer-main-instructions-v1'


def path_for(root):return Path(root)/'_system/workers/main-instructions.json'


def compile_instructions(plan,worker,reports,decisions=None):
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
    from .jobCompletion import assess
    jobs={};approved={}
    for job in plan.for_worker(worker):
        progress=assess(job,records,schedules,history,decisions)
        required=['provider:sofascore','provider:fotmob','provider:whoscored','sofascore_match_stats']
        if job.spatial_capture=='all':required+=['spatial:'+name for name in ('average_positions','heatmap','shotmap','rating_breakdown')]
        for mid in progress['targets']:
            if mid in progress['remaining']:continue
            key=job_key('sofascore',job.competition,job.season,mid);row=records.get(key,{})
            approved[key]={'capabilities':row.get('capabilities',[]),'required_capabilities':required,
                           'holders':row.get('holders',[]),'resolved':True,
                           'waived_resources':progress['waivers'].get(mid,[])}
        revision=hashlib.sha256(json.dumps([job.as_dict(),progress['targets'],progress['remaining'],
            progress['waivers'],{k:v for k,v in (decisions or {}).get('decisions',{}).items()
                if k.startswith(progress['configuration']+'|')}],sort_keys=True).encode()).hexdigest()[:20]
        jobs[job.id]={**progress,'season':job.as_dict()['season'],'required_resources':job.resources,
                      'revision':revision,
                      'recheck':bool(not progress['done'] and progress['schedule_known'] and progress['remaining']
                          and job.id in history and history[job.id].get('instruction_revision')!=revision)}
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
    authorized=set(decision.get('capabilities',[]))
    if decision.get('resolved'):authorized.update(decision.get('required_capabilities',[]))
    return bool(holders-{progress.origin} or decision.get('waived_resources')) and set(required)<=authorized


def requested_again(root,worker,revision,history):
    """A local finished flag cannot veto a new recheck ordered by Main."""
    path=path_for(root)
    if not path.exists():return set()
    value=json.loads(path.read_text())
    if value.get('worker_id')!=worker or value.get('plan_revision')!=revision:return set()
    return {job for job,command in value.get('jobs',{}).items() if command.get('recheck') and
            history.get(job,{}).get('instruction_revision')!=command.get('revision')}


def reviewed_by_main(progress,competition,season,mid):
    value=json.loads(path_for(progress.root).read_text())
    return bool(value.get('approved',{}).get(job_key('sofascore',competition,season,mid),{}).get('waived_resources'))


def current_commands(root,worker,revision):
    path=path_for(root)
    if not path.exists():return {}
    value=json.loads(path.read_text())
    if value.get('worker_id')!=worker or value.get('plan_revision')!=revision:return {}
    return value.get('jobs',{})
