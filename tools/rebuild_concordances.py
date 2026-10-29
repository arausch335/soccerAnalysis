"""Offline full/scraped concordance rebuild after MSI integration verification."""
import argparse
import json
from pathlib import Path


def publish(root,dataset,*,worker='msi'):
    """Publish compiled tables through the existing MSI consumer channel."""
    from collection.storageServer import Archive
    from collection.storageAgent import dataset_files
    from collection.storageTransfer import FORMAT,digest
    from concordance.build import validate_bundle
    from collection.workerIdentity import read_worker_id
    if worker!='msi' or read_worker_id(root)!='msi':raise ValueError('Publish this concordance rebuild from MSI')
    check=validate_bundle(Path(dataset)/'concordance')
    if not check['ready_for_R']:raise ValueError('An unconfirmed preview cannot be published as ready for R')
    files=dataset_files(dataset)
    manifest={'format':FORMAT,'kind':'dataset','worker':worker,'files':[
        {'path':p,'size':v.stat().st_size,'sha256':digest(v)} for p,v in sorted(files.items())]}
    Archive(root).publish(manifest,files)


def main(argv=None):
    import config
    from concordance.build import rebuild,validate_bundle
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--dataset-root',type=Path,default=config.DATASET_ROOT)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--integration-report',type=Path)
    parser.add_argument('--allow-unconfirmed',action='store_true',help='Local preview only; ready_for_R stays false')
    parser.add_argument('--validate-only',action='store_true')
    parser.add_argument('--publish',action='store_true',help='Publish ready compiled tables from MSI to Main')
    args=parser.parse_args(argv)
    if args.validate_only:
        result=validate_bundle(args.output or args.dataset_root/'concordance')
    else:
        result=rebuild(args.data_root,args.dataset_root,output=args.output,
            integration_report=args.integration_report,allow_unconfirmed=args.allow_unconfirmed)
    if args.publish:
        if args.output and args.output.resolve()!=(args.dataset_root/'concordance').resolve():
            parser.error('Publishing requires the standard dataset/concordance output')
        publish(args.data_root,args.dataset_root)
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
