"""Offline match coverage for every competition/season in config/settings.yaml.

Percentages use saved finished, non-qualification fixtures, not an assumed full
season size. Missing schedules are unknown; a partial schedule can overstate
season coverage. Run the collectors with refreshed schedules before auditing.
"""
import argparse
import json
from pathlib import Path
import re
import pandas as pd
import config
from collection.paths import collection_directory
from collection.runner import atomic_csv
from collection.selection import competition_by_name, season_label, is_qualification_event
from providers.cache import ResponseCache
from storage import PandasStore


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def finished(events):
    result={}
    for event in events:
        if (event.get('status',{}).get('type')=='finished'
                and not is_qualification_event(event)
                and not event.get('isAwarded') and not event.get('status',{}).get('awarded')):
            mid=int(event['id'])
            if mid in result and result[mid]!=event:raise ValueError('Conflicting scheduled match IDs')
            result[mid]=event
    return result


def who_valid(payload,mid,event=None):
    data=payload.get('matchCentreData',{})
    if (payload.get('match_id')!=mid or data.get('statusCode')!=6
            or not isinstance(data.get('events'),list) or not data['events']):return False
    if event:
        from datetime import datetime, timezone
        date=datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat()
        if data.get('startDate','')[:10]!=date:return False
        if any(data.get(side,{}).get('teamId')!=event[side+'Team']['id']
               for side in ('home','away')):return False
    return True


def raw_payload(directory,provider,key):
    path=ResponseCache(Path(directory)/'raw').path(provider,key)
    if not path.exists():return None
    saved=read(path)
    if saved.get('provider')!=provider or saved.get('key')!=key:
        raise ValueError(f'Cache identity mismatch: {path}')
    return saved['payload']


def sofa_valid(payload):
    for side in ('home','away'):
        players=payload.get(side,{}).get('players',[])
        if not players:return False
        played=[p for p in players if not p.get('substitute',False)
                or (p.get('statistics',{}).get('minutesPlayed') or 0)>0]
        if not played or any(not p.get('statistics') for p in played):return False
    return True


def checkpoint_sofa(directory,mid):
    store=PandasStore(Path(directory)/'match_checkpoints'/str(mid))
    if not store.exists:return False
    dataset=store.from_csv();match=dataset.matches.get(mid)
    if match is None or match.metadata.get('id_namespace','sofascore')!='sofascore':return False
    for lineup in match.lineups:
        if lineup is None or not lineup.players:return False
        played=[p for p in lineup.players if p.starter or p.did_play]
        if not played or any(not {k:v for k,v in p.stats.raw.items() if not k.startswith('_')}
                             for p in played):return False
    return True


def integrated_who(directory,mid):
    store=PandasStore(Path(directory)/'match_checkpoints'/str(mid))
    if not store.exists:return False
    dataset=store.from_csv();match=dataset.matches.get(mid)
    if match is None:return False
    who_id=match.metadata.get('provider_match_ids',{}).get('whoscored')
    if who_id is None:return False
    payload=raw_payload(directory,'whoscored',f'match/{who_id}')
    if not payload or not who_valid(payload,who_id):return False
    data=payload['matchCentreData']
    if data.get('startDate','')[:10]!=match.kickoff.date().isoformat():return False
    return True


