"""Export, verify or install a full fixture catalogue for MSI ensemble ingestion."""
import argparse
from pathlib import Path
import config
from collection.fixtureTransfer import export_bundle,verify,install_bundle


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-root',type=Path,default=config.DATASET_ROOT)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--export',type=Path,metavar='NEW_FOLDER')
    action.add_argument('--verify',type=Path,metavar='BUNDLE')
    action.add_argument('--import',dest='source',type=Path,metavar='BUNDLE')
    args=parser.parse_args(argv)
    if args.export:print('Verified fixture transfer ready:',args.export,'fixtures:',export_bundle(args.dataset_root,args.export))
    elif args.verify:print('Verified fixture transfer:',verify(args.verify)['fixtures'],'fixtures')
    else:
        changed=install_bundle(args.source,args.dataset_root)
        print('Fixture baseline installed.' if changed else 'Fixture baseline already installed.')
        print('The next package build will merge the full history with local saved fixtures.')
    return 0


if __name__=='__main__':raise SystemExit(main())
