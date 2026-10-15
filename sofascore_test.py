"""Run one live Sofascore match check from an IDE or terminal."""
import argparse
from pathlib import Path

from filelock import FileLock, Timeout
from providers.safeClient import SafeSofaScoreClient
from providers.sofaScoreProbe import DEFAULT_MATCH_ID, probe_match
from storage.pandasStore import atomic_json


MATCH_ID = DEFAULT_MATCH_ID  # Change to another Sofascore match ID if needed.
DATA_ROOT = Path(__file__).resolve().parent/'data'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--match-id',type=int,default=MATCH_ID)
    parser.add_argument('--data-root',type=Path,default=DATA_ROOT)
    args = parser.parse_args(argv)
    if args.match_id < 1:
        parser.error('match ID must be positive')
    root = args.data_root.resolve()
    root.mkdir(parents=True,exist_ok=True)
    if not any(root.iterdir()):
        atomic_json(root/'.soccer-scraper.json',{'format':1})
    try:
        with FileLock(str(root/'.collector.lock'),timeout=0):
            with SafeSofaScoreClient(root,'sofascore') as client:
                ok,message = probe_match(client,args.match_id)
                print(message)
                return 0 if ok else 2
    except Timeout:
        print('NOT TESTED: another collector is using this data root; no request sent.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
