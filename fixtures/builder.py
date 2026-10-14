"""Build dataset/fixtures.csv from schedules only, without match statistics.

Run this file in an IDE. Change the Python settings below rather than passing
command-line arguments. Each completed competition-season is checkpointed, so
an interrupted historical backfill resumes without repeating finished seasons.
The selected provider owns every match/team ID in the output. FotMob schedule
feeds do not contain venue or city, so those columns are blank in FotMob runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from filelock import FileLock
import pandas as pd

from providers.events import fotmob_fixture
from providers.safeClient import SafeFotMobClient, SafeSofaScoreClient
from collection.runner import atomic_csv
from collection.selection import resolve_sofascore_season
from dataset_builder import FIXTURE_COLUMNS, fixtures_from_events
from storage.pandasStore import atomic_json


# Python settings for an IDE run. END_SEASON=None includes the season underway
# (or most recently started) in the current calendar year.
PROVIDER = 'fotmob'  # 'sofascore' or 'fotmob'; IDs belong to that provider.
START_SEASON = 2010
END_SEASON = None
INCLUDE_UPCOMING = True
REFRESH_CURRENT_SEASON = True
OFFLINE = False  # True uses existing checkpoints/HTTP cache only.
DATA_ROOT = Path(__file__).resolve().parents[1] / 'data'
OUTPUT_CSV = Path(__file__).resolve().parents[1] / 'dataset' / 'fixtures.csv'


@dataclass(frozen=True)
class FixtureCompetition:
    name: str
    sofascore_id: int
    first_season: int = 2010
    fotmob_id: int | None = None
    fotmob_qualification_id: int | None = None


# SofaScore unique-tournament IDs. Qualifying rounds of the UEFA tournaments
# are included in the same season endpoint and identified per event.
COMPETITIONS = (
    FixtureCompetition('Premier League', 17, fotmob_id=47),
    FixtureCompetition('Championship', 18, fotmob_id=48),
    FixtureCompetition('La Liga', 8, fotmob_id=87),
    FixtureCompetition('La Liga 2', 54, fotmob_id=140),
    FixtureCompetition('Bundesliga', 35, fotmob_id=54),
    FixtureCompetition('2. Bundesliga', 44, fotmob_id=146),
    FixtureCompetition('Serie A', 23, fotmob_id=55),
    FixtureCompetition('Serie B', 53, fotmob_id=86),
    FixtureCompetition('Ligue 1', 34, fotmob_id=53),
    FixtureCompetition('Ligue 2', 182, fotmob_id=110),
    FixtureCompetition('Champions League', 7, fotmob_id=42,
                       fotmob_qualification_id=10611),
    FixtureCompetition('Europa League', 679, fotmob_id=73,
                       fotmob_qualification_id=10613),
    FixtureCompetition('Conference League', 17015, 2021, 10216, 10615),
)
EUROPEAN_COMPETITIONS = {'Champions League', 'Europa League', 'Conference League'}


def current_season_start(now=None):
    now = now or datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def season_label(start):
    return f'{start}-{start + 1}'


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')


def _events_from_checkpoint(path, provider, competition_id, season_id):
    saved=json.loads(path.read_text())
    if (saved.get('provider') != provider or
            saved.get('competition_id') != competition_id or
            saved.get('season_id') != season_id):
        raise ValueError(f'{path}: checkpoint belongs to another competition/season')
    return saved['events']


def _fotmob_event(row, competition_id, season_id, qualifying):
    """Keep the common fixture schema without requesting match-detail stats."""
    event=fotmob_fixture(row)
    status=row['status']
    event['status']={'type':('finished' if status.get('finished') else
                             'canceled' if status.get('cancelled') else
                             'inprogress' if status.get('started') else 'notstarted'),
                     'awarded':bool(status.get('awarded'))}
    event['isAwarded']=bool(status.get('awarded'))
    round_value=row.get('round')
    round_name=row.get('roundName')
    event['roundInfo']={'round':int(round_value) if str(round_value).isdigit() else None,
                        'name':(str(round_name) if round_name is not None and
                                not str(round_name).isdigit() else None)}
    event['tournament']={'qualificationOrPreliminary':qualifying,
                         'uniqueTournament':{'id':competition_id}}
    event['season']={'id':season_id}
    home=(event['homeScore'] or {}).get('current')
    away=(event['awayScore'] or {}).get('current')
    reason=(status.get('reason') or {}).get('shortKey')
    if (status.get('finished') and not status.get('awarded') and
            home is not None and away is not None):
        event['winnerCode']=(1 if home > away else 2 if away > home else
                             None if str(reason).startswith('penalt') else 3)
    return event


def _fotmob_season_events(client, competition, start, *, include_upcoming):
    season=f'{start}/{start + 1}'
    ids=[(competition.fotmob_id,False)]
    if competition.fotmob_qualification_id is not None:
        ids.append((competition.fotmob_qualification_id,True))
    events={}
    for competition_id,qualifying in ids:
        if competition_id is None:
            raise ValueError(f'{competition.name}: no FotMob competition ID configured')
        if season not in client.seasons(competition_id):
            raise ValueError(f'{competition.name} {season}: FotMob has no season for '
                             f'competition ID {competition_id}')
        for row in client.season_events(competition_id,season):
            if not include_upcoming and not row['status'].get('finished'):
                continue
            event=_fotmob_event(row,competition_id,start,qualifying)
            if event['id'] in events:
                raise ValueError(f'{competition.name} {season}: duplicate FotMob match ID '
                                 f'{event["id"]}')
            events[event['id']]=event
    return list(events.values())


def _schedule_events(client, competition, start, season_id, *, provider,
                     current_start, data_root, include_upcoming, refresh_current):
    label=season_label(start)
    competition_id=(competition.sofascore_id if provider=='sofascore' else
                    competition.fotmob_id)
    checkpoint=(data_root/'fixture_schedules'/provider/_slug(competition.name)/
                f'{label}.json')
    refresh=refresh_current and start == current_start
    save_checkpoint=False
    if checkpoint.exists() and not refresh:
        events=_events_from_checkpoint(checkpoint,provider,competition_id,season_id)
    else:
        # Finished full-collection schedules can be reused; unlike their match
        # statistics, these contain both main-tournament and qualifying events.
        saved=data_root/_slug(competition.name)/label/'schedule.json'
        if provider=='sofascore' and saved.exists() and not refresh:
            events=json.loads(saved.read_text())['events']
        else:
            client.refresh_cache=refresh
            try:
                if provider=='sofascore':
                    events=list(client.iter_season_events(
                        competition.sofascore_id,season_id,
                        include_upcoming=include_upcoming))
                else:
                    events=_fotmob_season_events(client,competition,start,
                                                include_upcoming=include_upcoming)
            finally:
                client.refresh_cache=False
        save_checkpoint=True
    if not events:
        raise ValueError(f'{competition.name} {label}: no fixtures returned')
    ids=[]
    for event in events:
        if not isinstance(event,dict) or not isinstance(event.get('id'),int):
            raise ValueError(f'{competition.name} {label}: invalid fixture')
        tournament_id=((event.get('tournament') or {}).get('uniqueTournament') or {}).get('id')
        event_season_id=(event.get('season') or {}).get('id')
        allowed_ids=({competition.sofascore_id} if provider=='sofascore' else
                     {competition.fotmob_id,competition.fotmob_qualification_id})
        if tournament_id is not None and tournament_id not in allowed_ids:
            raise ValueError(f'{competition.name} {label}: fixture {event["id"]} has another tournament ID')
        if event_season_id is not None and event_season_id != season_id:
            raise ValueError(f'{competition.name} {label}: fixture {event["id"]} has another season ID')
        ids.append(event['id'])
    if len(ids) != len(set(ids)):
        raise ValueError(f'{competition.name} {label}: duplicate fixture IDs')
    if save_checkpoint:
        atomic_json(checkpoint,{'provider':provider,'competition_id':competition_id,
                                'season_id':season_id,'events':events})
    return events


def build(*, provider=PROVIDER, client=None, data_root=DATA_ROOT, output_csv=OUTPUT_CSV,
          competitions=COMPETITIONS, start_season=START_SEASON,
          end_season=END_SEASON, include_upcoming=INCLUDE_UPCOMING,
          refresh_current=REFRESH_CURRENT_SEASON, offline=OFFLINE, now=None):
    """Backfill all selected seasons and atomically replace one combined CSV."""
    data_root=Path(data_root);output_csv=Path(output_csv)
    provider=provider.strip().casefold()
    if provider not in ('sofascore','fotmob'):
        raise ValueError("PROVIDER must be 'sofascore' or 'fotmob'")
    current_start=current_season_start(now)
    end=current_start if end_season is None else end_season
    if start_season < 2010 or end < start_season or end > current_start:
        raise ValueError('Select seasons from 2010 through the current season')
    if offline and refresh_current:
        raise ValueError('OFFLINE=True requires REFRESH_CURRENT_SEASON=False')
    data_root.mkdir(parents=True,exist_ok=True)
    client=client or (SafeSofaScoreClient(data_root,'sofascore',offline=offline)
                      if provider=='sofascore' else
                      SafeFotMobClient(data_root,'fotmob',offline=offline))
    print(f'Fixture provider: {provider}; match and team IDs use its namespace.',
          flush=True)
    frames=[]
    with FileLock(str(data_root/'.collector.lock'),timeout=0):
        for competition in competitions:
            first=max(start_season,competition.first_season)
            if first > end:continue
            for start in range(first,end+1):
                label=season_label(start)
                # Missing expected historical seasons are errors, not silent gaps.
                if provider=='sofascore':
                    try:
                        season=resolve_sofascore_season(client,competition,label)
                    except ValueError:
                        # A cached seasons index may predate a newly started year.
                        if start != current_start or not refresh_current:
                            raise
                        client.refresh_cache=True
                        try:
                            season=resolve_sofascore_season(client,competition,label)
                        finally:
                            client.refresh_cache=False
                    season_id=season['id']
                else:
                    season_id=start
                events=_schedule_events(client,competition,start,season_id,
                    provider=provider,current_start=current_start,data_root=data_root,
                    include_upcoming=include_upcoming,refresh_current=refresh_current)
                frame=fixtures_from_events(events,competition.name,label,
                    include_qualifying=competition.name in EUROPEAN_COMPETITIONS)
                if frame.empty:
                    raise ValueError(f'{competition.name} {label}: no fixture rows')
                frames.append(frame)
                print(f'{competition.name} {label}: {len(frame)} fixtures',flush=True)
        if not frames:raise ValueError('No seasons selected')
        combined=pd.concat(frames,ignore_index=True)
        duplicated=combined[combined.duplicated('match_id',keep=False)]
        if not duplicated.empty:
            raise ValueError(f'Duplicate {provider} match IDs across competitions: '
                             +', '.join(map(str,duplicated.match_id.unique()[:10])))
        for column in ('match_id','matchweek','home_team_id','away_team_id','home_score',
                       'away_score','winner_code'):
            combined[column]=pd.to_numeric(combined[column],errors='raise').astype('Int64')
        combined=combined.sort_values(['date','kickoff_utc','match_id'],
                                      kind='stable').reset_index(drop=True)
        atomic_csv(combined.loc[:,FIXTURE_COLUMNS],output_csv)
    print(f'Wrote {len(combined)} fixtures to {output_csv}',flush=True)
    return output_csv


if __name__ == '__main__':
    build()