def coverage(root,competition,season,provider):
    slug=re.sub(r'[^a-z0-9]+','-',competition.name.lower()).strip('-')
    label=season_label(season)
    directory=collection_directory(root,f'{slug}-{label}')
    own=directory
    who_schedule=own/'whoscored_schedule.json'
    if not who_schedule.exists():
        from collection.whoScoredData import LEGACY_SUBTREES,metadata_path
        for subtree in LEGACY_SUBTREES:
            own=Path(root)/subtree/slug/label;who_schedule=metadata_path(own,'schedule.json')
            if who_schedule.exists():break
    # Prefer the provider's own schedule, retaining independent ID namespaces.
    if provider=='fotmob':
        from collection.fotmobOnly import player_rows
        own=Path(root)/'fotmob_player_stats'/slug/label
        schedule=own/'schedule.json'
        if not schedule.exists():
            return dict(complete=0,total=None,percent=None,schedule='',note='no saved FotMob schedule')
        saved=read(schedule)
        if saved.get('provider')!='fotmob':raise ValueError('FotMob schedule namespace mismatch')
        events=finished(saved['events'])
        def check(mid,event):
            payload=raw_payload(own,'fotmob',f'match/{mid}')
            if payload is None:return False
            return bool(player_rows(payload,competition.name,season,event))
    elif provider=='whoscored' and who_schedule.exists():
        schedule=who_schedule;events=finished(read(schedule)['events'])
        def check(mid,event):
            payload=raw_payload(own,'whoscored',f'match/{mid}')
            if payload is None:payload=raw_payload(directory,'whoscored',f'match/{mid}')
            return bool(payload and who_valid(payload,mid,event))
    else:
        schedule=directory/'schedule.json'
        if schedule.exists():
            settings=read(directory/'config.json') if (directory/'config.json').exists() else {}
            if settings.get('baseline_provider','sofascore')!='sofascore':
                return dict(complete=0,total=None,percent=None,schedule=str(schedule),
                            note='saved collection uses another ID namespace')
            events=finished(read(schedule)['events'])
        elif provider=='sofascore':
            schedule=Path(root)/'fixture_schedules'/'sofascore'/slug/(label+'.json')
            if not schedule.exists():
                return dict(complete=0,total=None,percent=None,schedule='',note='no saved schedule')
            saved=read(schedule)
            if saved.get('provider')!='sofascore' or saved.get('competition_id')!=competition.sofascore_id:
                raise ValueError(f'Fixture schedule identity mismatch: {schedule}')
            events=finished(saved['events'])
        else:
            return dict(complete=0,total=None,percent=None,schedule='',note='no saved schedule')
        def check(mid,event):
            if provider=='whoscored':return integrated_who(directory,mid)
            payload=raw_payload(directory,'sofascore',f'lineups/{mid}')
            return sofa_valid(payload) if payload is not None else checkpoint_sofa(directory,mid)
    count=0;invalid=0;complete_ids=[]
    for mid,event in events.items():
        try:
            if check(mid,event):count+=1;complete_ids.append(str(mid))
        except (OSError,ValueError,KeyError,TypeError):invalid+=1
    total=len(events)
    note='coverage of saved finished fixtures; refresh schedule for current season coverage'
    if invalid:note+=f'; {invalid} invalid/unreadable matches excluded'
    return dict(complete=count,total=total,percent=100*count/total if total else None,
                schedule=str(schedule),note=note,_complete_ids=complete_ids)


def build_report(root,competitions,seasons,provenance=None):
    portable={}
    if provenance is not None:
        from collection.ensembleProgress import shared_progress
        portable=shared_progress(provenance)
    rows=[]
    for name in competitions:
        competition=competition_by_name(name)
        for season in seasons:
            for provider,dataset in (('whoscored','events'),('sofascore','player_match_stats'),('fotmob','player_match_stats')):
                try:result=coverage(root,competition,season,provider)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    result=dict(complete=0,total=None,percent=None,schedule='',note=f'cannot audit: {exc}')
                local=set(result.pop('_complete_ids',[]))
                namespace={'whoscored':'whoscored_events','sofascore':'sofascore','fotmob':'fotmob_players'}[provider]
                elsewhere={str(record['match_id']) for record in portable.values()
                    if record.get('namespace')==namespace and record.get('competition')==competition.name
                    and record.get('season')==season_label(season) and record.get('status')=='complete'
                    and (provider!='sofascore' or 'provider:sofascore' in record.get('capabilities',[]))}-local
                result['portable_elsewhere']=len(elsewhere)
                rows.append(dict(competition=competition.name,season=season_label(season),
                                 provider=provider,dataset=dataset,**result))
    return pd.DataFrame(rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--output',type=Path,help='Optional CSV copy of the report')
    parser.add_argument('--ensemble-root',type=Path,default=config.PROJECT_ROOT/'ensemble_data')
    args=parser.parse_args(argv)
    frame=build_report(args.data_root,config.COMPETITIONS,config.SEASONS,args.ensemble_root/'provenance.json')
    print('Coverage of saved finished fixtures (awarded games and qualification excluded).')
    print('Refresh saved schedules first; these percentages do not verify the fixture list is exhaustive.')
    for (competition,season),group in frame.groupby(['competition','season'],sort=False):
        parts=[]
        for row in group.to_dict('records'):
            text=(f"{row['percent']:.0f}% complete ({row['complete']}/{int(row['total'])})"
                  if pd.notna(row['percent']) else 'N/A (no saved finished-fixture denominator)')
            parts.append(f"{row['provider']} {row['dataset']}: {text}")
            if row['portable_elsewhere']:parts[-1]+=f"; {row['portable_elsewhere']} recorded elsewhere (not counted as local data)"
            if row['note'].startswith('cannot audit:'):print(row['note'])
        print(f"{competition} {season}: "+'; '.join(parts))
    if args.output:atomic_csv(frame,args.output)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
