"""Inspect storage queues or deliver already queued raw files to MSI; never scrape."""
import argparse
import json
from pathlib import Path
import sqlite3

from storage.sqlitePaths import readonly_uri


def queue_status(root):
    root=Path(root);result={'captures':0,'queued_uploads':0}
    for relative,sql,name in (
        ('_system/storage/tasks.sqlite','SELECT count(*) FROM captures','captures'),
        ('_system/storage/outbox/queue.sqlite',"SELECT count(*) FROM queue WHERE state='queued'",'queued_uploads')):
        path=root/relative
        if path.exists():
            with sqlite3.connect(readonly_uri(path),uri=True) as db:result[name]=db.execute(sql).fetchone()[0]
    return result


def drain(root,dataset,worker,settings,*,max_ticks=1000,report=print):
    from collection.storageAgent import StorageAgent
    from collection.storageConfig import path_for
    options=json.loads(path_for(root).read_text(encoding='utf-8'))
    if options.get('role')!='producer' or options.get('worker_id')!=worker:
        raise ValueError('Queued raw delivery must run on its original producer computer')
    agent=StorageAgent(root,worker,settings,dataset_root=dataset,log=report)
    try:
        for _ in range(max_ticks):
            if not any(queue_status(root).values()):return True
            agent.tick();report(json.dumps(agent.status),flush=True)
            if agent.status.get('state')=='sending_elsewhere':return False
        return not any(queue_status(root).values())
    finally:agent.close()


def main(argv=None):
    import config
    from config.workerPlan import load_plan
    from collection.workerIdentity import read_worker_id
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--dataset-root',type=Path,default=config.PROJECT_ROOT/'dataset')
    parser.add_argument('--send-queued',action='store_true')
    args=parser.parse_args(argv)
    print(json.dumps(queue_status(args.data_root)))
    if args.send_queued:
        done=drain(args.data_root,args.dataset_root,read_worker_id(args.data_root),load_plan().storage)
        print('Queued delivery finished.' if done else 'Delivery remains queued; another sender may be active.')
        return 0 if done else 1
    return 0


if __name__=='__main__':raise SystemExit(main())
