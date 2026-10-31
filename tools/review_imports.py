"""Inspect preserved MSI uploads that were set aside, or retry one after review."""
import argparse
import json
from pathlib import Path


def retry_identity_matches(root,archive):
    """Retry only held team checks now supported by published provider IDs."""
    from providers.reep import index
    bridges=index(root);retried=[];held=[]
    for row in archive.diagnostics()['quarantined_uploads']:
        comparison=row.get('identity_comparison') or {}
        checkpoints=comparison.get('checkpoints',[])
        valid='team differs from the checkpoint' in (row.get('error') or '') and len(checkpoints)==1
        if valid:
            checkpoint=checkpoints[0];provider=checkpoint.get('provider','sofascore')
            valid=bool(comparison.get('date')) and checkpoint.get('date')==comparison['date']
            for side in ('home','away'):
                baseline=checkpoint.get('teams',{}).get(side,{}).get('id')
                peer=comparison.get('teams',{}).get(side,{}).get('id')
                valid=valid and baseline is not None and peer is not None and bridges.same(
                    'team',provider,str(baseline),'whoscored',str(peer)) is True
        if valid:
            archive.retry(row['id']);retried.append(row['id'])
        else:held.append(row['id'])
    return {'retried':retried,'still_held':held,'reep_version':bridges.metadata.get('version'),
            'note':'Retried uploads undergo the full import validation again; no team checks are bypassed.'}


def main(argv=None):
    import config
    from collection.storageServer import Archive
    from collection.workerIdentity import read_worker_id
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--show',metavar='UPLOAD_ID',help='Show preserved file locations for one upload')
    action.add_argument('--retry',metavar='UPLOAD_ID',help='Return a reviewed upload to the import queue')
    action.add_argument('--retry-matching-identities',action='store_true',help='Retry held team errors only when current Reep IDs verify both teams and the fixture date agrees')
    args=parser.parse_args(argv)
    if read_worker_id(args.data_root)!='msi':parser.error('Run this command on MSI')
    archive=Archive(args.data_root)
    if args.retry_matching_identities:
        print(json.dumps(retry_identity_matches(args.data_root,archive),indent=2));return 0
    if args.retry:
        archive.retry(args.retry)
        print('Upload returned to the import queue:',args.retry)
        return 0
    held=archive.quarantined()
    if args.show:
        row=next((r for r in held if r['id']==args.show),None)
        if row is None:parser.error('Upload is not set aside for review')
        manifest,_=archive.upload(row['worker'],row['id'])
        row['files']=[{**f,'preserved_at':str(archive.objects/f['sha256'])} for f in manifest['files']]
        held=[row]
    print(json.dumps(held,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
