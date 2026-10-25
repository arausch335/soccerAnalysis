"""Reconcile Main's received fixture/concordance IDs without rescraping."""
import argparse
import json
from pathlib import Path
import config
from fixtures.identityRefresh import reconcile_exports


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--dataset-root',type=Path,default=config.DATASET_ROOT)
    parser.add_argument('--refresh-reep',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    if args.refresh_reep:
        if args.dry_run:parser.error('Use --dry-run with the installed index; refreshing installs a new index')
        from providers.reep import refresh
        refresh(args.data_root,args.dataset_root)
    report=reconcile_exports(args.data_root,args.dataset_root,dry_run=args.dry_run)
    print(json.dumps(report,indent=2))
    return report


if __name__=='__main__':main()
