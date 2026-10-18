"""Standalone, resumable WhoScored event collection without Sofascore discovery."""
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
from providers.safeClient import SafeWhoScoredClient
from storage.pandasStore import atomic_json
from .progress import get_job, collection_scope
from .selection import competition_by_name, season_key, season_label, configured_whoscored_stages


class WhoScoredEventRunner:
    def __init__(self, client, directory, report=print):
        self.client = client
        self.directory = Path(directory)
        self.cache = ResponseCache(self.directory / 'raw')
        self.report = report

    def schedule(self, competition, season, *, refresh=False):
        path = self.directory / 'schedule.json'
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
        timing.begin()
        self.directory.mkdir(parents=True, exist_ok=True)
        job = self.directory/'job.json'
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
        failure_path = self.directory/'failures.json'
        failures = json.loads(failure_path.read_text(encoding='utf-8')) if failure_path.exists() else {}
        complete = 0;elsewhere=0
        timing.schedule(events,completed=[e['id'] for e in events
            if self.cache.path('whoscored',f"match/{e['id']}").exists()],mode='http')
        for index,event in enumerate(events,1):
            mid = event['id']
            cached=self.cache.path('whoscored',f'match/{mid}').exists()
            timing.start_game(mid,clients=(self.client,),cached=cached)
            game_status='complete'
            identity=(f"WhoScored {competition.name} {season_label(season)} | match {mid} | "
                      f"{event['homeTeam']['name']} vs {event['awayTeam']['name']}")
            self.report(f'{identity}: {index}/{len(events)}',flush=True)
            from .ensembleProgress import remote_done, record_complete
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
                timing.finish_game(clients=(self.client,),status=game_status)
        self.report(f'WhoScored {competition.name} {season_label(season)} raw responses saved locally: {complete-elsewhere}/{len(events)}; {elsewhere} recorded on another computer',flush=True)
        code=0 if events and complete == len(events) else 2
        timing.end(code)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--competition',default=config.COMPETITIONS[0])
    parser.add_argument('--season',default=config.SEASONS[0])
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--refresh-schedule',action=argparse.BooleanOptionalAction,
                        default=config.WHOSCORED_REFRESH_SCHEDULE,
                        help='Refresh fixtures to include newly finished matches')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error('limit must be positive')
    if args.refresh_schedule and args.offline:
        parser.error('--refresh-schedule requires online collection')
    try:
        competition = competition_by_name(args.competition)
        season = season_key(args.season)
        slug = re.sub(r'[^a-z0-9]+','-',competition.name.lower()).strip('-')
        root = args.data_root.resolve()
        root.mkdir(parents=True,exist_ok=True)
        if not any(root.iterdir()):
            atomic_json(root/'.soccer-scraper.json',{'format':1})
        # Share the existing safety ledger and lock; standalone jobs have their
        # own subtree so the Sofascore-keyed dataset builder cannot ingest them.
        directory = root/'whoscored_events'/slug/season_label(season)
        with collection_scope('whoscored_events',root,[competition.name],[season],limit=args.limit), \
                FileLock(str(root/'.collector.lock'),timeout=0):
            with SafeWhoScoredClient(root,'whoscored',offline=args.offline) as client:
                return WhoScoredEventRunner(client,directory).run(competition.name,season,
                    limit=args.limit,refresh_schedule=args.refresh_schedule)
    except KeyboardInterrupt:
        print('Interrupted. Run the same command to resume from saved responses.')
        return 130
    except (ProviderError,ValueError,OSError,KeyError,Timeout) as exc:
        print(f'WhoScored collection paused: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
