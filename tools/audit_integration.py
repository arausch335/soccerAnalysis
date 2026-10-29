"""Prove MSI holds the scraped resources reported by the other workers, offline."""
import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sqlite3

from storage.pandasStore import atomic_json
from storage.sqlitePaths import readonly_uri

FORMAT='soccer-integration-audit-v1'
EXPECTED='soccer-expected-holdings-v1'


def now():return datetime.now(timezone.utc)


def export_expected(root,output,*,max_age_hours=24):
    """Export authenticated Main reports, never tokens or raw response bodies."""
    path=Path(root)/'_system/workers/reporting.sqlite'
    workers=[]
    with sqlite3.connect(readonly_uri(path),uri=True) as db:
        for name,seen,body,runtime in db.execute('SELECT worker,last_seen,inventory,runtime FROM workers'):
            if name=='msi':continue
            inventory=json.loads(body or '{}');state=json.loads(runtime or '{}')
            workers.append({'worker':name,'last_seen':seen,'records':inventory.get('records',{}),
                            'queued_uploads':state.get('storage',{}).get('queued',0)})
    result={'format':EXPECTED,'generated_at':now().isoformat(),'workers':workers,
            'max_age_hours':max_age_hours}
    atomic_json(output,result,indent=2)
    return result


def inspect_queues(root,ensemble):
    root=Path(root);problems=[];counts={}
    path=root/'_system/storage/hub/archive.sqlite'
    if not path.exists():problems.append('MSI archive ledger is missing')
    else:
        with sqlite3.connect(readonly_uri(path),uri=True) as db:
            counts=dict(db.execute('SELECT state,count(*) FROM uploads GROUP BY state'))
        if counts.get('pending',0):problems.append('Uploads are still incomplete')
        if counts.get('received',0):problems.append('Received uploads have not been incorporated')
        if counts.get('quarantined',0):problems.append('Uploads set aside for review have not been incorporated')
    staged=list(Path(ensemble).rglob('transfer-manifest.json')) if Path(ensemble).exists() else []
    if staged:problems.append('Portable imports remain staged')
    # An unfinished portable copy may only have the marker, not its final manifest.
    markers=list(Path(ensemble).rglob('.soccer-scraper.json')) if Path(ensemble).exists() else []
    if markers and not staged:problems.append('Unfinished portable copy remains staged')
    return counts,problems


def audit(root,ensemble,expected_path,*,max_age_hours=24,scan=None):
    from collection.workerIdentity import read_worker_id
    root=Path(root).resolve();counts,problems=inspect_queues(root,ensemble)
    if read_worker_id(root)!='msi':problems.append('Run the integration proof on MSI')
    if expected_path is None:
        expected={};problems.append('A fresh expected-holdings export from Main is required')
    else:
        expected=json.loads(Path(expected_path).read_text(encoding='utf-8'))
        if expected.get('format')!=EXPECTED:raise ValueError('Invalid expected-holdings export')
        if now()-datetime.fromisoformat(expected['generated_at'])>timedelta(hours=max_age_hours):
            problems.append('Expected-holdings export is stale')
    wanted={};sources=[]
    for worker in expected.get('workers',[]):
        seen=worker.get('last_seen')
        if not seen or now()-datetime.fromisoformat(seen)>timedelta(hours=max_age_hours):
            problems.append('Stale worker report: '+worker['worker'])
        if worker.get('queued_uploads'):problems.append('Worker still has queued uploads: '+worker['worker'])
        sources.append(worker['worker'])
        for key,row in worker.get('records',{}).items():
            entry=wanted.setdefault(key,{'capabilities':set(),'resources':set()})
            entry['capabilities'].update(row.get('capabilities',[]))
            entry['resources'].update(k for k,v in row.get('resources',{}).items() if v=='complete')
    if not wanted:problems.append('No expected holdings supplied; completeness is unproven')
    if scan is None:
        from collection.ensembleProgress import PortableProgress
        inventory=PortableProgress(None,root);inventory.scan(full=True,cache=False)
        local=inventory.local_records
    else:local=scan(root)
    missing=[]
    for key,required in wanted.items():
        row=local.get(key,{})
        capabilities=required['capabilities']-set(row.get('capabilities',[]))
        resources=required['resources']-{k for k,v in row.get('resources',{}).items() if v=='complete'}
        if key not in local or capabilities or resources:
            missing.append({'key':key,'capabilities':sorted(capabilities),'resources':sorted(resources)})
    if missing:problems.append('MSI lacks some independently validated reported resources')
    return {'format':FORMAT,'checked_at':now().isoformat(),'data_root':str(root),'ensemble_root':str(Path(ensemble).resolve()),
            'state':'ready' if not problems else 'not_ready','expected_workers':sources,
            'expected_records':len(wanted),'validated_local_records':len(local),'archive':counts,
            'problems':problems,'missing':missing,
            'scope':'All reported scraped holdings; not proof that every scheduled match was scraped'}


def main(argv=None):
    import config
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--ensemble-root',type=Path,default=config.PROJECT_ROOT/'ensemble_data')
    parser.add_argument('--export-expected',type=Path)
    parser.add_argument('--expected-inventory',type=Path)
    parser.add_argument('--output',type=Path,default=Path('integration-audit.json'))
    parser.add_argument('--max-age-hours',type=int,default=24)
    args=parser.parse_args(argv)
    if args.export_expected:
        result=export_expected(args.data_root,args.export_expected,max_age_hours=args.max_age_hours)
        print('Exported expected holdings for',len(result['workers']),'workers');return 0
    result=audit(args.data_root,args.ensemble_root,args.expected_inventory,max_age_hours=args.max_age_hours)
    atomic_json(args.output,result,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='missing'},indent=2))
    return 0 if result['state']=='ready' else 2


if __name__=='__main__':raise SystemExit(main())
