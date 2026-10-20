"""Historical artifact writers used only to exercise data migration/import tests."""
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


from collection.playerStatsSchema import (PLAYER_MATCH_COLUMNS, FOTMOB_PLAYER_MATCH_COLUMNS,
                               FOTMOB_COLUMNS, fotmob_column)


from collection.files import atomic_csv, now


from collection.progress import get_job, collection_scope


from collection.selection import competition_by_name, season_key, season_label


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
            from collection.ensembleProgress import remote_done, record_complete
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


import argparse


from datetime import datetime, timezone


import json


from pathlib import Path


import re


import config


from filelock import FileLock, Timeout


from providers.cache import ResponseCache


from providers.events import whoscored_fixture


from providers.httpClient import ProviderError, ProviderRequestError


from providers.transports.whoscored import create_whoscored_client


from storage.pandasStore import atomic_json


from collection.progress import get_job, collection_scope


from collection.selection import competition_by_name, season_key, season_label, configured_whoscored_stages


from collection.whoScoredData import metadata_path, standalone_directory


class WhoScoredEventRunner:
    def __init__(self, client, directory, report=print):
        self.client = client
        self.directory = Path(directory)
        self.cache = ResponseCache(self.directory / 'raw')
        self.report = report

    def _mode(self):
        mode=getattr(self.client,'active',getattr(self.client,'mode','http'))
        return mode if isinstance(mode,str) else 'http'

    def schedule(self, competition, season, *, refresh=False):
        path = metadata_path(self.directory,'schedule.json')
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding='utf-8'))['events']
        start = int(season[:4])
        now = datetime.now(timezone.utc)
        # Include June and July for shifted seasons and early competition starts.
        months = [f'{year}{month:02d}' for year in (start,start+1)
                  for month in range(1,13)
                  if (start,6) <= (year,month) <= min((start+1,8),(now.year,now.month))]
        events = {}
        stages=configured_whoscored_stages(competition,season)
        if stages is None:
            stages=[]
            for source in (competition.whoscored_path,*competition.whoscored_supplemental_paths):
                available = self.client.seasons(source)
                found = [s for s in available if season_key(s['name']) == season]
                if len(found) != 1:
                    raise ValueError(f'WhoScored season {season} is not uniquely available')
                stages.extend(self.client.season_stages(found[0]['path']))
        for stage in dict.fromkeys(stages):
            for month in months:
                self.report(f'WhoScored {competition.name} {season_label(season)} fixtures: stage {stage}, {month}', flush=True)
                for row in self.client.season_events(stage,month):
                    if row.get('status') == 6:
                        fixture = whoscored_fixture(row)
                        events[fixture['id']] = fixture
        result = sorted(events.values(),key=lambda e:(e['startTimestamp'],e['id']))
        atomic_json(path, {'provider':'whoscored','events':result})
        return result

    def run(self, competition, season, *, limit=None, refresh_schedule=False):
        competition = competition_by_name(competition)
        season = season_key(season)
        timing=get_job(self.directory,'whoscored_events',competition.name,season_label(season),report=self.report)
        timing.begin();timing.set_provider('whoscored')
        self.directory.mkdir(parents=True, exist_ok=True)
        job = metadata_path(self.directory,'job.json')
        settings = dict(provider='whoscored',competition=competition.name,season=season)
        if job.exists() and json.loads(job.read_text(encoding='utf-8')) != settings:
            raise ValueError('WhoScored collection selection differs from the saved job')
        atomic_json(job,settings)
        self.client.refresh_cache = refresh_schedule
        try:
            events = self.schedule(competition,season,refresh=refresh_schedule)
        finally:
            self.client.refresh_cache = False
        if limit is not None:
            events = events[:limit]
        failure_path = metadata_path(self.directory,'failures.json')
        failures = json.loads(failure_path.read_text(encoding='utf-8')) if failure_path.exists() else {}
        complete = 0;elsewhere=0
        timing.schedule(events,completed=[e['id'] for e in events
            if self.cache.path('whoscored',f"match/{e['id']}").exists()],mode=self._mode())
        for index,event in enumerate(events,1):
            mid = event['id']
            from collection.whoScoredData import reuse_match
            if not self.cache.path('whoscored',f'match/{mid}').exists():
                reuse_match(self.directory,mid,event,standalone=True)
            cached=self.cache.path('whoscored',f'match/{mid}').exists()
            timing.start_game(mid,clients=(self.client,),cached=cached)
            timing.set_provider('whoscored')
            game_status='complete'
            identity=(f"WhoScored {competition.name} {season_label(season)} | match {mid} | "
                      f"{event['homeTeam']['name']} vs {event['awayTeam']['name']}")
            self.report(f'{identity}: {index}/{len(events)}',flush=True)
            from collection.ensembleProgress import remote_done, record_complete
            if not cached and remote_done('whoscored_events',competition.name,season,mid):
                complete+=1;elsewhere+=1;failures.pop(str(mid),None)
                self.report(f'{identity}: recorded on another computer; raw data not present locally')
                timing.finish_game(clients=(self.client,),status='elsewhere',cached=True)
                continue
            try:
                def validate(payload):
                    data = payload['matchCentreData']
                    if (payload['match_id'] != mid or data.get('statusCode') != 6
                            or not data.get('events')):
                        raise ValueError('WhoScored match has no finished event stream')
                    expected_date = datetime.fromtimestamp(
                        event['startTimestamp'],timezone.utc).date().isoformat()
                    if data.get('startDate','')[:10] != expected_date:
                        raise ValueError('WhoScored fixture date mismatch')
                    for side in ('home','away'):
                        if data[side]['teamId'] != event[side+'Team']['id']:
                            raise ValueError('WhoScored fixture team mismatch')
                    return payload
                payload=self.cache.fetch('whoscored',f'match/{mid}',
                    lambda:validate(self.client.match_details(mid)))
                validate(payload)
                complete += 1
                record_complete('whoscored_events',competition.name,season,mid,collected_here=not cached)
                failures.pop(str(mid),None)
            except (ProviderError,ValueError,KeyError,TypeError) as exc:
                game_status='failed'
                failures[str(mid)] = str(exc)
                self.report(f'{identity} paused: {exc}',flush=True)
                if (isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429)
                        or getattr(getattr(self.client,'request_gate',None),'is_blocked',False)):
                    break
            finally:
                atomic_json(failure_path,failures)
                timing.finish_game(clients=(self.client,),status=game_status,mode=self._mode())
        self.report(f'WhoScored {competition.name} {season_label(season)} raw responses saved locally: {complete-elsewhere}/{len(events)}; {elsewhere} recorded on another computer',flush=True)
        code=0 if events and complete == len(events) else 2
        timing.end(code)
        return code
