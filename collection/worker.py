"""Run the Git-shared assignment for one computer, resuming saved collector jobs."""
from storage.layout import system_path,organize_data_root,full_path
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import webbrowser

from filelock import FileLock
from .inventoryMonitor import InventoryMonitor
import config
from config.workerPlan import load_plan
from .ensemble import ensemble_session
from .codeUpdates import CodeUpdates
from .progress import observe_progress
from .workerIdentity import read_worker_id, ensure_identity
from .ensembleProgress import shared_progress, read_progress
from storage.pandasStore import atomic_json
from .workerRuntime import Runtime, now
from .reporting import load_local, ReportingServer, Reporter, capture_logs

RESTART=75


def fingerprint(job,settings):
    return hashlib.sha256(b'worker-reporting-v3-qualified-scopes\0'+settings+json.dumps(job.as_dict(),sort_keys=True).encode()).hexdigest()


def prepare_extension(root,jobs,monitor):
    """Publish the local code early; no browser command or connection wait yet."""
    if monitor:return
    if not any(set(job.collection_types)&{'sofascore','combined'} for job in jobs):return
    from providers.transports.bridge import acquire_bridge
    bridge,_=acquire_bridge(connect_timeout=config.TRANSPORT['extension']['manual_wait_seconds'],
                            diagnostics_dir=system_path(root,'extension_diagnostics')/'sofascore')
    bridge.start()
    return bridge


