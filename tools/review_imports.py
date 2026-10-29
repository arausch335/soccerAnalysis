"""Inspect preserved MSI uploads that were set aside, or retry one after review."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    import config
    from collection.storageServer import Archive
    from collection.workerIdentity import read_worker_id
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--show',metavar='UPLOAD_ID',help='Show preserved file locations for one upload')
    action.add_argument('--retry',metavar='UPLOAD_ID',help='Return a reviewed upload to the import queue')
    args=parser.parse_args(argv)
    if read_worker_id(args.data_root)!='msi':parser.error('Run this command on MSI')
    archive=Archive(args.data_root)
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
