"""Read inventories and saved schedules, without scanning raw payloads or scraping."""
from storage.layout import system_path
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time

from config.workerPlan import RESOURCES, load_plan
from collection.ensembleProgress import read_progress
from .coverage import build_grid
from .jobTiming import refresh_timing
from collection.reporting import inventory_paths, read_logs

NAMESPACE_RESOURCES={
 'sofascore':['sofascore_stats','sofascore_details','match_statistics','incidents','momentum','average_positions','heatmaps','shotmaps','action_maps','whoscored_stats','fotmob_enrichment'],
 'whoscored_events':['whoscored_events'],'fotmob_players':['fotmob_stats']}


def resource_states(row):
    states={name:'pending' for name in NAMESPACE_RESOURCES.get(row.get('namespace'),[])}
    explicit=row.get('resources',{})
    # A resource-aware local audit records evidence; omitted optional fields were not collected.
    # Truly legacy inventories without this map remain uncertain until their next audit.
    if isinstance(explicit,dict) and explicit:states={key:'not_attempted' for key in states}
    caps=set(row.get('capabilities',[]))
    if not isinstance(explicit,dict) or not explicit:
        for old,new in (('average_positions','average_positions'),('heatmap','heatmaps'),('shotmap','shotmaps'),('rating_breakdown','action_maps')):
            if 'spatial:'+old in caps:states[new]='unknown'
        if 'browser_details' in caps:
            for name in ('sofascore_details','match_statistics','incidents','momentum'):states[name]='unknown'
    # Older terminal spatial flags include unavailable/deferred. Their availability is unknown.
    if row.get('status')=='complete':
        if row.get('namespace')=='whoscored_events':states['whoscored_events']='complete'
        if row.get('namespace')=='fotmob_players':states['fotmob_stats']='complete'
    if 'sofascore_match_stats' in caps or 'browser_details' in caps:states['sofascore_stats']='complete'
    if 'provider:whoscored' in caps:states['whoscored_stats']='complete'
    if 'provider:fotmob' in caps:states['fotmob_enrichment']='complete'
    explicit=row.get('resources',{})
    if isinstance(explicit,dict):
        for name,value in explicit.items():
            if name in states and value in ('complete','partial','unavailable','deferred','failed','not_attempted','pending','visible_only'):
                states[name]=value
    return states


