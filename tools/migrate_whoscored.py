"""Migrate legacy WhoScored folders and refresh this worker's inventory offline."""
import argparse
import json
from pathlib import Path

from filelock import FileLock, Timeout
import config
from collection.whoScoredData import migrate_legacy
from collection.ensembleProgress import PortableProgress


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--ensemble-root',type=Path,default=config.PROJECT_ROOT/'ensemble_data')
    args=parser.parse_args(argv)
    root=args.data_root.expanduser().resolve();ensemble=args.ensemble_root.expanduser().resolve()
    try:
        if root==ensemble or root in ensemble.parents or ensemble in root.parents:
            raise ValueError('Data and ensemble roots must be separate directories')
        marker=root/'.soccer-scraper.json'
        if not marker.exists() or json.loads(marker.read_text(encoding='utf-8')).get('format')!=1:
            raise ValueError('Choose an existing marked scraper data folder')
        with FileLock(str(root/'.collector.lock'),timeout=0):
            count=migrate_legacy(root)
            progress=PortableProgress(ensemble/'provenance.json',root)
            progress.scan()
            print(f'WhoScored migration complete: {count} legacy collections moved; worker inventory refreshed.')
        return 0
    except Timeout:
        print('WhoScored migration paused: stop the running scraper, then run this command again.')
        return 2
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(f'WhoScored migration paused: {exc}. Unverified source folders are retained.')
        return 2


if __name__=='__main__':raise SystemExit(main())
