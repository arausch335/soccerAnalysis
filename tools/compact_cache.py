"""Preview/reclaim duplicate transport caches without deleting unique match data."""
import argparse
from contextlib import ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from filelock import FileLock
from storage.layout import organize_data_root,system_path
from storage.requestCache import RequestCache
from storage.pandasStore import atomic_replace
from providers.cache import ResponseCache
from providers.whoScoredClient import WhoScoredClient
from providers.httpClient import ProviderError
from providers.fotMobClient import without_incident_stream
import re

LEGACY_CACHES=('http_cache','browser_cache','extension_cache')

def normalize(path):
    saved=json.loads(path.read_text(encoding='utf-8'))
    provider=path.parent.name
    if provider not in ('sofascore','fotmob','whoscored'):raise ValueError('Unknown cached provider')
    if 'identity' in saved:
        identity=json.loads(saved['identity'])
        if len(identity)!=4 or urlsplit(identity[0]).hostname not in ('api.sofascore.com','www.sofascore.com','www.fotmob.com','www.whoscored.com'):raise ValueError('Invalid response identity')
        _,endpoint,params,kind=identity
    else:
        endpoint=saved['endpoint'];params=saved.get('params');kind='json'
    if not isinstance(endpoint,str) or kind not in ('json','text') or params is not None and not isinstance(params,dict):raise ValueError('Invalid retired response fields')
    value={**saved,'identity':json.dumps([provider,endpoint,params,kind],sort_keys=True)}
    return provider,endpoint,params,kind,value

def authoritative(root,provider,endpoint,payload,*,directories=None):
    """Only discard an old match response if the season raw data is equivalent."""
    if provider=='whoscored':
        found=re.fullmatch(r'matches/(\d+)/live',endpoint,re.I)
        if not found:return False
        payload=WhoScoredClient.parse_match(payload,int(found[1]));mid=found[1]
    elif provider=='fotmob':
        if endpoint!='matchDetails' or not isinstance(payload,dict):return False
        mid=str(payload.get('general',{}).get('matchId',''))
        payload=without_incident_stream(payload)
    else:return False
    for directory in (directories if directories is not None else root.glob('*/*')):
        if directory.parent.name=='_system':continue
        p=ResponseCache(directory/'raw').path(provider,'match/'+mid)
        if p.exists():
            saved=json.loads(p.read_text(encoding='utf-8'))
            if saved.get('provider')==provider and saved.get('key')=='match/'+mid and saved.get('payload')==payload:return True
    return False

def compact(root,*,apply=False,limit=None):
    root=Path(root)
    if not (root/'.soccer-scraper.json').exists():raise ValueError('Not a marked scraper data folder')
    totals=dict(files=0,bytes_before=0,bytes_after=0,duplicates=0,recovered=0,unrecognized=0)
    # Includes an earlier interrupted migration's cache folders.
    directories=tuple(p for p in root.glob('*/*') if re.fullmatch(r'\d{4}-\d{4}',p.name) and p.is_dir())
    sources=[root/name for name in LEGACY_CACHES]+[root/'_system'/'retired_cache'/name for name in LEGACY_CACHES]
    for source in sources:
        for path in source.glob('*/*.json'):
            if limit is not None and totals['files']>=limit:
                totals['bytes_saved']=totals['bytes_before']-totals['bytes_after'];totals['sampled']=True
                return totals
            try:size=path.stat().st_size
            except FileNotFoundError:continue
            totals['files']+=1;totals['bytes_before']+=size
            try:
                provider,endpoint,params,kind,value=normalize(path)
                try:duplicate=value.get('status',200)==200 and authoritative(root,provider,endpoint,value.get('payload'),directories=directories)
                except (ProviderError,ValueError,TypeError,KeyError):duplicate=False
                if duplicate:
                    totals['duplicates']+=1
                    if apply:path.unlink()
                    continue
                # Unique responses remain recoverable, outside the bounded scratch cache.
                raw=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
                packed=gzip.compress(raw,compresslevel=6,mtime=0)
                totals['bytes_after']+=len(packed);totals['recovered']+=1
                if apply:
                    target=system_path(root,'cache')/'recovered'/provider/RequestCache(root,provider).path(endpoint,params,kind).name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():
                        with gzip.open(target,'rt',encoding='utf-8') as f:existing=json.load(f)
                        if existing!=value:raise ValueError('Recovered response conflict')
                    else:
                        temp=target.with_suffix('.tmp')
                        with temp.open('wb') as output:
                            output.write(packed);output.flush();os.fsync(output.fileno())
                        with gzip.open(temp,'rt',encoding='utf-8') as f:
                            if json.load(f)!=value:raise ValueError('Compression verification failed')
                        atomic_replace(temp,target)
                    path.unlink()
            except (ValueError,KeyError,TypeError,OSError):
                totals['unrecognized']+=1;totals['bytes_after']+=path.stat().st_size if path.exists() else 0
        if apply and source.exists():
            for directory in sorted(source.rglob('*'),reverse=True):
                if directory.is_dir():
                    try:directory.rmdir()
                    except OSError:pass
            try:source.rmdir()
            except OSError:pass
    totals['bytes_saved']=totals['bytes_before']-totals['bytes_after']
    return totals

def main(argv=None):
    import config
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--apply',action='store_true',help='Apply after stopping Main/worker; default is a read-only preview')
    parser.add_argument('--sample',type=int,help='Read-only preview of the first N legacy responses')
    args=parser.parse_args(argv)
    if args.sample is not None and (args.sample<1 or args.apply):parser.error('--sample must be positive and cannot be combined with --apply')
    if args.apply:
        organize_data_root(args.data_root)
        with FileLock(str(system_path(args.data_root,'.worker.lock')),timeout=0),FileLock(str(system_path(args.data_root,'.collector.lock')),timeout=0):
            result=compact(args.data_root,apply=True)
    else:result=compact(args.data_root,limit=args.sample)
    print(json.dumps({'applied':args.apply,**result},indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
