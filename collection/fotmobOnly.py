"""FotMob-only player stats, with isolated IDs, resumable raw data and no enrichment."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from filelock import FileLock, Timeout
import pandas as pd
import config
from providers.cache import ResponseCache
from providers.events import fotmob_fixture
from providers.fotMobClient import without_incident_stream
from providers.fotMobLoader import FotMobLoader
from providers.httpClient import ProviderError, ProviderRequestError
from providers.safeClient import SafeFotMobClient
from storage.pandasStore import atomic_json
from .playerStatsSchema import (PLAYER_MATCH_COLUMNS, FOTMOB_PLAYER_MATCH_COLUMNS,
                               FOTMOB_COLUMNS, fotmob_column)
from .runner import atomic_csv, now
from .progress import get_job, collection_scope
from .selection import competition_by_name, season_key, season_label


def player_rows(payload, competition, season, event):
    decoded = FotMobLoader().decode(payload)
    actual = decoded['event']
    if actual['id'] != event['id']:
        raise ValueError('FotMob fixture match ID mismatch')
    if actual['startTimestamp']//86400 != event['startTimestamp']//86400:
        raise ValueError('FotMob fixture date mismatch')
    for side in ('home','away'):
        if (actual[side+'Team']['id'] != event[side+'Team']['id'] or
                actual[side+'Score'] != event[side+'Score']):
            raise ValueError('FotMob fixture team or score mismatch')
    rows = []
    for side, other in (('home','away'),('away','home')):
        for player in decoded[side]['players']:
            row = dict.fromkeys(PLAYER_MATCH_COLUMNS)
            row.update(competition=competition,season=season_label(season),
                match_id=f"fmb_{actual['id']}",
                date=datetime.fromtimestamp(actual['startTimestamp'],timezone.utc).date().isoformat(),
                team_id=f"fmb_{actual[side+'Team']['id']}",team=actual[side+'Team']['name'],
                opponent_id=f"fmb_{actual[other+'Team']['id']}",opponent=actual[other+'Team']['name'],
                player_id=f"fmb_{player['id']}",player_name=player['name'],
                starter=player['starter'],appeared=player['appeared'],
                position=player['position'],shirt_number=player['shirt_number'],
                fotmob_player_stats_status=player['data_status'])
            if player['appeared']:
                row.update({name:value for name,value in player['metrics'].items()
                            if name in FOTMOB_COLUMNS})
                for stem in ('passes','long_balls','crosses','dribbles','ground_duels',
                             'total_duels','aerial_duels'):
                    attempted=row.get(stem+'_attempted');completed=row.get(stem+'_completed')
                    if attempted is not None and completed is not None and attempted > 0:
                        row[stem+'_completion_rate']=completed/attempted
            profile=player.get('lineup_profile',{})
            row.update(fotmob_opta_id=player.get('opta_id'),fotmob_position_id=profile.get('positionId'),
                fotmob_age_reported=profile.get('age'),fotmob_country=profile.get('countryName'),
                fotmob_market_value=profile.get('marketValue'))
            rows.append({fotmob_column(k):v for k,v in row.items()})
    return rows


class FotMobOnlyRunner:
    def __init__(self,client,directory,report=print):
        self.client=client;self.directory=Path(directory);self.report=report
        self.cache=ResponseCache(self.directory/'raw')

    def run(self,competition,season,*,limit=None,refresh_schedule=False):
        competition=competition_by_name(competition);season=season_key(season)
        if limit is not None and limit < 1:raise ValueError('limit must be positive')
        timing=get_job(self.directory,'fotmob_players',competition.name,season_label(season),report=self.report)
        timing.begin()
        self.directory.mkdir(parents=True,exist_ok=True)
        job=self.directory/'job.json'
        settings={'provider':'fotmob','competition':competition.name,'season':season,
                  'id_namespace':'fmb'}
        if job.exists() and json.loads(job.read_text(encoding='utf-8')) != settings:
            raise ValueError('FotMob output belongs to another competition/season')
        atomic_json(job,settings)
        schedule=self.directory/'schedule.json'
        if schedule.exists() and not refresh_schedule:
            events=json.loads(schedule.read_text(encoding='utf-8'))['events']
        else:
            self.client.refresh_cache=refresh_schedule
            try:
                raw=self.client.season_events(competition.fotmob_id,season)
            finally:
                self.client.refresh_cache=False
            events={}
            for row in raw:
                if row['status'].get('finished') and not row['status'].get('awarded'):
                    fixture=fotmob_fixture(row)
                    if fixture['id'] in events:raise ValueError('Duplicate FotMob fixture ID')
                    events[fixture['id']]=fixture
            events=sorted(events.values(),key=lambda e:(e['startTimestamp'],e['id']))
            atomic_json(schedule,{'provider':'fotmob','fetched_at':now(),'events':events})
        selected=events[:limit] if limit else events
        failures={};stop=False
        timing.schedule(selected,completed=[e['id'] for e in selected
            if self.cache.path('fotmob',f"match/{e['id']}").exists()],mode='http')
        for index,event in enumerate(selected,1):
            mid=event['id']
            cached=self.cache.path('fotmob',f'match/{mid}').exists()
            timing.start_game(mid,clients=(self.client,),cached=cached)
            game_status='complete'
            identity=(f"FotMob {competition.name} {season_label(season)} | match {mid} | "
                      f"{event['homeTeam']['name']} vs {event['awayTeam']['name']}")
            from .ensembleProgress import remote_done, record_complete
            if not cached and remote_done('fotmob_players',competition.name,season,mid):
                self.report(f'{identity}: recorded on another computer; raw data not present locally')
                timing.finish_game(clients=(self.client,),status='elsewhere',cached=True)
                continue
            try:
                def fetch():
                    payload=without_incident_stream(self.client.match_details(mid))
                    player_rows(payload,competition.name,season,event)
                    return payload
                payload=self.cache.fetch('fotmob',f'match/{mid}',fetch)
                player_rows(payload,competition.name,season,event)
                record_complete('fotmob_players',competition.name,season,mid,collected_here=not cached)
                self.report(f'{identity}: {index}/{len(selected)} saved')
            except (ProviderError,ValueError,KeyError,TypeError) as exc:
                game_status='failed'
                failures[str(mid)]=str(exc)
                self.report(f'{identity} paused: {exc}')
                stop=(isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429)
                      or getattr(getattr(self.client,'request_gate',None),'is_blocked',False))
            timing.finish_game(clients=(self.client,),status=game_status)
            if stop:break
        # Rebuild from every saved scheduled match, so --limit never truncates
        # previous successes. Invalid caches are excluded and reported.
        rows=[];complete=0;elsewhere=0
        for event in events:
            mid=event['id'];path=self.cache.path('fotmob',f'match/{mid}')
            if not path.exists():
                if remote_done('fotmob_players',competition.name,season,mid):elsewhere+=1
                continue
            try:
                payload=self.cache.fetch('fotmob',f'match/{mid}',lambda:None)
                rows.extend(player_rows(payload,competition.name,season,event));complete+=1
            except (ValueError,KeyError,TypeError) as exc:
                failures[str(mid)]=str(exc)
        atomic_csv(pd.DataFrame(rows,columns=FOTMOB_PLAYER_MATCH_COLUMNS),
                   self.directory/'player_match_stats.csv')
        atomic_json(self.directory/'failures.json',failures)
        atomic_json(self.directory/'run_summary.json',dict(settings,
            finished_matches_discovered=len(events),saved_matches=complete,elsewhere=elsewhere,failures=failures))
        self.report(f'FotMob {competition.name} {season_label(season)} player stats: {complete}/{len(events)} matches; {len(rows)} player rows')
        code=0 if events and complete+elsewhere==len(events) and not failures else 2
        timing.end(code)
        return code


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--competition')
    parser.add_argument('--season')
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--refresh-schedule',action='store_true')
    args=parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:parser.error('limit must be positive')
    if args.offline and args.refresh_schedule:parser.error('Cannot refresh offline')
    result=0
    try:
        root=args.data_root.resolve();root.mkdir(parents=True,exist_ok=True)
        if not any(root.iterdir()):atomic_json(root/'.soccer-scraper.json',{'format':1})
        with collection_scope('fotmob_players',root,
                [args.competition] if args.competition else config.COMPETITIONS,
                [args.season] if args.season else config.SEASONS,limit=args.limit), \
                FileLock(str(root/'.collector.lock'),timeout=0):
            with SafeFotMobClient(root,'fotmob',offline=args.offline) as client:
                for name in ([args.competition] if args.competition else config.COMPETITIONS):
                    competition=competition_by_name(name)
                    slug=re.sub(r'[^a-z0-9]+','-',competition.name.lower()).strip('-')
                    for season in ([args.season] if args.season else config.SEASONS):
                        directory=root/'fotmob_player_stats'/slug/season_label(season)
                        code=FotMobOnlyRunner(client,directory).run(name,season,
                            limit=args.limit,refresh_schedule=args.refresh_schedule)
                        result=max(result,code)
                        if client.request_gate.is_blocked:return 2
        return result
    except KeyboardInterrupt:
        print('Interrupted; run again to resume.');return 130
    except (ProviderError,ValueError,OSError,KeyError,TypeError,Timeout) as exc:
        print(f'FotMob collection paused: {exc}');return 2
