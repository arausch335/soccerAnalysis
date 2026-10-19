"""Check one match per provider, using config/settings.yaml and shared cooldowns."""
import argparse
from pathlib import Path

from filelock import FileLock, Timeout
import config
from providers.safeClient import SafeSofaScoreClient, SafeWhoScoredClient, SafeFotMobClient
from providers.providerProbe import LABELS, probe_provider
from providers.whoScoredTransport import create_whoscored_client
from storage.pandasStore import atomic_json


CLIENTS = {'sofascore':SafeSofaScoreClient,'whoscored':create_whoscored_client,'fotmob':SafeFotMobClient}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers',nargs='+',choices=LABELS,default=config.API_TEST_PROVIDERS)
    parser.add_argument('--match-id',type=int,help='Override the match ID when testing one provider')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--whoscored-transport',choices=('http','auto','browser','extension'),default=config.WHOSCORED_ACCESS)
    args = parser.parse_args(argv)
    providers = list(dict.fromkeys(args.providers))
    if args.match_id is not None and (args.match_id < 1 or len(providers) != 1):
        parser.error('--match-id requires a positive ID and exactly one provider')
    root = args.data_root.resolve()
    root.mkdir(parents=True,exist_ok=True)
    if not any(root.iterdir()):
        atomic_json(root/'.soccer-scraper.json',{'format':1})
    try:
        with FileLock(str(root/'.collector.lock'),timeout=0):
            results = []
            for provider in providers:
                mid = args.match_id if args.match_id is not None else config.API_TEST_MATCH_IDS[provider]
                with CLIENTS[provider](root,provider,**({'mode':args.whoscored_transport} if provider=='whoscored' else {})) as client:
                    ok,message = probe_provider(client,provider,mid)
                print(message,flush=True)
                results.append(ok)
            return 0 if all(results) else 2
    except Timeout:
        print('NOT TESTED: another collector is using this data root; no request sent.')
        return 2
    except KeyboardInterrupt:
        print('Interrupted; completed provider checks and cooldowns are retained.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
