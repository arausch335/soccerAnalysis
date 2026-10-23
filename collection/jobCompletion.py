"""Configuration-scoped progress and explicit Main-owned collection exceptions."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from .workerRuntime import now

GROUPS=(('player_match_stats','Player match stats',('sofascore_stats','fotmob_enrichment','whoscored_stats','fotmob_stats')),
        ('events','Events',('whoscored_events',)),
        ('match_details','Match details',('sofascore_details','match_statistics','incidents','momentum')),
        ('spatial_maps','Spatial maps',('average_positions','heatmaps','shotmaps','action_maps')))


def configuration(job):
    return configuration_key(job.competition,job.as_dict()['season'],job.resources,job.spatial_capture)


def configuration_key(competition,season,resources,spatial):
    value=[2,competition,season,sorted(resources),spatial]
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:24]


def scoped_decisions(job,state):
    """A reviewed resource remains reviewed when requirements or workers change."""
    signature=configuration(job);contexts=state.get('configurations',{});selected={}
    for row in state.get('decisions',{}).values():
        context=contexts.get(row['configuration'],{})
        if row['configuration']!=signature:
            if (context.get('competition'),context.get('season'))!=(job.competition,job.as_dict()['season']):continue
            if row['resource']=='_season' and row['action']=='close' and not set(job.resources)<=set(context.get('resources',[])):continue
        key='|'.join((signature,row['match_id'],row['resource']))
        if row['id']>selected.get(key,{}).get('id',-1):selected[key]=row
    return selected


class CompletionStore:
    def __init__(self,root):self.path=Path(root)/'_system/workers/completion.sqlite'
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY,configuration TEXT,match_id TEXT,resource TEXT,action TEXT,reason TEXT,attempts INTEGER,at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS finished(configuration TEXT PRIMARY KEY,summary TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS configurations(configuration TEXT PRIMARY KEY,context TEXT)')
        return closing(db)
    def register(self,db,job):
        # Recover hashes written before contexts were stored, including the old
        # fast requirement for momentum. Never remove or rewrite old decisions.
        for spatial in ('fast','all'):
            from config.workerPlan import requested_resources
            resources=requested_resources(job.collection_types,spatial)
            for required in (resources,sorted(set(resources)|{'momentum'})):
                context={'competition':job.competition,'season':job.as_dict()['season'],
                         'resources':required,'spatial_capture':spatial}
                key=configuration_key(context['competition'],context['season'],required,spatial)
                db.execute('INSERT OR IGNORE INTO configurations VALUES(?,?)',(key,json.dumps(context)))
        context={'competition':job.competition,'season':job.as_dict()['season'],
                 'resources':job.resources,'spatial_capture':job.spatial_capture}
        db.execute('INSERT OR IGNORE INTO configurations VALUES(?,?)',(configuration(job),json.dumps(context)))

    def snapshot(self,jobs=()):
        if not self.path.exists() and not jobs:return {'decisions':{},'history':[],'completed':{},'revision':0,'configurations':{}}
        with self.connect() as db:
            for job in jobs:self.register(db,job)
            db.commit()
            rows=[dict(zip(('id','configuration','match_id','resource','action','reason','attempts','at'),row))
                  for row in db.execute('SELECT * FROM actions ORDER BY id')]
            completed={key:json.loads(value) for key,value in db.execute('SELECT configuration,summary FROM finished')}
            contexts={key:json.loads(value) for key,value in db.execute('SELECT * FROM configurations')}
            for key,summary in completed.items():
                if key not in contexts and summary.get('competition') and summary.get('season'):
                    context={k:summary[k] for k in ('competition','season')}
                    context['resources']=list(summary.get('counts',{}))
                    db.execute('INSERT OR IGNORE INTO configurations VALUES(?,?)',(key,json.dumps(context)))
                    contexts[key]=context
            db.commit()
        current={}
        for row in rows:current['|'.join(row[k] for k in ('configuration','match_id','resource'))]=row
        history=[{**contexts.get(row['configuration'],{}),**row} for row in rows[-200:]]
        return {'decisions':current,'history':history,'completed':completed,'configurations':contexts,'revision':rows[-1]['id'] if rows else 0}
    def freeze(self,job,summary,revision):
        if summary.get('frozen') or not summary.get('done') or summary.get('configuration')!=configuration(job):return False
        with self.connect() as db:
            self.register(db,job);db.commit()
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT coalesce(max(id),0) FROM actions').fetchone()[0]!=revision:return False
            db.execute('INSERT OR IGNORE INTO finished VALUES(?,?)',(configuration(job),json.dumps({**summary,'competition':job.competition,'season':job.as_dict()['season']})))
            db.commit()
        return True

    def record(self,job,action,*,mid='',resources=(),reason='',attempts=0):
        if action not in ('unavailable','retry','close','reopen'):raise ValueError('Unknown collection review action')
        if action in ('unavailable','close') and not 5<=len(reason.strip())<=1000:raise ValueError('Explain the review decision (5–1000 characters)')
        if action in ('unavailable','retry') and (not str(mid).isdigit() or not resources or set(resources)-set(job.resources)):
            raise ValueError('Choose a saved match and required resources')
        resources=resources if action in ('unavailable','retry') else ('_season',)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.register(db,job)
            if action in ('reopen','retry'):
                keys=[key for key,body in db.execute('SELECT configuration,context FROM configurations')
                      if (json.loads(body)['competition'],json.loads(body)['season'])==(job.competition,job.as_dict()['season'])]
                db.executemany('DELETE FROM finished WHERE configuration=?',[(key,) for key in keys])
            if action=='reopen':
                rows=[dict(zip(('id','configuration','match_id','resource','action','reason','attempts','at'),r))
                      for r in db.execute('SELECT * FROM actions ORDER BY id')]
                previous={'decisions':{str(r['id']):r for r in rows},
                          'configurations':{key:json.loads(body) for key,body in db.execute('SELECT * FROM configurations')}}
                for row in scoped_decisions(job,previous).values():
                    if row['action']=='unavailable':
                        db.execute('INSERT INTO actions(configuration,match_id,resource,action,reason,attempts,at) VALUES(?,?,?,?,?,?,?)',
                            (configuration(job),row['match_id'],row['resource'],'retry',reason.strip(),row['attempts'],now()))
            for resource in resources:
                db.execute('INSERT INTO actions(configuration,match_id,resource,action,reason,attempts,at) VALUES(?,?,?,?,?,?,?)',
                           (configuration(job),str(mid),resource,action,reason.strip(),attempts,now()))
            db.commit()


def assess(job,records,schedules,history,decisions=None):
    signature=configuration(job);state=decisions or {};decisions=scoped_decisions(job,state)
    frozen=state.get('completed',{}).get(signature)
    if frozen:
        closure=decisions.get('|'.join((signature,'','_season')),{})
        return {**frozen,'frozen':True,'closed':closure.get('action')=='close','state':'done','review':[],'remaining':{}}

    season=job.as_dict()['season'];targets=set();selected={}
    matching=[s for s in schedules if s.get('competition')==job.competition and s.get('season')==season and s.get('kind')=='combined']
    # Audited inventories carry scoped schedules. Do not resurrect qualifying
    # IDs from an older runtime that predated separate competitions.
    scoped=[s for s in matching if s.get('scope_version')==2]
    for schedule in scoped or matching:targets.update(map(str,schedule.get('match_ids',[])))
    known=bool(matching)
    for row in records.values():
        if row.get('namespace')=='sofascore' and row.get('competition')==job.competition and row.get('season')==season:
            selected[str(row['match_id'])]=row
    if not scoped:targets.update(selected)
    missing={};waivers={};candidates=[];counts={r:{'complete':0,'unavailable':0} for r in job.resources}
    for mid in sorted(targets):
        row=selected.get(mid,{});resources=dict(row.get('resources',{}))
        who=row.get('provider_match_ids',{}).get('whoscored')
        if who is not None:
            from .ensembleProgress import job_key
            events=records.get(job_key('whoscored_events',job.competition,season,who),{})
            if events.get('resources',{}).get('whoscored_events')=='complete':resources['whoscored_events']='complete'
        evidence=row.get('evidence',{});attempts=int(evidence.get('attempts',0));unresolved=[];accepted=[];retrying=False
        for name in job.resources:
            decision=decisions.get('|'.join((signature,mid,name)),{})
            if resources.get(name)=='complete':counts[name]['complete']+=1
            elif decision.get('action')=='unavailable':counts[name]['unavailable']+=1;accepted.append(name)
            else:
                unresolved.append(name)
                if decision.get('action')=='retry' and attempts<decision.get('attempts',0)+1:retrying=True
        if accepted:waivers[mid]=accepted
        if unresolved:
            missing[mid]=unresolved
            candidate={'match_id':mid,'label':evidence.get('label') or '', 'date':evidence.get('date'), 'resources':unresolved,
                       'attempts':attempts,'error':evidence.get('error',''),'states':{k:resources.get(k,'pending') for k in unresolved},
                       'retrying':retrying}
            candidates.append(candidate)
    attempted=history.get(job.id,{})
    small=len(missing)<=max(5,math.ceil(len(targets)*.02))
    reviews=[]
    for c in candidates:
        failed=any(s in ('failed','partial','unavailable','deferred') for s in c['states'].values())
        if not c['retrying'] and failed and (c['attempts']>=2 or 'unavailable' in c['states'].values() or small and attempted):reviews.append(c)
    done=known and bool(targets) and not missing
    groups=[]
    for key,label,names in GROUPS:
        names=[r for r in names if r in counts]
        if not names:continue
        total=len(targets)*len(names);complete=sum(counts[r]['complete'] for r in names);waived=sum(counts[r]['unavailable'] for r in names)
        groups.append({'id':key,'label':label,'total':total,'complete':complete,'unavailable':waived,
                       'percent':100*complete/total if total else 0,'resolved_percent':100*(complete+waived)/total if total else 0})
    closure=decisions.get('|'.join((signature,'','_season')),{})
    audit=closure.get('action')=='reopen' and int(attempted.get('review_audit_id',0))<closure['id']
    done=done and not audit
    return {'audit_requested':audit,'audit_id':closure['id'] if audit else 0,'configuration':signature,'schedule_known':known,'known_matches':len(targets),'remaining':missing,
            'remaining_matches':len(missing),'completed_matches':len(targets)-len(missing),'waivers':waivers,
            'groups':groups,'review':reviews,'counts':counts,'done':done,'closed':done and closure.get('action')=='close',
            'state':'done' if done else 'needs_review' if reviews else 'in_progress' if attempted or audit else 'planned',
            'targets':sorted(targets)}
