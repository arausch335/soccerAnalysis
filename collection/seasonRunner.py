"""Resumable full-season collection, with independent per-match checkpoints."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import pandas as pd
from providers import SofaScoreLoader, FotMobLoader, WhoScoredLoader, ProviderError, ProviderRequestError
from providers.cache import ResponseCache
from providers.events import fotmob_fixture, whoscored_fixture
from providers.fotMobClient import without_incident_stream
from providers.identity import IdentityResolver, IdentityError, match_fixture
from providers.statistics import synthesize
from models import FootballDataset
from storage import PandasStore
from storage.pandasStore import atomic_json, KEYS
from .files import atomic_csv, now
from .selection import (competition_by_name, resolve_sofascore_season, season_key,
                        season_label, months_between, is_qualification_event,
                        with_reviewed_schedule_results, configured_whoscored_stages)
from .spatial import (RAW_DRIBBLE_RECORD_COLUMNS, RAW_PASS_RECORD_COLUMNS,
                      extract_minimal_spatial, extract_raw_dribbles,
                      extract_raw_passes)
from .spatialManifest import SPATIAL_MAP_COLUMNS, match_map_rows, player_map_rows
from .fotmobExports import match_payload, source_rows
from .sofascoreTeams import team_stat_rows as sofascore_team_rows, TEAM_STAT_COLUMNS
from .playerStatsSchema import PLAYER_MATCH_COLUMNS
from .progress import get_job, network_count
from .completion import sofascore_stats_complete
from .jsonFiles import clear_json_cache, read_json
from .whoScoredExports import event_rows as whoscored_event_rows, match_payload as whoscored_match_payload, EVENT_COLUMNS

DATA_ERRORS=(ProviderError,ValueError,KeyError,TypeError)


SPATIAL_ENDPOINTS = ('heatmap', 'rating_breakdown', 'average_positions', 'shotmap')

TEAM_RESULT_COLUMNS = (
    'match_id','date','season','competition','matchweek','team_id','team',
    'opponent_id','opponent','venue','goals_for','goals_against','result','points',
    'awarded','played_goals_for','played_goals_against','lineup_available',
    'fotmob_team_statistics_available','data_status',
)


def _is_awarded(event):
    return bool(event.get('isAwarded') or event.get('status',{}).get('awarded'))


def _event_score(event,side,*,official):
    score=event.get(side+'Score',{})
    keys=('display','normaltime','current') if official else ('current','normaltime','display')
    for key in keys:
        if score.get(key) is not None:return score[key]
    return None


def _team_result_rows(match,event,team_stat_rows):
    awarded=_is_awarded(event)
    played_home=_event_score(event,'home',official=False) if awarded else match.home_score
    played_away=_event_score(event,'away',official=False) if awarded else match.away_score
    rows=[]
    for side,team,opponent,goals_for,goals_against,played_for,played_against,lineup in (
            ('home',match.home_team,match.away_team,match.home_score,match.away_score,
             played_home,played_away,match.home_lineup),
            ('away',match.away_team,match.home_team,match.away_score,match.home_score,
             played_away,played_home,match.away_lineup)):
        result=(None if goals_for is None or goals_against is None else
                'W' if goals_for>goals_against else 'L' if goals_for<goals_against else 'D')
        rows.append({'match_id':match.id,'date':match.kickoff.date().isoformat(),
            'season':season_label(match.season.name),'competition':match.season.competition.name,
            'matchweek':match.round_number,'team_id':team.id,'team':team.name,
            'opponent_id':opponent.id,'opponent':opponent.name,'venue':side,
            'goals_for':goals_for,'goals_against':goals_against,'result':result,
            'points':None if result is None else {'W':3,'D':1,'L':0}[result],
            'awarded':awarded,'played_goals_for':played_for,
            'played_goals_against':played_against,'lineup_available':lineup is not None,
            'fotmob_team_statistics_available':any(r.get('side')==side for r in team_stat_rows),
            'data_status':'awarded_result_only' if awarded and lineup is None else
                          'complete' if lineup is not None else 'result_only'})
    return rows


class SeasonRunner:
    def __init__(self,clients,directory,*,providers=('sofascore','fotmob','whoscored'),
                 identity_map=None,refresh=False,spatial=SPATIAL_ENDPOINTS,
                 spatial_clients=None,baseline_provider='sofascore',report=print):
        self.clients=clients;self.directory=Path(directory)
        if baseline_provider not in ('sofascore','fotmob'):
            raise ValueError("baseline_provider must be 'sofascore' or 'fotmob'")
        self.baseline_provider=baseline_provider
        self.providers=tuple(dict.fromkeys((baseline_provider,*providers)))
        if set(self.providers)-{'sofascore','fotmob','whoscored'}:raise ValueError('Unknown provider')
        if set(self.providers)-clients.keys():raise ValueError('Missing requested provider client')
        self.cache=ResponseCache(self.directory/'raw')
        from providers.identityReview import _ROOT
        local_root=next((c.root for c in clients.values() if isinstance(getattr(c,'root',None),(str,Path))),None)
        self.identity_resolver=IdentityResolver(identity_map,review_root=_ROOT.get() or local_root or self.directory)
        from providers.playerProfiles import ProfileVerifier
        self.profile_verifier=ProfileVerifier(clients,self.identity_resolver.review_root,report,
            on_provider=lambda provider:self.timing.set_provider(provider) if hasattr(self,'timing') else None)
        self.identity_resolver.investigator=self.profile_verifier
        self.refresh=refresh;self.report=report
        self.spatial=tuple(dict.fromkeys(spatial))
        if set(self.spatial)-set(SPATIAL_ENDPOINTS):raise ValueError('Unknown Sofascore spatial endpoint')
        if baseline_provider!='sofascore' and self.spatial:
            raise ValueError('Sofascore spatial endpoints require a Sofascore baseline')
        self.spatial_clients=(tuple(spatial_clients or (clients['sofascore'],))
                              if baseline_provider=='sofascore' else ())
        if baseline_provider=='sofascore' and not self.spatial_clients:
            raise ValueError('At least one Sofascore spatial client is required')
        self._sofascore_blocked=False
        coverage_path=self.directory/'browser_coverage.json'
        self.browser_coverage=json.loads(coverage_path.read_text(encoding='utf-8')) if coverage_path.exists() else {}
        self.capability_path=self.directory/'capabilities.json'
        self.capabilities=json.loads(self.capability_path.read_text(encoding='utf-8')) if self.capability_path.exists() else {}

    def _fetch(self,provider,key,request,*,refresh=False):
        timing=getattr(self,'timing',None)
        if timing:timing.set_provider(provider)
        return self.cache.fetch(provider,key,request,refresh=refresh or self.refresh)

    def _fetch_fotmob_match(self,match_id,client):
        # matchDetails bundles incidents with stats and shots. Discard incidents
        # before newly fetched responses reach the cache or the loader.
        return self._fetch('fotmob',f'match/{match_id}',
            lambda:without_incident_stream(client.match_details(match_id)))

    def _player_spatial_request(self, client, match_id, player_id, name):
        key=f'{name}/{match_id}/{player_id}'
        cap=self.capabilities.setdefault(name, {'available':False,'missing_matches':[]})
        if not cap['available'] and len(cap['missing_matches'])>=3:
            return player_id,name,'deferred',None,None
        request=(lambda:client.player_heatmap(match_id,player_id)) if name=='heatmap' else (
            lambda:client.player_rating_breakdown(match_id,player_id))
        try:
            payload=self._fetch('sofascore',key,request)
            # Only an actual map schema proves availability; empty responses do not.
            fields=('heatmap',) if name=='heatmap' else ('passes','dribbles')
            if any(isinstance(payload.get(field),list) for field in fields):
                cap['available']=True
                atomic_json(self.capability_path,self.capabilities)
            return player_id,name,'complete',str(self.cache.path('sofascore',key).relative_to(self.directory)),None
        except ProviderRequestError as exc:
            if exc.status_code==404:
                cap['missing_matches']=sorted(set(cap['missing_matches'])|{match_id})
                atomic_json(self.capability_path,self.capabilities)
            state='unavailable' if exc.status_code==404 else 'failed'
            return player_id,name,state,None,exc

    def _collect_player_requests(self, match, requests, status, files):
        """Run small batches whose clients share one global request gate."""
        errors=[];blocked=False
        if not requests:return errors,blocked,[]
        completed=[]
        # Browser objects are thread-affine. One caller thread also gives the
        # intended sequential behavior for HTTP clients without an executor.
        for name,pid in requests:
            pid,name,state,path,exc=self._player_spatial_request(
                self.spatial_clients[0],match.id,pid,name)
            status.setdefault(str(pid),{})[name]=state
            if path is not None:files.setdefault(str(pid),{})[name]=path
            if exc is not None and exc.status_code!=404:errors.append(f'{name} player {pid}: {exc}')
            if exc is not None and (exc.status_code in (401,403,429) or
                    getattr(self.clients.get('sofascore'),'requires_manual_attention',False) is True):blocked=True
            if exc is not None and exc.status_code==403:self._sofascore_blocked=True
            completed.append((pid,name,state,exc))
            if blocked:break
        return errors,blocked,completed

    def _fixtures(self,competition,season,selected,only=None):
        result={};errors={}
        for provider in self.providers:
            if provider==self.baseline_provider or (only is not None and provider not in only):continue
            if provider=='sofascore':
                errors[provider]='Sofascore cannot supplement a non-Sofascore baseline without a fixture ID map'
                self.report(f'sofascore fixture discovery skipped: {errors[provider]}')
                continue
            try:
                client=self.clients[provider]
                if provider=='fotmob':
                    events={}
                    competition_ids=(competition.fotmob_id,*competition.fotmob_supplemental_ids)
                    for competition_id in competition_ids:
                        raw=self._fetch(provider,f'fixtures/{competition_id}/{season}',
                            lambda competition_id=competition_id:
                                client.season_events(competition_id,season))
                        for row in raw:
                            if row['status'].get('finished') and not row['status'].get('awarded'):
                                fixture=fotmob_fixture(row);events[fixture['id']]=fixture
                    result[provider]=list(events.values())
                else:
                    events={}
                    from .whoScoredData import standalone_fixtures
                    local=standalone_fixtures(self.directory)
                    if local and not self.refresh:
                        result[provider]=local
                        try:
                            for fixture in selected:match_fixture(fixture,local,provider='whoscored',review_root=self.identity_resolver.review_root,baseline=self.baseline_provider)
                        except (ValueError,IdentityError):pass
                        else:
                            self.report(f'whoscored: reusing {len(local)} locally saved fixtures.')
                            continue
                    events.update({row['id']:row for row in local})
                    stages=configured_whoscored_stages(competition,season)
                    if stages is None:
                        stages=[]
                        paths=(competition.whoscored_path,*competition.whoscored_supplemental_paths)
                        for competition_path in paths:
                            if competition.parent and hasattr(client,'qualification_path'):
                                competition_path=client.qualification_path(competition_path)
                            seasons=self._fetch(provider,f'seasons/{competition_path}',
                                lambda competition_path=competition_path:client.seasons(competition_path))
                            found=[s for s in seasons if season_key(s['name'])==season_key(season)]
                            if len(found)!=1:
                                raise ValueError(f'WhoScored season {season} is not uniquely available')
                            path=found[0]['path']
                            multi_stage=competition.name in ('Champions League','Europa League') or bool(competition.parent)
                            stage_key=('stages-v2/' if multi_stage else 'stage/')+path
                            discovered=self._fetch(provider,stage_key,lambda path=path:client.season_stages(path))
                            stages.extend([discovered] if isinstance(discovered,int) else discovered)
                    result[provider]=list(events.values())
                    for stage in dict.fromkeys(stages):
                        for month in months_between(selected):
                            raw=self._fetch(provider,f'fixtures/{stage}/{month}',
                                lambda stage=stage,month=month:client.season_events(stage,month))
                            for row in raw:
                                if row.get('status')==6:events[row['id']]=whoscored_fixture(row)
                            result[provider]=list(events.values())
                    result[provider]=list(events.values())
                if len({e['id'] for e in result[provider]})!=len(result[provider]):raise ValueError('Duplicate fixture IDs')
                self.report(f'{provider}: {len(result[provider])} finished fixtures available.')
            except DATA_ERRORS as exc:
                if not result.get(provider):errors[provider]=str(exc)
                self.report(f'{provider} fixture discovery failed: {exc}')
        return result,errors

    def _collect_spatial(self, match, event, sofa):
        """Cache optional Sofascore spatial responses without interpreting them.

        A 404 is normal for older action-map categories or players with too few
        minutes. Other provider failures are retained for a later resumable run.
        """
        status=match.metadata.setdefault('sofascore_spatial_status',{})
        files=match.metadata.setdefault('sofascore_spatial_files',{})
        errors=[]
        if 'average_positions' in self.spatial and status.get('average_positions') not in ('complete','unavailable','deferred'):
            key=f'average_positions/{match.id}'
            try:
                self._fetch('sofascore',key,lambda:sofa.average_positions(match.id))
                status['average_positions']='complete';files['average_positions']=str(self.cache.path('sofascore',key).relative_to(self.directory))
            except ProviderRequestError as exc:
                status['average_positions']='unavailable' if exc.status_code==404 else 'failed'
                if exc.status_code!=404:errors.append(f'average positions: {exc}')
                if exc.status_code in (401,403,429):self._sofascore_blocked=True
                if exc.status_code in (401,403,429):return errors
        if 'shotmap' in self.spatial and status.get('shotmap') not in ('complete','unavailable','deferred'):
            key=f'shotmap/{match.id}'
            try:
                self._fetch('sofascore',key,lambda:sofa.shotmap(match.id))
                status['shotmap']='complete';files['shotmap']=str(self.cache.path('sofascore',key).relative_to(self.directory))
            except ProviderRequestError as exc:
                status['shotmap']='unavailable' if exc.status_code==404 else 'failed'
                if exc.status_code!=404:errors.append(f'shotmap: {exc}')
                if exc.status_code in (401,403,429):self._sofascore_blocked=True
                if exc.status_code in (401,403,429):return errors
        appearances=[appearance for lineup in match.lineups for appearance in lineup.players if appearance.did_play]
        if 'heatmap' in self.spatial:
            if event.get('hasEventPlayerHeatMap',True):
                requests=[('heatmap',p.player.id) for p in appearances
                          if status.setdefault(str(p.player.id),{}).get('heatmap') not in ('complete','unavailable','deferred')]
                new_errors,blocked,_=self._collect_player_requests(match,requests,status,files)
                errors.extend(new_errors)
                if blocked:return errors
            else:
                for p in appearances:status.setdefault(str(p.player.id),{})['heatmap']='unavailable'

        if 'rating_breakdown' in self.spatial:
            capabilities=status.setdefault('_capabilities',{})
            pending=[p for p in appearances if status.setdefault(str(p.player.id),{}).get('rating_breakdown') not in ('complete','unavailable','deferred')]
            capability=capabilities.get('rating_breakdown','unknown')
            if capability=='unavailable':
                for p in pending:status[str(p.player.id)]['rating_breakdown']='unavailable'
            else:
                if capability!='available' and pending:
                    # A 90-minute starter is the strongest availability probe. Try one
                    # from each team so a player-specific 404 cannot hide a supported map.
                    pending_ids={p.player.id for p in pending};probes=[]
                    for lineup in match.lineups:
                        candidates=[p for p in lineup.players if p.player.id in pending_ids]
                        if candidates:
                            probes.append(min(candidates,key=lambda p:(not p.starter,-(p.minutes_played or 0))))
                    probes=probes[:2]
                    probe_requests=[('rating_breakdown',p.player.id) for p in probes]
                    new_errors,blocked,outcomes=self._collect_player_requests(match,probe_requests,status,files)
                    errors.extend(new_errors)
                    if blocked:return errors
                    if any(state=='complete' for _,_,state,_ in outcomes):
                        capability='available';capabilities['rating_breakdown']=capability
                    elif outcomes and all(state=='unavailable' for _,_,state,_ in outcomes):
                        capability='unavailable';capabilities['rating_breakdown']=capability
                        skipped=0
                        for p in pending:
                            player_status=status[str(p.player.id)]
                            if player_status.get('rating_breakdown') not in ('complete','unavailable','deferred'):
                                player_status['rating_breakdown']='unavailable';skipped+=1
                        self.report(f'[{match.id}] Rating breakdown unavailable; skipped {skipped} player requests.')
                if capability=='available':
                    requests=[('rating_breakdown',p.player.id) for p in appearances
                              if status[str(p.player.id)].get('rating_breakdown') not in ('complete','unavailable','deferred')]
                    new_errors,blocked,_=self._collect_player_requests(match,requests,status,files)
                    errors.extend(new_errors)
                    if blocked:return errors
        return errors

    def _spatial_done(self, match):
        if not self.spatial:return True
        status=match.metadata.get('sofascore_spatial_status',{})
        for name in set(self.spatial)&{'average_positions','shotmap'}:
            if status.get(name) not in ('complete','unavailable','deferred'):return False
        for lineup in match.lineups:
            for appearance in lineup.players:
                if not appearance.did_play:continue
                player_status=status.get(str(appearance.player.id),{})
                for name in set(self.spatial)-{'average_positions','shotmap'}:
                    if player_status.get(name) not in ('complete','unavailable','deferred'):return False
        return True

    def run(self,competition_name,season_name,*,limit=None,build_csvs=True,
            compile_events=True):
        if limit is not None and (isinstance(limit,bool) or not isinstance(limit,int) or limit<1):raise ValueError('limit must be positive')
        self.directory.mkdir(parents=True,exist_ok=True)
        competition=competition_by_name(competition_name);canonical=season_key(season_name)
        sofa=self.clients.get('sofascore')
        timing=get_job(self.directory,'fotmob_players' if self.baseline_provider=='fotmob' else
                       'combined' if len(self.providers)>1 else 'sofascore',
                       competition.name,season_label(canonical),report=self.report)
        self.timing=timing
        timing.begin();timing.set_provider(self.baseline_provider)
        def timing_mode():
            return (str(getattr(sofa,'active','http')),self.providers,self.spatial)
        if self.baseline_provider=='sofascore':
            season=self._fetch('sofascore',f'season/{competition.sofascore_id}/{canonical}', lambda:resolve_sofascore_season(sofa,competition,canonical))
            competition_id=competition.sofascore_id
            events=self._fetch('sofascore',f'schedule/{competition_id}/{season["id"]}', lambda:list(sofa.iter_season_events(competition_id,season['id'])))
            events=with_reviewed_schedule_results(competition,canonical,season['id'],events)
            # Check scope rather than quietly accepting a wrong provider selection.
            for event in events:
                if (event.get('season',{}).get('id')!=season['id'] or
                        event.get('tournament',{}).get('uniqueTournament',{}).get('id')!=competition_id):
                    raise ValueError('Sofascore returned fixtures outside the requested season')
            config={'schema_version':1,'competition':competition.name,'competition_id':competition_id,
                    'season':canonical,'season_id':season['id']}
        else:
            competition_id=competition.fotmob_id
            season={'id':int(canonical.split('/')[0]),'name':canonical}
            events_by_id={}
            for selected_id in (competition_id,*competition.fotmob_supplemental_ids):
                raw=self._fetch('fotmob',f'fixtures/{selected_id}/{canonical}',
                    lambda selected_id=selected_id:
                        self.clients['fotmob'].season_events(selected_id,canonical))
                for row in raw:
                    if row['status'].get('finished') and not row['status'].get('awarded'):
                        fixture=fotmob_fixture(row);events_by_id[fixture['id']]=fixture
            events=list(events_by_id.values())
            config={'schema_version':1,'competition':competition.name,'competition_id':competition_id,
                    'season':canonical,'season_id':season['id'],
                    'baseline_provider':'fotmob','id_namespace':'fotmob'}
        path=self.directory/'config.json'
        if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=config:raise ValueError('Output folder belongs to another season')
        atomic_json(path,config)
        events=sorted((e for e in events if e['status']['type']=='finished'),key=lambda e:(e['startTimestamp'],e['id']))
        from .selection import in_competition
        scope={'events':events if not competition.parent else [e for e in events if in_competition(e,competition)]}
        scope_path=self.directory/'fixture-scopes.json'
        if not scope_path.exists() or json.loads(scope_path.read_text(encoding='utf-8'))!=scope:atomic_json(scope_path,scope)
        events=[e for e in events if in_competition(e,competition)]
        if len({e['id'] for e in events})!=len(events):raise ValueError('Duplicate baseline fixture IDs')
        atomic_json(self.directory/'schedule.json',{'fetched_at':now(),'events':events})
        selected=events[:limit] if limit else events
        self.report(f'{competition.name} {canonical}: {len(events)} finished matches; collecting {len(selected)}.')
        fixtures,disabled=self._fixtures(competition,canonical,selected) if selected else ({},{})
        import time
        discovery_retries={p:0 for p in disabled}
        retry_at={p:time.monotonic()+300 for p in disabled}
        atomic_json(self.directory/'provider_discovery.json',{'errors':disabled,'counts':{k:len(v) for k,v in fixtures.items()}})
        status_path=self.directory/'collection.csv'
        old=pd.read_csv(status_path,keep_default_na=False).to_dict('records') if status_path.exists() else []
        if competition.name in ('Champions League','Europa League'):
            qualifying_ids={str(e['id']) for e in scope['events'] if is_qualification_event(e)}
            legacy=[r for r in old if str(r['match_id']) in qualifying_ids]
            if legacy:
                archive=self.directory/'qualifier-attempts.json'
                retained=json.loads(archive.read_text(encoding='utf-8')) if archive.exists() else {}
                retained.update({str(r['match_id']):r for r in legacy});atomic_json(archive,retained)
                old=[r for r in old if str(r['match_id']) not in qualifying_ids]
        statuses={int(r['match_id']):r for r in old}
        for row in statuses.values():row['competition']=competition.name
        for event in selected:
            statuses.setdefault(event['id'],{'match_id':event['id'],'competition':competition.name,
                'status':'pending','attempts':0,'updated_at':'','error':''})
        # CSV completion predates newer resource requirements. Every selected
        # checkpoint must be checked before it can count as finished this pass.
        timing.schedule(selected,mode=timing_mode())
        loaders={'fotmob':FotMobLoader(identity_resolver=self.identity_resolver),
                 'whoscored':WhoScoredLoader(identity_resolver=self.identity_resolver)}
        for index,event in enumerate(selected,1):
            if hasattr(sofa,'register_event'):sofa.register_event(event)
            self.report(f"{competition.name} {season_label(canonical)} | match {event['id']} | {event['homeTeam']['name']} vs {event['awayTeam']['name']}")
            timing.start_game(event['id'],clients=self.clients.values())
            mid=event['id'];store=PandasStore(self.directory/'match_checkpoints'/str(mid));entry=statuses[mid]
            if competition.parent and not store.exists:
                from .qualifiers import reuse_checkpoint
                reuse_checkpoint(self.directory.parent.parent,competition,season_label(canonical),mid)
            from .ensembleProgress import remote_done, record_match, ACTIVE
            required=['provider:'+provider for provider in self.providers]+['spatial:'+category for category in self.spatial]
            if self.baseline_provider=='sofascore':required.append('sofascore_match_stats')
            coordinated=ACTIVE.get() is not None and ACTIVE.get().trusted_only
            if (not store.exists or coordinated) and not self.refresh and remote_done('sofascore',competition.name,canonical,mid,required):
                from .workInstructions import reviewed_by_main
                reviewed=reviewed_by_main(ACTIVE.get(),competition.name,canonical,mid) if coordinated else False
                entry.update(status='reviewed' if reviewed else 'elsewhere',
                             error='Main accepted unavailable resources' if reviewed else 'Completed on another computer; raw data not present locally',updated_at=now())
                atomic_csv(pd.DataFrame(statuses.values()),status_path)
                self.report(f'[{index}/{len(selected)}] '+('Main confirmed reviewed exceptions; ' if reviewed else 'Main confirms required data is held; ' if coordinated else 'Recorded on another computer; ')+f'skipped {mid}.')
                timing.finish_game(clients=self.clients.values(),status=entry['status'],cached=True,mode=timing_mode())
                continue
            resuming=store.exists and not self.refresh
            if resuming:
                dataset=store.from_csv();match=dataset.matches[mid]
                entry['attempts']=max(int(entry.get('attempts',0)),int(match.metadata.get('collection_evidence',{}).get('attempts',0)))
                # Only a fully complete provider is terminal. Partial and failed
                # enrichments are deliberately revisited on resume so identity
                # aliases, decoder fixes, and previously skipped sources can
                # backfill the checkpoint from cached or newly fetched data.
                if (all(match.metadata.get('provider_status',{}).get(p)=='complete' for p in self.providers)
                        and self._spatial_done(match)
                        and (self.baseline_provider!='sofascore' or sofascore_stats_complete(match))):
                    entry.update(status='complete',error='')
                    if match.metadata.get('browser_details') is not None:
                        self.browser_coverage[str(mid)]={**match.metadata['browser_details'],
                            'competition':match.season.competition.name,'season':season_label(match.season.name),
                            'home':match.home_team.name,'away':match.away_team.name}
                        self._write_collection_coverage(match)
                    self.report(f'[{index}/{len(selected)}] Already complete: {mid}')
                    timing.finish_game(clients=self.clients.values(),cached=True,mode=timing_mode())
                    continue
            else:
                try:
                    if self.baseline_provider=='sofascore':
                        payload=self._fetch('sofascore',f'lineups/{mid}',lambda:sofa.lineups(mid))
                        if not all(payload.get(s,{}).get('players') for s in ('home','away')):raise ValueError('Both populated Sofascore lineups are required')
                        loader=SofaScoreLoader(None);local_season=loader.select_season(competition_id,competition.name,season['id'],season['name'])
                        match=loader._build_match(local_season,event,payload);dataset=loader.dataset
                        match.metadata['provider_status']={'sofascore':'complete'}
                        match.metadata['provider_match_ids']={'sofascore':mid}
                        for lineup in match.lineups:
                            for p in lineup.players:
                                values=synthesize(p.stats.raw,{},p.starter or p.did_play)
                                p.stats.raw['_combined']=values['metrics'];p.stats.raw['_combined_sources']=values['sources']
                                p.stats.actions=values['actions']
                    else:
                        payload=self._fetch_fotmob_match(mid,self.clients['fotmob'])
                        loader=FotMobLoader(identity_resolver=self.identity_resolver)
                        local_season=loader.select_season(competition_id,competition.name,season['id'],season['name'])
                        match=loader.load_match(payload,local_season,event);dataset=loader.dataset
                        match.metadata['provider_status']={'fotmob':'complete'}
                        match.metadata.setdefault('raw_response_files',{})['fotmob']=str(
                            self.cache.path('fotmob',f'match/{mid}').relative_to(self.directory))
                except DATA_ERRORS as exc:
                    entry.update(status='failed',error=str(exc),updated_at=now(),attempts=int(entry.get('attempts',0))+1)
                    atomic_csv(pd.DataFrame(statuses.values()),status_path)
                    from .ensembleProgress import record_failure
                    record_failure(competition.name,canonical,event,entry)
                    self.report(f'[{index}/{len(selected)}] {mid}: failed ({exc})')
                    timing.finish_game(clients=self.clients.values(),status='failed',mode=timing_mode())
                    if getattr(sofa,'requires_manual_attention',False) is True:
                        self._sofascore_blocked=True
                        self.report('Chrome extension needs attention; saved checkpoints and paused the season.')
                        break
                    if isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429):
                        if exc.status_code==403:self.report(f'{self.baseline_provider} request safety stop triggered; collection stopped.')
                        break
                    continue
            errors=[]
            if self.baseline_provider=='sofascore':timing.set_provider('sofascore')
            browser_done=resuming and sofascore_stats_complete(match)
            if self.baseline_provider=='sofascore' and getattr(sofa,'captures_details',False) and not browser_done:
                errors.extend(self._browser_match_details(sofa,event,match));browser_done=True
            if self.baseline_provider=='sofascore' and not self._sofascore_blocked:
                errors.extend(self._collect_spatial(match,event,sofa))
                if getattr(sofa,'requires_manual_attention',False) is True:self._sofascore_blocked=True
                # Fallback can begin on an optional spatial request after HTTP
                # lineups succeeded. The same match still needs full browser data.
                if getattr(sofa,'captures_details',False) and not browser_done and not self._sofascore_blocked:
                    errors.extend(self._browser_match_details(sofa,event,match))
                    if not self._sofascore_blocked:errors.extend(self._collect_spatial(match,event,sofa))
            for provider in self.providers:
                if provider==self.baseline_provider:continue
                if match.metadata['provider_status'].get(provider)=='complete' and not self.refresh:continue
                if (provider in discovery_retries and provider in disabled and discovery_retries[provider]<2
                        and time.monotonic()>=retry_at[provider]
                        and not any(code in disabled[provider] for code in ('401','403','429'))
                        and getattr(self.clients[provider],'requires_manual_attention',False) is not True):
                    self.report(f'{provider}: retrying unavailable fixture discovery before this match.')
                    recovered,unavailable=self._fixtures(competition,canonical,selected,only={provider})
                    discovery_retries[provider]+=1;retry_at[provider]=time.monotonic()+900
                    if recovered.get(provider):fixtures[provider]=recovered[provider]
                    if provider not in unavailable:disabled.pop(provider,None)
                    else:disabled[provider]=unavailable[provider]
                    atomic_json(self.directory/'provider_discovery.json',{'errors':disabled,'counts':{k:len(v) for k,v in fixtures.items()}})
                if provider in disabled:
                    errors.append(f'{provider}: {disabled[provider]}');match.metadata['provider_status'][provider]='failed';continue
                timing.set_provider(provider)
                try:
                    fixture=match_fixture(event,fixtures[provider],provider=provider,review_root=self.identity_resolver.review_root,baseline=self.baseline_provider);pid=fixture['id'];client=self.clients[provider]
                    if provider=='whoscored' and not self.refresh and not self.cache.path(provider,f'match/{pid}').exists():
                        from .whoScoredData import reuse_match
                        reuse_match(self.directory,pid,fixture)
                    payload=(self._fetch_fotmob_match(pid,client) if provider=='fotmob' else
                             self._fetch(provider,f'match/{pid}',lambda:client.match_details(pid)))
                    loaders[provider].enrich(match,payload,fixture,allow_partial=True)
                    mismatches=match.metadata.get('identity_mismatches',{}).get(provider,[])
                    match.metadata['provider_status'][provider]='partial' if mismatches else 'complete'
                    if mismatches:
                        errors.append(f'{provider}: {len(mismatches)} player identity mismatch(es)')
                    match.metadata.setdefault('raw_response_files',{})[provider]=str(self.cache.path(provider,f'match/{pid}').relative_to(self.directory))
                except DATA_ERRORS as exc:
                    errors.append(f'{provider}: {exc}');match.metadata['provider_status'][provider]='failed'
                    if isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429):disabled[provider]=str(exc)
            # An I/O failure propagates; never record an unsaved match as complete.
            if hasattr(sofa,'audit'):
                match.metadata['collection_transport']={key:value for key,value in sofa.audit.items()
                    if key==f'event/{mid}' or key.startswith(f'event/{mid}/')}
            from .reporting import redact
            match.metadata['collection_evidence']={'attempts':int(entry.get('attempts',0))+1,
                'label':event['homeTeam']['name']+' vs '+event['awayTeam']['name'],
                'error':redact('; '.join(errors))[:2000],
                'qualifying':bool(competition.parent)}
            store.to_csv(dataset)
            # Raw files and the checkpoint now hold every successful provider result.
            if all(match.metadata.get('provider_status',{}).get(p)=='complete' for p in self.providers):
                for provider,client in self.clients.items():
                    provider_id=match.metadata.get('provider_match_ids',{}).get(provider,mid)
                    for transport in (getattr(client,'http',None),getattr(client,'extension',None)):
                        cache=getattr(transport,'cache',None)
                        if cache is not None:cache.release_match(provider_id)
                responses=getattr(sofa,'responses',None)
                if isinstance(responses,dict):responses.clear()
            after=network_count(self.clients.values())
            record_match(match,collected_here=timing.before is None or after is None or after>timing.before)
            entry.update(status='complete' if not errors else 'partial',error='; '.join(errors),
                         updated_at=now(),attempts=int(entry.get('attempts',0))+1)
            if str(mid) in self.browser_coverage:self._write_collection_coverage(match)
            atomic_csv(pd.DataFrame(statuses.values()),status_path)
            self.report(f'[{index}/{len(selected)}] {event["homeTeam"]["name"]} vs {event["awayTeam"]["name"]}: {entry["status"]}'+(f' ({entry["error"]})' if errors else ''))
            if not self._sofascore_blocked:self.profile_verifier.pending(limit=1)
            timing.finish_game(clients=self.clients.values(),status=entry['status'],mode=timing_mode())
            if self._sofascore_blocked:
                self.report('Chrome extension recovery could not finish; saved the current checkpoint. Inspect the extension diagnostic log and resume collection.'
                    if getattr(sofa,'requires_manual_attention',False) is True else
                    'SofaScore request safety stop triggered; saved the current checkpoint and stopped collection.')
                break
        atomic_csv(pd.DataFrame(statuses.values(),columns=['match_id','competition','status','attempts','updated_at','error']),status_path)
        if build_csvs:
            self.report(f'[{competition.name} {canonical}] Rebuilding combined exports...')
            self._export(events,compile_events=compile_events)
            self.report(f'[{competition.name} {canonical}] Export complete.')
        else:
            self.report(f'[{competition.name} {canonical}] Combined CSV rebuild skipped.')
        result=pd.DataFrame([statuses[e['id']] for e in selected],
            columns=['match_id','competition','status','attempts','updated_at','error'])
        result.attrs['sofascore_blocked']=self._sofascore_blocked
        result.attrs['baseline_provider']=self.baseline_provider
        atomic_json(self.directory/'run_summary.json',{'competition':competition.name,'season':canonical,
            'finished_matches_discovered':len(events),'selected_matches':len(selected),
            'complete':int(result.status.eq('complete').sum()),'partial':int(result.status.eq('partial').sum()),
            'elsewhere':int(result.status.eq('elsewhere').sum()),
            'failed_or_pending':int(result.status.isin(['failed','pending']).sum()),
            'baseline_provider':self.baseline_provider,'providers':list(self.providers),
            'csvs_built':bool(build_csvs),'events_csv_compiled':bool(build_csvs and compile_events)})
        timing.end(0 if len(result) and result.status.isin(('complete','elsewhere','reviewed')).all() else 2)
        return result

    def _browser_match_details(self,sofa,event,match):
        mid=event['id']
        try:
            payload=self._fetch('sofascore',f'lineups/{mid}',lambda:sofa.lineups(mid))
            detailed,coverage=sofa.collect_details(event,payload)
            self._apply_browser_details(match,detailed)
            self._fetch('sofascore',f'lineups/{mid}',lambda:detailed,refresh=True)
            if hasattr(sofa,'match_payloads'):
                for label,payload in sofa.match_payloads(mid).items():
                    key=f'{label}/{mid}'
                    self._fetch('sofascore',key,lambda payload=payload:payload,refresh=True)
                    match.metadata.setdefault('raw_response_files',{})['sofascore_'+label]=str(self.cache.path('sofascore',key).relative_to(self.directory))
            match.metadata['browser_details']={**coverage,'complete':not coverage['errors']}
            errors=coverage['errors']
            if coverage.get('blocked'):self._sofascore_blocked=True
        except DATA_ERRORS as exc:
            errors=[f'browser match details: {exc}']
            match.metadata['browser_details']={'complete':False,'errors':[str(exc)]}
            if isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429):self._sofascore_blocked=True
        if getattr(sofa,'requires_manual_attention',False) is True:self._sofascore_blocked=True
        self.browser_coverage[str(mid)]={**match.metadata['browser_details'],
            'competition':match.season.competition.name,'season':season_label(match.season.name),
            'home':match.home_team.name,'away':match.away_team.name}
        return errors

    def _write_collection_coverage(self, match):
        """Human-readable resource coverage alongside the detailed checkpoint."""
        from copy import deepcopy
        current=self.browser_coverage[str(match.id)]
        spatial=deepcopy(match.metadata.get('sofascore_spatial_status',{}))
        for category in self.spatial:
            if category in ('heatmap','rating_breakdown'):
                for lineup in match.lineups:
                    for appearance in lineup.players:
                        if appearance.did_play:spatial.setdefault(str(appearance.player.id),{}).setdefault(category,'not_attempted')
            else:spatial.setdefault(category,'not_attempted')
        current['spatial_status']=spatial
        rows=[]
        for mid,record in self.browser_coverage.items():
            resources={**record.get('coverage',{}),
                **{key:'skipped_not_played' for key in record.get('skipped',{})}}
            resources.setdefault('lineups','complete')
            for resource,status in resources.items():
                rows.append({'match_id':mid,'competition':record.get('competition',''),
                    'season':record.get('season',''),'home':record.get('home',''),
                    'away':record.get('away',''),'resource':resource,'status':status,
                    'reason':record.get('reasons',{}).get(resource,
                        'Player did not play' if status=='skipped_not_played' else
                        'Collection paused before this request' if status=='not_attempted' else '')})
        # Include spatial categories from every saved coverage record, not just
        # the current match, so a resumed run retains earlier observations.
        for mid,record in self.browser_coverage.items():
            for key,value in record.get('spatial_status',{}).items():
                if key.startswith('_'):continue
                values=value.items() if isinstance(value,dict) else [(key,value)]
                for category,status in values:
                    if not isinstance(status,str):continue
                    rows.append({'match_id':mid,'competition':record.get('competition',''),
                        'season':record.get('season',''),'home':record.get('home',''),
                        'away':record.get('away',''),'resource':f'player/{key}/{category}' if isinstance(value,dict) else category,
                        'status':status,'reason':{
                            'not_attempted':'Collection paused before this request',
                            'unavailable':'Provider has no data for this resource',
                            'failed':'See checkpoint and extension diagnostic log',
                            'deferred':'Deferred by configured collection strategy'}.get(status,'')})
        atomic_csv(pd.DataFrame(rows,columns=['match_id','competition','season','home','away','resource','status','reason']),
                   self.directory/'collection_coverage.csv')
        atomic_json(self.directory/'browser_coverage.json',self.browser_coverage)
        counts={}
        for row in rows:
            if str(row['match_id'])==str(match.id):counts[row['status']]=counts.get(row['status'],0)+1
        self.report(f"[Coverage] {match.season.competition.name} {season_label(match.season.name)} | match {match.id} | "+
            ', '.join(f'{status}={count}' for status,count in sorted(counts.items())))
        self.report(f'Coverage report: {self.directory/"collection_coverage.csv"}')

    @staticmethod
    def _apply_browser_details(match,lineups):
        for side in ('home','away'):
            lineup=getattr(match,side+'_lineup')
            if {p['player']['id'] for p in lineups[side]['players']}!={p.player.id for p in lineup.players}:
                raise ValueError('Browser detail lineup differs from checkpoint identities')
        for side in ('home','away'):
            lineup=getattr(match,side+'_lineup')
            entries={p['player']['id']:p for p in lineups[side]['players']}
            for appearance in lineup.players:
                stats=entries[appearance.player.id].get('statistics',{})
                appearance.stats.raw.update(stats)
                if stats.get('minutesPlayed') is not None:appearance.minutes_played=stats['minutesPlayed']
                providers=appearance.stats.raw.get('_providers',{})
                combined=synthesize(appearance.stats.raw,providers,appearance.starter or appearance.did_play)
                appearance.stats.raw['_combined']=combined['metrics']
                appearance.stats.raw['_combined_sources']=combined['sources']
                appearance.stats.raw['_source_differences']=combined['differences']
                appearance.stats.actions=combined['actions']

    def rebuild_cached(self):
        """Re-decode a FotMob-only collection from saved responses, without HTTP.

        Old checkpoint snapshots remain available. Missing per-player statistics
        are surfaced in data_quality.csv rather than dropping the whole fixture.
        """
        config=json.loads((self.directory/'config.json').read_text(encoding='utf-8'))
        if self.baseline_provider!='fotmob' or self.providers!=('fotmob',) or config.get('baseline_provider')!='fotmob':
            raise ValueError('Cached rebuild requires a FotMob-only baseline collection')
        events=json.loads((self.directory/'schedule.json').read_text(encoding='utf-8'))['events']
        statuses=[]
        for index,event in enumerate(events,1):
            mid=event['id'];path=self.cache.path('fotmob',f'match/{mid}')
            if not path.exists():
                raise ValueError(f'Missing cached FotMob response for {mid}; no network request was made')
            saved=read_json(path)
            if saved.get('provider')!='fotmob' or saved.get('key')!=f'match/{mid}':
                raise ValueError('Cached rebuild response identity mismatch')
            loader=FotMobLoader(identity_resolver=self.identity_resolver)
            season=loader.select_season(config['competition_id'],config['competition'],config['season_id'],config['season'])
            match=loader.load_match(saved['payload'],season,event)
            match.metadata['provider_status']={'fotmob':'complete'}
            match.metadata['raw_response_files']={'fotmob':str(path.relative_to(self.directory))}
            PandasStore(self.directory/'match_checkpoints'/str(mid)).to_csv(loader.dataset)
            statuses.append({'match_id':mid,'competition':config['competition'],'status':'complete',
                'attempts':0,'updated_at':now(),'error':''})
            if index%50==0:self.report(f'Rebuilt {index}/{len(events)} cached fixtures.')
        self._export(events)
        # Rebuilding is not a fresh collection attempt; retain historical attempt counts.
        old_path=self.directory/'collection.csv'
        old={int(r['match_id']):r for r in pd.read_csv(old_path,keep_default_na=False).to_dict('records')} if old_path.exists() else {}
        for row in statuses:row['attempts']=old.get(row['match_id'],{}).get('attempts',0)
        atomic_csv(pd.DataFrame(statuses),old_path)
        summary_path=self.directory/'run_summary.json'
        summary=json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
        summary.update(complete=len(events),partial=0,failed_or_pending=0,
                       rebuilt_from_cache_at=now(),data_quality_warnings=len(self.data_quality_warnings))
        atomic_json(summary_path,summary)
        return summary

    def _export(self,events,*,compile_events=True):
        started=time.monotonic();clear_json_cache()
        config=json.loads((self.directory/'config.json').read_text(encoding='utf-8'))
        competition=competition_by_name(config['competition'])
        events=with_reviewed_schedule_results(
            competition,config['season'],config['season_id'],events)
        events=sorted(events,key=lambda event:(event['startTimestamp'],event['id']))
        schedule_path=self.directory/'schedule.json'
        fetched_at=(json.loads(schedule_path.read_text(encoding='utf-8')).get('fetched_at')
                    if schedule_path.exists() else now())
        atomic_json(schedule_path,{'fetched_at':fetched_at,'events':events})
        from .selection import in_competition
        export_events=[event for event in events if in_competition(event,competition)]
        excluded=len(events)-len(export_events);total=len(export_events)
        def progress(message):
            self.report(message)
            sys.stdout.flush()
        if excluded:
            progress(f'[export] Retained {excluded} qualification fixtures in schedule.json; excluded them from CSVs.')
        progress(f'[export 1/5] Reading {total} match checkpoints...')
        frames=[]
        for index,event in enumerate(export_events,1):
            store=PandasStore(self.directory/'match_checkpoints'/str(event['id']))
            if store.exists:frames.append(store.read_frames())
            if index==total or index%50==0:
                progress(f'[export 1/5] Read {index}/{total} schedules; {len(frames)} checkpoints available.')
        progress(f'[export 2/5] Combining {len(frames)} checkpoints into the season dataset...')
        if frames:
            merged={name:pd.concat([f[name] for f in frames],ignore_index=True).drop_duplicates(KEYS[name],keep='last') for name in frames[0]}
            self.dataset=PandasStore.from_frames(merged)
        else:
            self.dataset=FootballDataset()
        self._include_awarded_results(export_events)
        # Checkpoints intentionally preserve the decoder output that existed when
        # a match was collected. Re-decode cached provider responses here so an
        # offline export picks up later mappings without scraping the match again.
        events_by_id={event['id']:event for event in export_events}
        progress('[export 3/5] Recovering and refreshing cached provider data...')
        cached_fixtures=self._cached_provider_fixtures()
        matches=list(self.dataset.matches.values());match_total=len(matches)
        for index,match in enumerate(matches,1):
            event=events_by_id.get(match.id)
            if event is not None:
                self._recover_cached_enrichments(match,event,cached_fixtures)
            self._refresh_match_metrics(match)
            if index==match_total or index%25==0:
                progress(f'[export 3/5] Refreshed {index}/{match_total} matches.')
        progress('[export 4/5] Writing the combined relational dataset snapshot...')
        PandasStore(self.directory/'dataset').to_csv(self.dataset)
        context_columns=['match_id','date','season','competition','team','opponent','player_id','player_name','starter','position','minutes_played']
        rows=[];sources=[];differences=[];identities=[];spatial_maps=[];passes=[];dribbles=[]
        shots=[];fotmob_raw=[];fotmob_teams=[];sofascore_teams=[];quality=[]
        whoscored_events=[];identity_mismatches=[];lineup_differences=[];team_results=[]
        progress('[export 5/5] Building flat analysis CSVs...')
        ordered=sorted(self.dataset.matches.values(),key=lambda m:(m.kickoff,m.id))
        for match_index,match in enumerate(ordered,1):
            for provider,mismatches in match.metadata.get('identity_mismatches',{}).items():
                for mismatch in mismatches:
                    identity_mismatches.append({'match_id':match.id,
                        'date':match.kickoff.date().isoformat(),'season':season_label(match.season.name),
                        'competition':match.season.competition.name,
                        **mismatch})
            for provider,differences_for_provider in match.metadata.get('lineup_differences',{}).items():
                for difference in differences_for_provider:
                    lineup_differences.append({'match_id':match.id,
                        'date':match.kickoff.date().isoformat(),'season':season_label(match.season.name),
                        'competition':match.season.competition.name,
                        **difference})
            saved=match_payload(match,self.directory)
            shot_rows,stat_rows,team_rows=source_rows(match,saved)
            shots.extend(shot_rows);fotmob_raw.extend(stat_rows);fotmob_teams.extend(team_rows)
            sofascore_teams.extend(sofascore_team_rows(match,self.directory))
            team_results.extend(_team_result_rows(match,events_by_id.get(match.id,{}),team_rows))
            spatial_maps.extend(match_map_rows(match,self.directory))
            if compile_events:
                whoscored_events.extend(whoscored_event_rows(
                    match,whoscored_match_payload(match,self.directory)))
            registered={str(p.stats.raw.get('_providers',{}).get('fotmob',{}).get('id'))
                        for lineup in match.lineups if lineup is not None for p in lineup.players}
            content=(saved or {}).get('payload',{}).get('content',{})
            for pid,entry in (content.get('playerStats') or {}).items():
                if str(pid) not in registered:
                    quality.append({'match_id':match.id,'date':match.kickoff.date().isoformat(),
                        'season':season_label(match.season.name),'competition':match.season.competition.name,
                        'player_name':entry.get('name'),
                        'team':entry.get('teamName'),'provider':'fotmob','provider_player_id':int(pid),
                        'issue':'player_stats_not_in_lineup'})
            for lineup in match.lineups:
                if lineup is None:continue
                for p in lineup.players:
                    context={'match_id':match.id,'date':match.kickoff.date().isoformat(),'season':season_label(match.season.name),
                        'competition':match.season.competition.name,
                        'team':p.team.name,'opponent':p.opponent.name,'player_id':p.player.id,'player_name':p.player.name,
                        'starter':p.starter,'position':p.position,'minutes_played':p.minutes_played}
                    row={**context,**p.stats.raw.get('_combined',{}),
                         'team_id':p.team.id,'opponent_id':p.opponent.id}
                    appeared=p.starter or p.did_play
                    row['appeared']=appeared
                    fot=p.stats.raw.get('_providers',{}).get('fotmob',{})
                    profile=fot.get('lineup_profile',{})
                    row.update(fotmob_player_stats_status=fot.get('data_status'),
                        shirt_number=p.shirt_number,fotmob_opta_id=fot.get('opta_id'),
                        fotmob_position_id=profile.get('positionId'),fotmob_age_reported=profile.get('age'),
                        fotmob_country=profile.get('countryName'),fotmob_market_value=profile.get('marketValue'))
                    if fot.get('data_status')=='missing_player_stats':
                        quality.append({**context,'provider':'fotmob','provider_player_id':fot['id'],
                            'issue':'missing_player_stats','appeared':appeared})
                    for name,action in p.stats.actions.items():row[name+'_completion_rate']=action.rate
                    heat_summary=extract_minimal_spatial(match,p,self.directory)
                    rows.append({**row,**heat_summary})
                    spatial_maps.extend(player_map_rows(match,p,self.directory))
                    passes.extend({**context,**pass_row}
                                  for pass_row in extract_raw_passes(match,p,self.directory))
                    dribbles.extend({**context,**dribble_row}
                                    for dribble_row in extract_raw_dribbles(match,p,self.directory))
                    for metric,source in p.stats.raw.get('_combined_sources',{}).items():
                        sources.append({**context,'metric':metric,'provider':source,'value':row.get(metric)})
                    differences.extend({**context,**d} for d in p.stats.raw.get('_source_differences',[]))
                    for provider,entry in p.stats.raw.get('_providers',{}).items():
                        identities.append({**context,'provider':provider,'provider_player_id':entry['id'],'provider_player_name':entry['name'],
                            'provider_match_id':match.metadata['provider_match_ids'][provider],'identity_method':entry['identity_method']})
            if match_index==len(ordered) or match_index%25==0:
                progress(f'[export 5/5] Processed {match_index}/{len(ordered)} matches.')
        atomic_csv(pd.DataFrame(rows).reindex(columns=PLAYER_MATCH_COLUMNS),
                   self.directory/'player_match_stats.csv')
        atomic_csv(pd.DataFrame(sources,columns=context_columns+['metric','provider','value']),
                   self.directory/'provenance.csv')
        atomic_csv(pd.DataFrame(differences,columns=context_columns+[
            'metric','selected_provider','selected_value','other_provider','other_value']),
            self.directory/'source_differences.csv')
        atomic_csv(pd.DataFrame(identities,columns=context_columns+[
            'provider','provider_player_id','provider_player_name','provider_match_id','identity_method']),
            self.directory/'identities.csv')
        mismatch_columns=['match_id','date','season','competition','provider','provider_match_id','side',
            'provider_player_id','provider_player_name','baseline_player_id',
            'baseline_player_name','issue']
        atomic_csv(pd.DataFrame(identity_mismatches,columns=mismatch_columns),
                   self.directory/'identity_mismatches.csv')
        atomic_csv(pd.DataFrame(lineup_differences,columns=mismatch_columns),
                   self.directory/'lineup_differences.csv')
        atomic_csv(pd.DataFrame(spatial_maps,columns=SPATIAL_MAP_COLUMNS),self.directory/'spatial_maps.csv')
        atomic_csv(pd.DataFrame(passes,columns=context_columns+list(RAW_PASS_RECORD_COLUMNS)),self.directory/'passes.csv')
        atomic_csv(pd.DataFrame(dribbles,columns=context_columns+list(RAW_DRIBBLE_RECORD_COLUMNS)),self.directory/'dribbles.csv')
        atomic_csv(pd.DataFrame(shots) if shots else pd.DataFrame(columns=['match_id','competition','provider','provider_match_id','id','playerId','teamId']),self.directory/'shots.csv')
        atomic_csv(pd.DataFrame(fotmob_raw) if fotmob_raw else pd.DataFrame(columns=['match_id','competition','provider_player_id','field','raw_value']),self.directory/'fotmob_raw_player_stats.csv')
        atomic_csv(pd.DataFrame(fotmob_teams) if fotmob_teams else pd.DataFrame(columns=['match_id','date','season','competition','provider','provider_match_id','fetched_at','side','provider_team_id','team','matchweek','period','group','field','label','value','format','type']),self.directory/'fotmob_team_statistics.csv')
        atomic_csv(pd.DataFrame(sofascore_teams,columns=TEAM_STAT_COLUMNS),self.directory/'sofascore_team_statistics.csv')
        atomic_csv(pd.DataFrame([*fotmob_teams,*sofascore_teams],columns=TEAM_STAT_COLUMNS),self.directory/'team_match_statistics.csv')
        atomic_csv(pd.DataFrame(team_results,columns=TEAM_RESULT_COLUMNS),self.directory/'team_match_results.csv')
        atomic_csv(pd.DataFrame(quality,columns=context_columns+['provider','provider_player_id','issue','appeared']),self.directory/'data_quality.csv')
        if compile_events:
            atomic_csv(pd.DataFrame(whoscored_events,columns=EVENT_COLUMNS),self.directory/'events.csv')
        status_path=self.directory/'collection.csv'
        if status_path.exists():
            status=pd.read_csv(status_path,keep_default_na=False)
            status['competition']=self.dataset.matches[next(iter(self.dataset.matches))].season.competition.name if self.dataset.matches else json.loads((self.directory/'config.json').read_text(encoding='utf-8'))['competition']
            columns=['match_id','competition']+[c for c in status.columns if c not in ('match_id','competition')]
            atomic_csv(status[columns],status_path)
        for obsolete in ('player_stats.csv','player_match_features.csv','spatial_features.csv','touches.csv',
                         'pass_features.csv','pass_zone_transitions.csv','dribble_features.csv',
                         'shot_features.csv'):
            (self.directory/obsolete).unlink(missing_ok=True)
        self.data_quality_warnings=quality
        clear_json_cache()
        event_note='including events.csv' if compile_events else 'events.csv compilation skipped'
        progress(f'[export complete] Wrote analysis CSVs ({event_note}) in {time.monotonic()-started:.1f}s.')

    def _include_awarded_results(self,events):
        """Add schedule-only awarded fixtures to result exports without inventing detail data."""
        awarded=[event for event in events if _is_awarded(event)]
        if not awarded:return
        config=json.loads((self.directory/'config.json').read_text(encoding='utf-8'))
        loader=SofaScoreLoader(None,dataset=self.dataset)
        season=loader.select_season(config['competition_id'],config['competition'],
                                    config['season_id'],config['season'])
        for event in awarded:
            match=self.dataset.matches.get(event['id'])
            if match is None:
                match=loader._build_match(season,event,{})
            match.home_score=_event_score(event,'home',official=True)
            match.away_score=_event_score(event,'away',official=True)
            match.round_number=event.get('roundInfo',{}).get('round')
            match.metadata['event']=deepcopy(event)
            match.metadata['awarded']=True
            match.metadata['played_score']={
                'home':_event_score(event,'home',official=False),
                'away':_event_score(event,'away',official=False)}
            match.metadata['official_score']={'home':match.home_score,'away':match.away_score}
            match.metadata['result_source']='sofascore_schedule_award'
            if match.home_lineup is None and match.away_lineup is None:
                match.metadata['data_status']='awarded_result_only'
        # Publish the relational result even when the provider has no lineup endpoint.
        PandasStore(self.directory/'dataset').to_csv(self.dataset)

    def _cached_provider_fixtures(self):
        """Read fixture indexes already on disk; never make a network request."""
        from .whoScoredData import standalone_fixtures
        result={}
        converters={'fotmob':fotmob_fixture,'whoscored':whoscored_fixture}
        for provider,convert in converters.items():
            fixtures={}
            folder=self.directory/'raw'/provider
            for path in folder.glob('*.json') if folder.exists() else ():
                try:
                    saved=read_json(path)
                    if saved.get('provider')!=provider or not saved.get('key','').startswith('fixtures/'):
                        continue
                    for row in saved.get('payload',[]):
                        fixture=convert(row)
                        if fixture['status']['type']=='finished':fixtures[fixture['id']]=fixture
                except (ValueError,KeyError,TypeError):
                    continue
            result[provider]=list(fixtures.values())
        fixtures={row['id']:row for row in standalone_fixtures(self.directory)}
        fixtures.update({row['id']:row for row in result.get('whoscored',[])})
        result['whoscored']=list(fixtures.values())
        return result

    def _recover_cached_enrichments(self,match,event,cached_fixtures):
        """Recover good player rows from provider games that once failed as a unit."""
        if all(lineup is None for lineup in match.lineups):return
        loaders={'fotmob':FotMobLoader(identity_resolver=self.identity_resolver),
                 'whoscored':WhoScoredLoader(identity_resolver=self.identity_resolver)}
        for provider,loader in loaders.items():
            try:
                fixture=match_fixture(event,cached_fixtures.get(provider,[]),provider=provider,review_root=self.identity_resolver.review_root,baseline=self.baseline_provider)
                relative=match.metadata.get('raw_response_files',{}).get(provider)
                path=(self.directory/relative if relative else
                      self.cache.path(provider,f'match/{fixture["id"]}'))
                if not path.exists() and provider=='whoscored':
                    from .whoScoredData import reuse_match
                    reuse_match(self.directory,fixture['id'],fixture)
                if not path.exists():continue
                saved=read_json(path)
                if saved.get('provider')!=provider or saved.get('key')!=f'match/{fixture["id"]}':
                    continue
                loader.enrich(match,saved['payload'],fixture,allow_partial=True)
                mismatches=match.metadata.get('identity_mismatches',{}).get(provider,[])
                match.metadata.setdefault('provider_status',{})[provider]='partial' if mismatches else 'complete'
                match.metadata.setdefault('raw_response_files',{})[provider]=str(path.relative_to(self.directory))
            except DATA_ERRORS:
                continue

    def _refresh_match_metrics(self,match):
        """Rebuild provider metrics from immutable raw responses for offline export."""
        decoded={}
        for provider,loader_class in (('fotmob',FotMobLoader),('whoscored',WhoScoredLoader)):
            relative=match.metadata.get('raw_response_files',{}).get(provider)
            if not relative:continue
            path=self.directory/relative
            if not path.exists():continue
            saved=read_json(path)
            expected=match.metadata.get('provider_match_ids',{}).get(provider)
            if saved.get('provider')!=provider or saved.get('key')!=f'match/{expected}':
                raise ValueError(f'{provider} export cache identity mismatch')
            event={'startTimestamp':int(match.kickoff.timestamp())}
            current=loader_class(identity_resolver=self.identity_resolver).decode(saved['payload'],event)
            decoded[provider]={str(entry['id']):entry for side in ('home','away')
                               for entry in current[side]['players']}
        standalone=match.metadata.get('id_namespace','sofascore')!='sofascore'
        for lineup in match.lineups:
            if lineup is None:continue
            for appearance in lineup.players:
                providers=deepcopy(appearance.stats.raw.get('_providers',{}))
                for provider,entries in decoded.items():
                    previous=providers.get(provider)
                    if not previous:continue
                    current=entries.get(str(previous.get('id')))
                    if current is None:
                        raise ValueError(f'{provider}: cached player is no longer present in decoded lineup')
                    providers[provider]={**current,'identity_method':previous.get('identity_method')}
                appeared=(appearance.starter or appearance.did_play or
                          any(entry.get('appeared',False) for entry in providers.values()))
                combined=synthesize(appearance.stats.raw,providers,appeared,standalone)
                appearance.stats.raw['_providers']=providers
                appearance.stats.raw['_combined']=combined['metrics']
                appearance.stats.raw['_combined_sources']=combined['sources']
                appearance.stats.raw['_source_differences']=combined['differences']
                appearance.stats.actions=combined['actions']
                if appearance.minutes_played is None:
                    appearance.minutes_played=combined['metrics'].get('minutes_played')
