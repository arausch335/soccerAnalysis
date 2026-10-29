"""Read actual player-stat evidence, not scheduled fixtures as proof of scraping."""
import csv
from collections import defaultdict
import json
from pathlib import Path
import re

from dataset_builder import _id_row, _reviewed_provider_player_id
from providers.reep import PROVIDERS
from storage.pandasStore import PandasStore


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        yield from csv.DictReader(stream)


def identifier(value,default='sofascore'):
    text=str(value or '')
    for prefix,p in [('fmb_','fotmob'),('wsc_','whoscored')]:
        if text.startswith(prefix):return p,text[len(prefix):]
    return default,text


def checkpoint_rows(source,cfg,players,teams,matches,observed,player_observed):
    """Project compact identity/performance tables, never match event streams."""
    from filelock import FileLock
    from collection.selection import season_label
    baseline=cfg.get('id_namespace',cfg.get('baseline_provider','sofascore'))
    competition=cfg['competition'];season=season_label(cfg['season'])
    stores=list((source/'match_checkpoints').glob('*/CURRENT.json'))
    if not stores and (source/'dataset/CURRENT.json').is_file():stores=[source/'dataset/CURRENT.json']
    for marker in stores:
        with FileLock(str(marker.parent/'.checkpoint.lock'),timeout=0):
            folder=PandasStore(marker.parent).current_directory
            names={r['id']:json.loads(r['name']) for r in read_csv(folder/'players.csv')}
            team_names={r['id']:json.loads(r['name']) for r in read_csv(folder/'teams.csv')}
            appearances=list(read_csv(folder/'performances.csv'))
            valid={r['match_id'] for r in appearances if any(not k.startswith('_') and v is not None
                for k,v in json.loads(r.get('raw_stats') or '{}').items())}
            for row in read_csv(folder/'matches.csv'):
                mid=row['id']
                if mid not in valid:continue
                metadata=json.loads(row.get('metadata') or '{}')
                provider=metadata.get('id_namespace',baseline)
                if provider not in PROVIDERS:raise ValueError('Unknown checkpoint namespace')
                observed.add((provider,mid))
                match=_id_row(matches,(competition,season,provider,mid),{
                    'competition':competition,'season':season,
                    'date':json.loads(row['kickoff'])[:10],
                    'home_team_name':team_names.get(row['home_team_id'],''),
                    'away_team_name':team_names.get(row['away_team_id'],'')})
                match['_ids'][provider+'_id'].append(mid)
                for p,n in metadata.get('provider_match_ids',{}).items():
                    if p in PROVIDERS:match['_ids'][p+'_id'].append(n)
                for side in ('home','away'):
                    tid=row[side+'_team_id']
                    team=_id_row(teams,(competition,season,provider,tid),{
                        'competition':competition,'season':season,'team_name':team_names.get(tid,'')})
                    team['_ids'][provider+'_id'].append(tid)
                for appearance in appearances:
                    if appearance['match_id']!=mid:continue
                    pid=appearance['player_id'];team_name=team_names.get(appearance['team_id'],'')
                    player_observed.add((provider,pid))
                    player=_id_row(players,(competition,season,team_name,provider,pid),{
                        'competition':competition,'season':season,'team_name':team_name,'player_name':names.get(pid,'')})
                    player['_ids'][provider+'_id'].append(pid)
                    stats=json.loads(appearance.get('raw_stats') or '{}')
                    for p,entry in stats.get('_providers',{}).items():
                        if p in PROVIDERS:
                            linked=_reviewed_provider_player_id(p,pid,entry.get('id'))
                            if linked:player['_ids'][p+'_id'].append(linked)


def lineup_rows(source,cfg,players,teams,matches,observed,player_observed):
    """Include saved Sofascore lineups even if checkpoint/CSV export was deferred."""
    from collection.selection import season_label
    season=season_label(cfg['season']);competition=cfg['competition']
    schedule=json.loads((source/'schedule.json').read_text(encoding='utf-8')).get('events',[])
    events={str(e['id']):e for e in schedule}
    for path in sorted((source/'raw/sofascore').glob('*.json')):
        with path.open(encoding='utf-8') as stream:head=stream.read(1024)
        found=re.search(r'"key"\s*:\s*"lineups/(\d+)"',head)
        if not found:continue
        saved=json.loads(path.read_text(encoding='utf-8'));mid=found[1]
        if saved.get('provider')!='sofascore':raise ValueError('Sofascore raw namespace mismatch: '+str(path))
        sides=saved.get('payload') or {};event=events.get(mid,{})
        valid=[(side,p) for side in ('home','away') for p in sides.get(side,{}).get('players',[]) if p.get('statistics')]
        if not valid:continue
        observed.add(('sofascore',mid))
        row=_id_row(matches,(competition,season,'sofascore',mid),{'competition':competition,'season':season})
        row['_ids']['sofascore_id'].append(mid)
        for side,appearance in valid:
            player=appearance.get('player') or {};pid=str(player.get('id') or '')
            if not pid:continue
            team=event.get(side+'Team',{});team_name=team.get('name','')
            player_observed.add(('sofascore',pid))
            row=_id_row(players,(competition,season,team_name,'sofascore',pid),{
                'competition':competition,'season':season,'team_name':team_name,'player_name':player.get('name','')})
            row['_ids']['sofascore_id'].append(pid)
            if team.get('id'):
                row=_id_row(teams,(competition,season,'sofascore',str(team['id'])),{
                    'competition':competition,'season':season,'team_name':team_name})
                row['_ids']['sofascore_id'].append(str(team['id']))


