"""Main grants one durable season claim; disconnected workers never lose ownership."""
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3

from storage.pandasStore import atomic_json
from .workerRuntime import now


def receipt_path(root):return Path(root)/'_system/workers/active-assignment.json'


class JobControl:
    def __init__(self,root,plan_path):
        self.plan_path=Path(plan_path);self.path=Path(root)/'_system/workers/assignments.sqlite';self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS claims(season TEXT PRIMARY KEY,worker TEXT,instance TEXT,job TEXT,token TEXT,claimed_at TEXT)');db.commit()
    def connect(self):return closing(sqlite3.connect(self.path,timeout=10))

    def instructions(self,worker,store):
        from config.workerPlan import load_plan
        from .workInstructions import compile_instructions
        with store.lock,store.connect() as db:
            reports=[{'inventory':json.loads(row['inventory']) if row['inventory'] else {},
                      'runtime':json.loads(row['runtime']) if row['runtime'] else {}}
                     for row in db.execute('SELECT inventory,runtime FROM workers')]
        from .jobCompletion import CompletionStore
        plan=load_plan(self.plan_path);completed=CompletionStore(self.path.parent.parent.parent);decisions=completed.snapshot()
        value=compile_instructions(plan,worker,reports,decisions)
        for job in plan.for_worker(worker):completed.freeze(job,value['jobs'][job.id],decisions['revision'])
        return value

    def command(self,worker,value):
        from config.workerPlan import load_plan
        from .storageAgent import season_for
        operation=value.get('operation');instance=value.get('instance')
        if not isinstance(instance,str) or not instance or len(instance)>128:raise ValueError('Missing worker instance')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if operation=='release':
                token=value.get('token')
                if not isinstance(token,str) or len(token)>128:raise ValueError('Invalid assignment receipt')
                db.execute('DELETE FROM claims WHERE worker=? AND instance=? AND token=?',(worker,instance,token));db.commit()
                return {'state':'released'}
            if operation!='claim':raise ValueError('Unknown assignment operation')
            plan=load_plan(self.plan_path)
            if value.get('revision')!=plan.revision:return {'state':'waiting','message':'Shared plan differs from Main; wait for code and plan update'}
            job=next((j for j in plan.for_worker(worker) if j.id==value.get('job_id')),None)
            if job is None:return {'state':'waiting','message':'Main has not assigned this job to this worker'}
            season=season_for(job);row=db.execute('SELECT worker,instance,job,token FROM claims WHERE season=?',(season,)).fetchone()
            if row and row[:2]!=(worker,instance):return {'state':'waiting','message':'Season still owned by another worker; waiting for its saved-job handoff'}
            token=value.get('token')
            if not isinstance(token,str) or not 32<=len(token)<=128:raise ValueError('Missing assignment claim token')
            if row and row[3]!=token:return {'state':'waiting','message':'A preceding worker run still owns this season; restore its receipt or clear the claim after stopping it'}
            db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?,?)',(season,worker,instance,job.id,token,now()));db.commit()
            return {'state':'granted','token':token,'job_id':job.id,'season':season,'revision':plan.revision}


class AssignmentClient:
    def __init__(self,root,reporter,plan):
        self.root=Path(root);self.reporter=reporter;self.plan=plan
        self.status={'state':'waiting','coordinator':plan.coordinator,'message':'Waiting for instructions from Main'}
    def request(self,value):
        if not self.reporter.url:raise OSError('Reporting not configured')
        value={**value,'instance':self.reporter.identity['instance_id']}
        return self.reporter.request('/v1/assignments',value)
    def release(self):
        path=receipt_path(self.root)
        if path.exists():
            receipt=json.loads(path.read_text(encoding='utf-8'));self.request({'operation':'release','token':receipt['token']});path.unlink()
            self.status={'state':'released','coordinator':self.plan.coordinator,'message':'Previous season ownership released'}
    def claim(self,job):
        try:
            # Never start another job while the preceding ownership receipt is unresolved.
            self.release()
            if not getattr(self.reporter,'url',None):raise OSError('Reporting not configured')
            self.reporter.pull()
            token=secrets.token_urlsafe(40)
            # Save before requesting: even a lost grant response can be safely released on retry.
            atomic_json(receipt_path(self.root),{'token':token,'job_id':job.id});receipt_path(self.root).chmod(0o600)
            result=self.request({'operation':'claim','job_id':job.id,'revision':self.plan.revision,'token':token})
            if result.get('state')!='granted':self.release()
            else:
                from .workInstructions import install
                install(self.root,result['instructions'],worker=self.reporter.worker,revision=self.plan.revision)
            self.status={k:v for k,v in result.items() if k not in ('token','instructions')}
            instruction=result.get('instructions',{}).get('jobs',{}).get(job.id,{})
            self.status['remaining_matches']=instruction.get('remaining_matches')
            self.status['schedule_known']=instruction.get('schedule_known',False)
            self.status['instruction_revision']=instruction.get('revision');self.status['coordinator']=self.plan.coordinator
            return result.get('state')=='granted'
        except (OSError,ValueError,KeyError,sqlite3.Error):
            self.status={'state':'waiting','coordinator':self.plan.coordinator,'message':'Main unavailable; saved work remains intact; new jobs wait for authorization'}
            return False


def main(argv=None):
    import argparse
    import config
    from .reporting import load_local
    parser=argparse.ArgumentParser(description='Inspect Main assignment ownership; clear only stopped workers')
    parser.add_argument('operation',choices=['status','clear'])
    parser.add_argument('--worker-id')
    parser.add_argument('--season',help='Canonical league/YYYY-YYYY')
    parser.add_argument('--confirm-stopped',action='store_true')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--plan',type=Path,default=config.PROJECT_ROOT/'config/workers.yaml')
    args=parser.parse_args(argv)
    local=load_local(args.data_root)
    if not local or local['mode']!='server':parser.error('Run assignment administration on Main')
    control=JobControl(args.data_root,args.plan)
    with control.connect() as db:
        if args.operation=='clear':
            if not args.worker_id or not args.season or not args.confirm_stopped:parser.error('Clear requires --worker-id, --season and --confirm-stopped')
            db.execute('DELETE FROM claims WHERE worker=? AND season=?',(args.worker_id,args.season));db.commit()
        for row in db.execute('SELECT season,worker,claimed_at FROM claims ORDER BY season'):print(' | '.join(row))
    return 0


if __name__=='__main__':raise SystemExit(main())