def run(argv=None,*,runner=None,session=ensemble_session,sync_factory=CodeUpdates,stop=None,clock=time.monotonic):
    parser=argparse.ArgumentParser(description='Shared worker plan and collection dashboard')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--worker',action='store_true',help='Scrape this computer’s assignments')
    modes.add_argument('--monitor',action='store_true',help='Synchronize progress without scraping')
    parser.add_argument('--with-dashboard',action='store_true',help='Show a dashboard on a worker')
    parser.add_argument('--worker-id',help='Use an ID defined in config/workers.yaml; saved with local data')
    parser.add_argument('--plan',type=Path,default=config.PROJECT_ROOT/'config/workers.yaml')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--ensemble-root',type=Path,default=config.PROJECT_ROOT/'ensemble_data')
    parser.add_argument('--dashboard',action='store_true',help='Dashboard only; do not collect or sync Git')
    parser.add_argument('--no-dashboard',action='store_true')
    parser.add_argument('--once',action='store_true',help='Run the pending plan once, then exit')
    parser.add_argument('--retry',action='store_true',help='Revisit previously finished/paused assignments using saved checkpoints')
    options=parser.parse_args(argv);plan=load_plan(options.plan)
    root=full_path(options.data_root);organize_data_root(root);ensemble=full_path(options.ensemble_root)
    worker=options.worker_id or read_worker_id(root)
    if not options.dashboard:plan.for_worker(worker)
    info=plan.workers.get(worker,{})
    monitor=options.monitor or (not options.worker and info.get('role')=='monitor')
    stop=stop or threading.Event()
    with ExitStack() as stack:
        from providers.identityReview import identity_scope
        stack.enter_context(identity_scope(root))
        if ((plan.dashboard['enabled'] and info.get('dashboard',False) or options.with_dashboard) and not options.no_dashboard) or options.dashboard:
            from dashboard.server import start_server
            server=start_server(root,ensemble,options.plan,worker,plan.dashboard['port'])
            stack.callback(server.close)
            print(f'Collection dashboard: {server.url}',flush=True)
            if plan.dashboard['open_browser']:webbrowser.open(server.url)
        if options.dashboard:
            try:
                while not stop.wait(1):pass
            except KeyboardInterrupt:pass
            return 0
        if root.exists() and any(root.iterdir()) and not (root/'.soccer-scraper.json').exists():
            raise ValueError('Worker data root must be a marked scraper directory')
        root.mkdir(parents=True,exist_ok=True)
        stack.enter_context(FileLock(str(system_path(root,'.worker.lock')),timeout=0))
        with FileLock(str(system_path(root,'.collector.lock')),timeout=0):
            if not (root/'.soccer-scraper.json').exists():atomic_json(root/'.soccer-scraper.json',{'format':1})
            inventory=ensemble/'workers'/(worker+'.json')
            historical=ensemble/'reporting'/'workers'/(worker+'.json')
            if not inventory.exists() and historical.exists():
                atomic_json(inventory,read_progress(historical))
            identity_file=system_path(root,'identity.json')
            previous=json.loads(identity_file.read_text(encoding='utf-8')) if identity_file.exists() else {}
            if inventory.exists() and read_progress(inventory).get('instance_id')!=previous.get('instance_id'):
                raise ValueError('This worker ID belongs to another data folder; use the original data folder or choose a new worker ID')
            ensure_identity(root,worker)
        from providers.transports.bridge import extension_scope
        stack.enter_context(extension_scope())
        extension_bridge=prepare_extension(root,plan.for_worker(worker),monitor)
        state=Runtime(root,ensemble,worker)
        if options.retry:state.value['jobs']={}
        reporting=None
        local_reporting=load_local(root) if plan.reporting['enabled'] else None
        if local_reporting:
            client_options=local_reporting
            if local_reporting['mode']=='server':
                receiver=ReportingServer(root,ensemble,local_reporting,storage_plan=plan.storage,workers=list(plan.workers),command_plan=options.plan if plan.storage['enabled'] else None).start();stack.callback(receiver.close)
                print(f"Worker reporting listening on {local_reporting['bind']}:{local_reporting['port']}",flush=True)
                client_options={'worker_id':worker,'url':f"http://{local_reporting['bind']}:{local_reporting['port']}",
                                'token':local_reporting['tokens'][worker]}
            reporting=Reporter(root,ensemble,worker,client_options,plan.reporting['heartbeat_seconds']).start()
            stack.callback(reporting.close)
            stack.enter_context(capture_logs(reporting))
            print('Direct reporting enabled: saved matches and logs queue locally and send in the background.',flush=True)
        elif plan.reporting['enabled']:
            client_options={'worker_id':worker,'url':None,'token':''}
            reporting=Reporter(root,ensemble,worker,client_options,plan.reporting['heartbeat_seconds']).start()
            stack.callback(reporting.close);stack.enter_context(capture_logs(reporting))
            print('Reporting setup pending: progress and logs queue locally. Configure Tailscale and see docs/WORKERS.md.',flush=True)
        sync=sync_factory(config.PROJECT_ROOT,worker,plan.git).start();stack.callback(sync.close)
        def publish(*,inventory=True):
            if reporting:
                state.save(reporting=reporting.status)
                reporting.enqueue(inventory=inventory)
        control=None
        if plan.storage['enabled'] and reporting and not monitor:
            from .jobControl import AssignmentClient
            control=AssignmentClient(root,reporting,plan)
        storage=None
        if plan.storage['enabled']:
            from .storageAgent import StorageAgent
            storage=StorageAgent(root,worker,plan.storage,dataset_root=config.DATASET_ROOT,ensemble=ensemble,
                log=(lambda message:reporting.log(message,'ERROR' if '[Storage ERROR]' in message else 'INFO')) if reporting else print).start()
            stack.callback(storage.close)
            storage.backfill(plan.for_worker(worker),state.value['jobs'])
        initialized=False;progress=None;last_boundary=0;retry_extension_job=None
        settings=config.CONFIG_FILE.read_bytes()
        state.save(state='starting',role='monitor' if monitor else 'worker',plan_revision=plan.revision,git=sync.status,process_id=os.getpid())
        inventory_watch=None
        if not options.once:
            inventory_watch=InventoryMonitor(root,ensemble,worker,
                on_change=(lambda:reporting.enqueue()) if reporting else None).start()
            stack.callback(inventory_watch.close)
        if runner is None:
            from main import _main
            runner=_main
        try:
            while not stop.is_set():
                if inventory_watch:state.save(inventory=inventory_watch.status)
                if control:
                    state.save(control=control.status)
                    # Release an earlier run's durable claim even if this worker is now unassigned.
                    try:
                        control.release();state.save(control=control.status)
                    except (OSError,ValueError,KeyError):pass
                if storage:
                    state.save(storage=storage.status)
                # Code updates are applied between collection jobs.
                changed=[]
                if clock()-last_boundary>=plan.git['poll_seconds'] or state.value['state'] in ('starting','between_jobs') or sync.status.get('state')=='update_pending':
                    changed=sync.boundary();last_boundary=clock()
                    if progress is not None:progress.records=shared_progress(progress.path,local_worker=worker,trusted=control is not None)
                if changed:
                    state.save(state='restart_required',git=sync.status,current=None)
                    return RESTART if plan.git['auto_restart'] else 3
                current_plan=load_plan(options.plan)
                if current_plan.revision!=plan.revision or config.CONFIG_FILE.read_bytes()!=settings:
                    state.save(state='restart_required',current=None)
                    return RESTART if current_plan.git['auto_restart'] else 3
                from providers.identityReview import revision
                identity_revision=revision(root)
                from .workInstructions import requested_again,current_commands
                recheck=requested_again(root,worker,plan.revision,state.value['jobs']) if control else set()
                commands=current_commands(root,worker,plan.revision) if control else {}
                for saved_job in plan.for_worker(worker):
                    if commands.get(saved_job.id,{}).get('done'):
                        completed=commands[saved_job.id]
                        prior=state.value['jobs'].get(saved_job.id,{})
                        if prior.get('state')!='done' or prior.get('configuration')!=completed['configuration']:
                            from .storageAgent import season_for
                            directory=root/season_for(saved_job)
                            if (directory/'job.json').exists():
                                atomic_json(directory/'completion.json',{'configuration':completed['configuration'],
                                    'done':True,'closed':completed['closed'],'counts':completed['counts'],
                                    'reviewed_unavailable':completed['waivers'],'coordinator':plan.coordinator,'at':now()})
                                if storage:storage.finished(saved_job,0)
                        state.value['jobs'][saved_job.id]={**state.value['jobs'].get(saved_job.id,{}),'state':'done','exit_code':0,'configuration':commands[saved_job.id]['configuration']}
                pending=[job for job in ([] if monitor else plan.for_worker(worker))
                         if not commands.get(job.id,{}).get('done') and (job.id in recheck or state.value['jobs'].get(job.id,{}).get('fingerprint')!=fingerprint(job,settings)
                         or (identity_revision and state.value['jobs'].get(job.id,{}).get('exit_code',0) not in (0,130)
                             and state.value['jobs'].get(job.id,{}).get('identity_revision','')!=identity_revision))]
                if pending and not initialized:
                    state.save(state='validating_inventory',git=sync.status)
                    progress=stack.enter_context(session(root,ensemble,worker_id=worker,background=True))
                    if progress is None:raise ValueError('Use a marked scraper data root')
                    progress.trusted_only=control is not None
                    progress.plan_revision=plan.revision
                    initialized=True
                    continue
                if pending and extension_bridge is not None and not extension_bridge.ready:
                    if state.value['state']!='waiting_for_extension':
                        state.save(state='waiting_for_extension',current=None)
                        print('Waiting for Soccer Collector to connect. Paste the printed code in Chrome; scraping and identity lookups will start after connection.',flush=True)
                        publish(inventory=False)
                    stop.wait(1);continue
                if retry_extension_job:
                    state.value['jobs'].pop(retry_extension_job,None);retry_extension_job=None
                    continue
                if not pending:
                    if not monitor and not options.once:
                        from providers.playerProfiles import investigate_queue
                        if investigate_queue(root):
                            publish(inventory=False)
                            continue
                    assigned=plan.for_worker(worker)
                    all_done=bool(assigned) and all(state.value['jobs'].get(j.id,{}).get('state')=='done' for j in assigned)
                    state.save(state='monitoring' if monitor else 'done' if all_done else 'paused' if any(state.value['jobs'].get(j.id,{}).get('exit_code',0) for j in assigned) else 'idle' if assigned else 'unassigned',current=None,git=sync.status)
                    if reporting:state.save(reporting=reporting.status)
                    if options.once:return max((state.value['jobs'].get(j.id,{}).get('exit_code',0) for j in plan.for_worker(worker)),default=0)
                    stop.wait(min(5,plan.git['poll_seconds']));continue
                job=pending[0]
                if control and not control.claim(job):
                    state.save(state='waiting_for_instructions',control=control.status,current=None)
                    publish(inventory=False)
                    if options.once:return 2
                    stop.wait(5);continue
                if control:state.save(control=control.status)
                if control and progress is not None:progress.records=shared_progress(progress.path,local_worker=worker,trusted=control is not None)
                state.begin_job(job.as_dict())
                if reporting:reporting.log(f'[Season started] {job.competition} {job.as_dict()["season"]}')
                publish()
                def observe(value):
                    state.value['git']=sync.status
                    if storage:state.value['storage']=storage.status
                    state.event(value)
                    if storage and worker!=plan.storage['hub'] and worker not in plan.storage['consumers'] and value['event']=='match_saved' and not value.get('cached_match'):
                        from .storageAgent import season_for
                        storage.capture(season_for(job),value['match_id'])
                        state.save(storage=storage.status)
                    if reporting:
                        if progress is not None and value['event']=='match_started':progress.records=shared_progress(progress.path,local_worker=worker,trusted=control is not None)
                        if value['event'] in ('schedule','match_saved','job_finished','provider_started'):publish(inventory=value['event'] in ('match_saved','job_finished'))
                        return
                # Capture the revision before running: approvals received mid-job also get a later backfill.
                attempted_identity_revision=identity_revision
                with observe_progress(observe):code=runner(job.args(root)+(['--no-build-dataset','--defer-exports'] if storage else []))
                if storage:storage.finished(job,code)
                if code and extension_bridge is not None and not extension_bridge.ready:
                    retry_extension_job=job.id
                if reporting:
                    milestone='completed' if code==0 else 'paused'
                    reporting.log(f'[Season {milestone}] {job.competition} {job.as_dict()["season"]}', 'INFO' if code==0 else 'WARNING')
                state.value['jobs'][job.id]={'fingerprint':fingerprint(job,settings),'exit_code':code,'identity_revision':attempted_identity_revision,
                                             'state':'finished' if code==0 else 'paused',
                                             'instruction_revision':control.status.get('instruction_revision') if control else None}
                state.save(state='between_jobs',job_ended_at=now(),current=None,git=sync.status)
                if reporting:publish()
                if code==130:
                    state.save(state='stopped')
                    return 130
        except KeyboardInterrupt:
            state.save(state='stopped',job_ended_at=state.value.get('job_ended_at') or now(),current=None,git=sync.status)
            publish()
            return 130
        except BaseException:
            if reporting:
                import traceback
                traceback.print_exc()
            state.save(state='paused',job_ended_at=state.value.get('job_ended_at') or now(),current=None,git=sync.status)
            publish()
            raise
        finally:
            state.save(state=state.value['state'],git=sync.status)
            if reporting:publish()
    return 0


def entrypoint(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    try:code=run(args)
    except (OSError,ValueError,TimeoutError) as exc:
        print(f'Worker paused: {exc}',flush=True);return 2
    if code==RESTART:
        print('Saved job closed. Restarting with updated code and settings.',flush=True)
        os.execv(sys.executable,[sys.executable,str(config.PROJECT_ROOT/'main.py'),*[arg for arg in args if arg!='--retry']])
    return code