def source_signature(root,dataset):
    """Detect input replacement/additions during an offline rebuild."""
    import hashlib
    rows=[]
    for base,patterns in [(Path(root),['*/*/config.json','*/*/schedule.json','*/*/identities.csv',
                                     '*/*/player_match_stats.csv','*/*/match_checkpoints/*/CURRENT.json',
                                     '*/*/dataset/CURRENT.json','*/*/raw/*/*.json',
                                     'fotmob_player_stats/*/*/raw/*/*.json','whoscored/*/*/raw/*/*.json',
                                     'whoscored_events/*/*/raw/*/*.json']),
                          (Path(dataset),['fixtures.csv','*/*/player_match_stats.csv','*/*/fixtures.csv'])]:
        for pattern in patterns:
            for path in base.glob(pattern):
                if base==Path(dataset) and pattern!='fixtures.csv' and not re.fullmatch(r'\d{4}-\d{4}',path.parent.name):continue
                stat=path.stat();rows.append((str(path),stat.st_size,stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def fotmob_references(payload,mid):
    """Read identity evidence without requiring the full statistical adapter.

    Some historical responses use teamId=0 in playerStats. The explicit lineup
    supplies team membership; a nonzero contradictory team still blocks export.
    """
    from providers.statistics import fotmob_stats
    general=payload['general'];content=payload['content']
    if str(general['matchId'])!=mid:raise ValueError('FotMob raw match ID mismatch')
    lineup=content.get('lineup')
    if not general.get('finished') or not lineup:return {}
    sides={}
    for index,side in enumerate(('home','away')):
        team=lineup[side+'Team'];players=[]
        if str(payload['header']['teams'][index]['id'])!=str(team['id']):
            raise ValueError('FotMob team order changed')
        for entry in team['starters']+team['subs']:
            player=(content.get('playerStats') or {}).get(str(entry['id']))
            if not player or not player.get('stats'):continue
            if str(player['id'])!=str(entry['id']):raise ValueError('FotMob player ID mismatch')
            if str(player.get('teamId') or 0) not in ('0',str(team['id'])):
                raise ValueError('FotMob player belongs to another team')
            if any(v is not None for v in fotmob_stats(player)[1].values()):
                players.append({'id':entry['id'],'name':entry['name']})
        sides[side]={'id':team['id'],'name':team['name'],'players':players}
    return sides


def observations(root,dataset,*,report=print):
    """Return native references plus exact matches with saved player statistics.

    Checkpoints supply richer native links even when CSV exports were deferred.
    Flat CSVs supply additional observed players; existing catalogs only enrich
    observed IDs and never establish scraped coverage by themselves.
    """
    root,dataset=Path(root),Path(dataset)
    players,teams,matches={},{},{};observed=set();player_observed=set();sources=[]
    for n,cfg_path in enumerate(sorted(root.glob('*/*/config.json')),1):
        source=cfg_path.parent;cfg=json.loads(cfg_path.read_text(encoding='utf-8'))
        baseline=cfg.get('id_namespace',cfg.get('baseline_provider','sofascore'))
        if baseline not in PROVIDERS:raise ValueError('Unknown collection ID namespace')
        if not (source/'schedule.json').is_file():continue
        checkpoint_rows(source,cfg,players,teams,matches,observed,player_observed)
        if baseline=='sofascore':lineup_rows(source,cfg,players,teams,matches,observed,player_observed)
        sources.append(str(source.relative_to(root)))
        report(f'Indexed stored collection {n}: {source.parent.name}/{source.name}',flush=True)

    # Include all current flat exports, including newer statistics than the old
    # concordances. Reading these does not require provider requests.
    flat=[]
    for base in (root,dataset):flat.extend(sorted(base.glob('*/*/player_match_stats.csv')))
    for path in flat:
        if not re.fullmatch(r'\d{4}-\d{4}',path.parent.name):continue
        links=defaultdict(list)
        identities=path.parent/'identities.csv'
        if identities.is_file():
            for link in read_csv(identities):
                if link.get('provider') in PROVIDERS:
                    links[link.get('match_id'),link.get('player_id')].append(link)
        for row in read_csv(path):
            provider,mid=identifier(row.get('match_id'))
            pp,pid=identifier(row.get('player_id'),provider)
            if not mid or not pid:continue
            observed.add((provider,mid));player_observed.add((pp,pid))
            competition=row.get('competition') or path.parent.parent.name
            season=(row.get('season') or path.parent.name).replace('/','-')
            key=(competition,season,row.get('team',''),pp,pid)
            entry=players.setdefault(key,{'competition':competition,'season':season,
                'team_name':row.get('team',''),'player_name':row.get('player_name',''),
                '_ids':{p+'_id':[] for p in PROVIDERS}})
            entry['_ids'][pp+'_id'].append(pid)
            for link in links[row.get('match_id'),row.get('player_id')]:
                linked=_reviewed_provider_player_id(link['provider'],pid,link.get('provider_player_id'))
                if linked:entry['_ids'][link['provider']+'_id'].append(linked)
    result={kind:[] for kind in ('match','team','player')}
    for kind,rows in [('match',matches),('team',teams),('player',players)]:
        for row in rows.values():
            native={p+'_id':sorted({str(v) for v in row['_ids'][p+'_id'] if v is not None and str(v)}) for p in PROVIDERS}
            if kind=='match' and not any((p,v) in observed for p in PROVIDERS for v in native[p+'_id']):continue
            if kind=='player' and not any((p,v) in player_observed for p in PROVIDERS for v in native[p+'_id']):continue
            result[kind].append({**{k:v for k,v in row.items() if k!='_ids'},'_native':native})

    # Supplement raw provider-only archives that have no combined checkpoint.
    raw_seen={'team':set(),'player':set()}
    for subtree in ('','fotmob_player_stats','whoscored','whoscored_events'):
        for directory in sorted((root/subtree).glob('*/*')):
            if not directory.is_dir():continue
            if not re.fullmatch(r'\d{4}-\d{4}',directory.name):continue
            providers=('fotmob','whoscored') if not subtree else ('fotmob',) if subtree=='fotmob_player_stats' else ('whoscored',)
            paths=[(p,path) for p in providers for path in sorted((directory/'raw'/p).glob('*.json'))]
            for provider,path in paths:
                with path.open(encoding='utf-8') as stream:head=stream.read(1024)
                if not re.search(r'"key"\s*:\s*"match/\d+"',head):continue
                saved=json.loads(path.read_text(encoding='utf-8'))
                if saved.get('provider')!=provider:raise ValueError('Raw provider archive namespace mismatch')
                mid=saved['key'].split('/')[-1];payload=saved['payload']
                if provider=='fotmob':
                    if not any(p.get('stats') for p in (payload.get('content',{}).get('playerStats') or {}).values()):continue
                    sides=fotmob_references(payload,mid)
                else:
                    if str(payload.get('match_id'))!=mid:raise ValueError('WhoScored raw match ID mismatch')
                    centre=payload.get('matchCentreData',{})
                    sides={side:{'id':centre.get(side,{}).get('teamId'),'name':centre.get(side,{}).get('name',''),
                                 'players':[{'id':p['playerId'],'name':p.get('name','')} for p in centre.get(side,{}).get('players',[]) if p.get('stats')]} for side in ('home','away')}
                if not any(s['players'] for s in sides.values()):continue
                context={'competition':directory.parent.name,'season':directory.name}
                observed.add((provider,mid))
                result['match'].append({**context,'_native':{provider+'_id':[mid]}})
                for side in sides.values():
                    team_key=(provider,str(side['id']),*context.values())
                    if side['id'] is not None and team_key not in raw_seen['team']:
                        result['team'].append({**context,'team_name':side['name'],'_native':{provider+'_id':[str(side['id'])]}})
                        raw_seen['team'].add(team_key)
                    for player in side['players']:
                        key=(provider,str(player['id']),side['name'],*context.values())
                        if key not in raw_seen['player']:
                            result['player'].append({**context,'team_name':side['name'],'player_name':player['name'],'_native':{provider+'_id':[str(player['id'])]}})
                            raw_seen['player'].add(key)
    result['observed']=observed;result['sources']=sources
    return result