class DashboardModel:
    def __init__(self,root,ensemble,plan_path,worker,*,clock=time.monotonic):
        self.root=Path(root);self.ensemble=Path(ensemble);self.plan_path=Path(plan_path);self.worker=worker
        self.clock=clock;self.loaded=-100;self.snapshot=None;self.rows=[];self.lock=threading.Lock();self.review_seeded=False

    def status(self):
        with self.lock:
            if self.snapshot is not None and self.clock()-self.loaded<3:return self.snapshot
            self.snapshot=self._build();self.loaded=self.clock();return self.snapshot

    def _schedule(self,competition,season,namespace,warnings):
        slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
        directory=self.root/slug/season
        path=directory/('whoscored_schedule.json' if namespace=='whoscored_events' else 'schedule.json')
        if namespace=='fotmob_players':path=self.root/'fotmob_player_stats'/slug/season/'schedule.json'
        if not path.exists():return None
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            return {str(event['id']) for event in saved['events'] if event.get('status',{}).get('type','finished')=='finished'}
        except (OSError,ValueError,KeyError,TypeError):
            warnings.append('A saved schedule could not be read. Its target count is unknown.');return None

    def _build(self):
        plan=load_plan(self.plan_path);warnings=[];rows=[];groups={};all_keys=set();workers={}
        for worker,info in plan.workers.items():
            workers[worker]={'id':worker,'label':info['label'],'color':info['color'],'enabled':info['enabled'],
                             'configured':True,'role':info['role'],'jobs':[j.as_dict() for j in plan.for_worker(worker)],'records':0,'collected':0,'runtime':None}
        for path in inventory_paths(self.ensemble,local_worker=self.worker):
            try:
                saved=read_progress(path)
                worker=saved.get('worker_id')
                if worker!=path.stem:raise ValueError('Worker identity mismatch')
                if worker not in workers:
                    workers[worker]={'id':worker,'label':worker,'color':'#7a8394','enabled':False,'jobs':[],
                                     'configured':False,'records':0,'collected':0,'runtime':None}
                for key,row in saved['records'].items():
                    if not all(isinstance(row.get(f),str) for f in ('namespace','competition','season','match_id')):continue
                    row={**row,'holder':worker,'resources':resource_states(row),'key':key}
                    rows.append(row);all_keys.add(key);workers[worker]['records']+=1
                    if worker in row.get('origins',[]):workers[worker]['collected']+=1
                workers[worker]['inventory_at']=max((row.get('updated_at','') for row in saved['records'].values()),default=None)
            except (OSError,ValueError,TypeError,KeyError):warnings.append(f'Inventory for {path.stem} could not be read.')
        for worker,info in workers.items():
            path=self.ensemble/'runtime'/(worker+'.json')
            direct=self.ensemble/'reporting'/'runtime'/(worker+'.json')
            if direct.exists():path=direct
            local=system_path(self.root,'worker_runtime')/(worker+'.json')
            if worker==self.worker and local.exists():path=local
            if path.exists():
                try:
                    state=json.loads(path.read_text(encoding='utf-8'))
                    if state.get('worker_id')==worker and state.get('format')=='soccer-worker-runtime-v1':
                        info['runtime']=refresh_timing(state)
                        # Remote files are published snapshots, never evidence that a process is online.
                        info['runtime_source']='local' if path==local else 'direct' if path==direct else 'published'
                        if path==direct:
                            received=datetime.fromisoformat(state['received_at'])
                            seconds=(datetime.now(timezone.utc)-received).total_seconds()
                            info['connection']='connected' if seconds<=plan.reporting['heartbeat_seconds']*3 else 'disconnected'
                except (OSError,ValueError,TypeError,KeyError):warnings.append(f'Status for {worker} could not be read.')
        for row in rows:
            for resource,state in row['resources'].items():
                key=(row['holder'],row['competition'],row['season'],resource)
                group=groups.setdefault(key,{'counts':Counter(),'ids':set(),'planned':False})
                group['counts'][state]+=1;group['ids'].add(row['match_id'])
        for job in plan.jobs:
            if not plan.workers[job.worker]['enabled']:continue
            for resource in job.resources:
                key=(job.worker,job.competition,job.as_dict()['season'],resource)
                groups.setdefault(key,{'counts':Counter(),'ids':set(),'planned':False})['planned']=True
        coverage=[];schedules={}
        for (worker,competition,season,resource),value in sorted(groups.items()):
            namespace=RESOURCES[resource][1];key=(competition,season,namespace)
            if key not in schedules:schedules[key]=self._schedule(*key,warnings)
            schedule=schedules[key]
            total=len(schedule|value['ids']) if schedule is not None else None
            counts=dict(value['counts'])
            if total is not None:counts['missing']=max(0,total-sum(counts.values()))
            coverage.append({'worker':worker,'competition':competition,'season':season,'resource':resource,
                             'counts':counts,'recorded':len(value['ids']),'total':total,'planned':value['planned']})
        def get_schedule(comp,season,namespace):
            key=(comp,season,namespace)
            if key not in schedules:schedules[key]=self._schedule(comp,season,namespace,warnings)
            return schedules[key]
        grid=build_grid(rows,plan,get_schedule,workers)
        self.rows=rows
        return {'updated_at':datetime.now(timezone.utc).isoformat(),'local_worker':self.worker,
                'coordinator':plan.coordinator,'plan_revision':plan.revision[:10],
                'resources':{key:label for key,(label,_) in RESOURCES.items()},'workers':list(workers.values()),
                'jobs':[j.as_dict() for j in plan.jobs if plan.workers[j.worker]['enabled']],
                'reporting':{**plan.reporting,'active':any(w.get('runtime_source')=='direct' or w.get('runtime') and w['runtime'].get('reporting',{}).get('state')=='connected' for w in workers.values())},'grid':grid,'poll_seconds':plan.git['poll_seconds'],
                'coverage':coverage,'unique_records':len(all_keys),
                'competitions':sorted({r['competition'] for r in rows}|{j.competition for j in plan.jobs}),
                'seasons':sorted({r['season'] for r in rows}|{j.as_dict()['season'] for j in plan.jobs},reverse=True),
                'warnings':list(dict.fromkeys(warnings))}

    def logs(self,filters):
        def value(key,default=''):return filters.get(key,[default])[0]
        try:limit=int(value('limit','200'));after=max(0,int(value('after','0')))
        except ValueError:limit=200;after=0
        return read_logs(self.root,value('worker'),value('level'),value('search'),limit,after,focus=value('focus'))

    def matches(self,filters):
        self.status()
        with self.lock:
            rows=self.rows
            for field in ('competition','season','holder'):
                value=filters.get(field,[''])[0]
                if value:rows=[r for r in rows if r[field]==value]
            resource=filters.get('resource',[''])[0]
            if resource:
                wanted=set(resource.split(','))
                rows=[r for r in rows if wanted.intersection(r['resources'])]
            try:offset=max(0,int(filters.get('offset',['0'])[0]));limit=min(100,max(1,int(filters.get('limit',['40'])[0])))
            except ValueError:offset=0;limit=40
            return {'total':len(rows),'offset':offset,'rows':[
                {key:row.get(key) for key in ('holder','namespace','competition','season','match_id','origins','resources','updated_at')}
                for row in rows[offset:offset+limit]]}

    def reviews(self):
        from providers.identityReview import ReviewStore,seed_existing
        with self.lock:
            if not self.review_seeded:
                seed_existing(self.root);self.review_seeded=True
        value=ReviewStore(self.root).snapshot()
        value['can_review']=self.worker==load_plan(self.plan_path).coordinator
        logs=read_logs(self.root,limit=1000,focus='warnings')['rows']
        # Show outages separately: a reviewed name cannot repair an unavailable response.
        issues={}
        for row in reversed(logs):
            message=row['message']
            if ': partial (' not in message and 'fixture discovery failed' not in message:continue
            if 'HTTP 502' in message:kind='WhoScored unavailable (HTTP 502)'
            elif 'Expected one fixture' in message:kind='Provider fixture missing or team identity unresolved'
            elif 'match team mismatch' in message:kind='Provider match team disagreement'
            elif 'identity mismatch' in message:kind='Player identity needs review'
            elif 'matchCentreData' in message:kind='WhoScored page data incomplete'
            else:continue
            key=(row['worker'],kind)
            if key not in issues:issues[key]={'worker':row['worker'],'reason':kind,'example':message,'at':row['at'],'count':0}
            issues[key]['count']+=1
        value['issues']=list(issues.values())
        return value

    def decide_identity(self,value):
        from providers.identityReview import ReviewStore
        if self.worker!=load_plan(self.plan_path).coordinator:
            raise ValueError('Confirm shared identities on the Main dashboard')
        if not isinstance(value,dict):raise ValueError('Invalid review decision')
        return ReviewStore(self.root).decide(value.get('id'),value.get('action'))
