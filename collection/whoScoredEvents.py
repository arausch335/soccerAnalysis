"""Standalone, resumable WhoScored event collection without Sofascore discovery."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from filelock import FileLock, Timeout
from providers.cache import ResponseCache
from providers.events import whoscored_fixture
from providers.httpClient import ProviderError, ProviderRequestError
from providers.safeClient import SafeWhoScoredClient
from storage.pandasStore import atomic_json
from .selection import competition_by_name, season_key, season_label


class WhoScoredEventRunner:
    def __init__(self, client, directory, report=print):
        self.client = client
        self.directory = Path(directory)
        self.cache = ResponseCache(self.directory / 'raw')
        self.report = report

    def schedule(self, competition, season, *, refresh=False):
        path = self.directory / 'schedule.json'
        if path.exists() and not refresh:
            return json.loads(path.read_text())['events']
        start = int(season[:4])
        now = datetime.now(timezone.utc)
        # Include June and July for shifted seasons and early competition starts.
        months = [f'{year}{month:02d}' for year in (start,start+1)
                  for month in range(1,13)
                  if (start,6) <= (year,month) <= min((start+1,8),(now.year,now.month))]
        events = {}
        for source in (competition.whoscored_path,*competition.whoscored_supplemental_paths):
            available = self.client.seasons(source)
            found = [s for s in available if season_key(s['name']) == season]
            if len(found) != 1:
                raise ValueError(f'WhoScored season {season} is not uniquely available')
            for stage in self.client.season_stages(found[0]['path']):
                for month in months:
                    self.report(f'WhoScored fixtures: stage {stage}, {month}', flush=True)
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
        self.directory.mkdir(parents=True, exist_ok=True)
        job = self.directory/'job.json'
        settings = dict(provider='whoscored',competition=competition.name,season=season)
        if job.exists() and json.loads(job.read_text()) != settings:
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
        failures = json.loads(failure_path.read_text()) if failure_path.exists() else {}
        for index,event in enumerate(events,1):
            mid = event['id']
            self.report(f'WhoScored match {mid}: {index}/{len(events)}',flush=True)
            try:
                def fetch():
                    payload = self.client.match_details(mid)
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
                self.cache.fetch('whoscored',f'match/{mid}',fetch)
                failures.pop(str(mid),None)
            except (ProviderError,ValueError,KeyError) as exc:
                failures[str(mid)] = str(exc)
                self.report(f'WhoScored match {mid} paused: {exc}',flush=True)
                if (isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429)
                        or getattr(getattr(self.client,'request_gate',None),'is_blocked',False)):
                    break
            finally:
                atomic_json(failure_path,failures)
        complete = sum(self.cache.path('whoscored',f"match/{event['id']}").exists()
                       for event in events)
        self.report(f'WhoScored raw responses saved: {complete}/{len(events)}',flush=True)
        return 0 if events and complete == len(events) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--competition',default='Premier League')
    parser.add_argument('--season',default='2024/25')
    parser.add_argument('--data-root',type=Path,
                        default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--refresh-schedule',action='store_true',
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
        with FileLock(str(root/'.collector.lock'),timeout=0):
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
