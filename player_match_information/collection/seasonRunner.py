"""Resumable full-season collection, with independent per-match checkpoints."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import pandas as pd
from api import SofaScoreLoader, FotMobLoader, WhoScoredLoader, ProviderError, ProviderRequestError
from api.cache import ResponseCache
from api.events import fotmob_fixture, whoscored_fixture
from api.fotMobClient import without_incident_stream
from api.identity import IdentityResolver, match_fixture
from api.statistics import synthesize
from models import FootballDataset
from storage import PandasStore
from storage.pandasStore import atomic_json, KEYS
from .runner import atomic_csv, now
from .selection import (competition_by_name, resolve_sofascore_season, season_key,
                        season_label, months_between, is_qualification_event,
                        with_reviewed_schedule_results)
from .heatmap import extract_heatmap
from .jsonFiles import clear_json_cache, read_json

DATA_ERRORS=(ProviderError,ValueError,KeyError,TypeError)


def _is_awarded(event):
    return bool(event.get('isAwarded') or event.get('status',{}).get('awarded'))


def _event_score(event,side,*,official):
    score=event.get(side+'Score',{})
    for key in (('display','normaltime','current') if official else ('current','normaltime','display')):
        if score.get(key) is not None:return score[key]
    return None


SPATIAL_ENDPOINTS = ('heatmap', 'rating_breakdown', 'average_positions', 'shotmap')


class SeasonRunner:
    def __init__(self,clients,directory,*,providers=('sofascore','fotmob','whoscored'),
                 identity_map=None,refresh=False,spatial=SPATIAL_ENDPOINTS,
                 spatial_clients=None,baseline_provider='sofascore',report=print):
        if baseline_provider != 'sofascore':
            raise ValueError('FotMob-only backup collection was removed; use the Sofascore baseline')
        self.clients=clients;self.directory=Path(directory)
        self.baseline_provider='sofascore'
        self.providers=tuple(dict.fromkeys(('sofascore',*providers)))
        if set(self.providers)-{'sofascore','fotmob','whoscored'}:raise ValueError('Unknown provider')
        if set(self.providers)-clients.keys():raise ValueError('Missing requested provider client')
        self.cache=ResponseCache(self.directory/'raw')
        self.identity_resolver=IdentityResolver(identity_map)
        self.refresh=refresh;self.report=report
        self.spatial=tuple(dict.fromkeys(spatial))
        if set(self.spatial)-set(SPATIAL_ENDPOINTS):raise ValueError('Unknown Sofascore spatial endpoint')
        self.spatial_clients=tuple(spatial_clients or (clients['sofascore'],))
        if not self.spatial_clients:raise ValueError('At least one Sofascore spatial client is required')
        self._sofascore_blocked=False
        self.capability_path=self.directory/'capabilities.json'
        self.capabilities=json.loads(self.capability_path.read_text()) if self.capability_path.exists() else {}

    def _fetch(self,provider,key,request,*,refresh=False):
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
        workers=1  # One request at a time, including spatial requests.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0,len(requests),workers):
                batch=requests[offset:offset+workers]
                futures=[pool.submit(self._player_spatial_request,self.spatial_clients[index],
                                     match.id,pid,name)
                         for index,(name,pid) in enumerate(batch)]
                results=[future.result() for future in futures]
                for pid,name,state,path,exc in results:
                    player_status=status.setdefault(str(pid),{})
                    player_files=files.setdefault(str(pid),{})
                    player_status[name]=state
                    if path is not None:player_files[name]=path
                    if exc is not None and exc.status_code!=404:
                        errors.append(f'{name} player {pid}: {exc}')
                    if exc is not None and exc.status_code in (401,403,429):blocked=True
                    if exc is not None and exc.status_code==403:self._sofascore_blocked=True
                    completed.append((pid,name,state,exc))
                if blocked:break
        return errors,blocked,completed

    def _fixtures(self,competition,season,selected):
        result={};errors={}
        for provider in self.providers:
            if provider==self.baseline_provider:continue
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
                    paths=(competition.whoscored_path,*competition.whoscored_supplemental_paths)
                    for competition_path in paths:
                        seasons=self._fetch(provider,f'seasons/{competition_path}',
                            lambda competition_path=competition_path:
                                client.seasons(competition_path))
                        found=[s for s in seasons if s['name']==season]
                        if len(found)!=1:
                            raise ValueError(f'WhoScored season {season} is not uniquely available for {competition_path}')
                        path=found[0]['path']
                        # European cups need every stage-option, not merely the
                        # landing page's selected stage. Version their key so runs
                        # created before multi-stage parsing automatically refresh.
                        multi_stage=competition.name in ('Champions League','Europa League')
                        stage_key=('stages-v2/' if multi_stage else 'stage/')+path
                        stages=self._fetch(provider,stage_key,lambda path=path:client.season_stages(path))
                        if isinstance(stages,int):stages=[stages]
                        for stage in stages:
                            for month in months_between(selected):
                                raw=self._fetch(provider,f'fixtures/{stage}/{month}',
                                    lambda stage=stage,month=month:client.season_events(stage,month))
                                for row in raw:
                                    if row.get('status')==6:events[row['id']]=whoscored_fixture(row)
                    result[provider]=list(events.values())
                if len({e['id'] for e in result[provider]})!=len(result[provider]):raise ValueError('Duplicate fixture IDs')
                self.report(f'{provider}: {len(result[provider])} finished fixtures available.')
            except DATA_ERRORS as exc:
                errors[provider]=str(exc)
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

    def run(self,competition_name,season_name,*,limit=None,build_csvs=True):
        if limit is not None and (isinstance(limit,bool) or not isinstance(limit,int) or limit<1):raise ValueError('limit must be positive')
        self.directory.mkdir(parents=True,exist_ok=True)
        competition=competition_by_name(competition_name);canonical=season_key(season_name)
        sofa=self.clients.get('sofascore')
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
        path=self.directory/'config.json'
        if path.exists() and json.loads(path.read_text())!=config:raise ValueError('Output folder belongs to another season')
        atomic_json(path,config)
        events=sorted((e for e in events if e['status']['type']=='finished'),key=lambda e:(e['startTimestamp'],e['id']))
        if len({e['id'] for e in events})!=len(events):raise ValueError('Duplicate baseline fixture IDs')
        atomic_json(self.directory/'schedule.json',{'fetched_at':now(),'events':events})
        selected=events[:limit] if limit else events
        self.report(f'{competition.name} {canonical}: {len(events)} finished matches; collecting {len(selected)}.')
        fixtures,disabled=self._fixtures(competition,canonical,selected) if selected else ({},{})
        atomic_json(self.directory/'provider_discovery.json',{'errors':disabled,'counts':{k:len(v) for k,v in fixtures.items()}})
        status_path=self.directory/'collection.csv'
        old=pd.read_csv(status_path,keep_default_na=False).to_dict('records') if status_path.exists() else []
        statuses={int(r['match_id']):r for r in old}
        for row in statuses.values():row['competition']=competition.name
        for event in selected:
            statuses.setdefault(event['id'],{'match_id':event['id'],'competition':competition.name,
                'status':'pending','attempts':0,'updated_at':'','error':''})
        loaders={'fotmob':FotMobLoader(identity_resolver=self.identity_resolver),
                 'whoscored':WhoScoredLoader(identity_resolver=self.identity_resolver)}
        for index,event in enumerate(selected,1):
            mid=event['id'];store=PandasStore(self.directory/'match_checkpoints'/str(mid));entry=statuses[mid]
            if store.exists and not self.refresh:
                dataset=store.from_csv();match=dataset.matches[mid]
                # Only a fully complete provider is terminal. Partial and failed
                # enrichments are deliberately revisited on resume so identity
                # aliases, decoder fixes, and previously skipped sources can
                # backfill the checkpoint from cached or newly fetched data.
                if (all(match.metadata.get('provider_status',{}).get(p)=='complete' for p in self.providers)
                        and self._spatial_done(match)):
                    entry.update(status='complete',error='')
                    self.report(f'[{index}/{len(selected)}] Already complete: {mid}')
                    continue
            else:
                try:
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
                except DATA_ERRORS as exc:
                    entry.update(status='failed',error=str(exc),updated_at=now(),attempts=int(entry.get('attempts',0))+1)
                    atomic_csv(pd.DataFrame(statuses.values()),status_path)
                    self.report(f'[{index}/{len(selected)}] {mid}: failed ({exc})')
                    if isinstance(exc,ProviderRequestError) and exc.status_code in (401,403,429):
                        if exc.status_code==403:self.report(f'{self.baseline_provider} request safety stop triggered; collection stopped.')
                        break
                    continue
            errors=[]
            errors.extend(self._collect_spatial(match,event,sofa))
            for provider in self.providers:
                if provider==self.baseline_provider:continue
                if match.metadata['provider_status'].get(provider)=='complete' and not self.refresh:continue
                if provider in disabled:
                    errors.append(f'{provider}: {disabled[provider]}');match.metadata['provider_status'][provider]='failed';continue
                try:
                    fixture=match_fixture(event,fixtures[provider]);pid=fixture['id'];client=self.clients[provider]
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
            store.to_csv(dataset)
            entry.update(status='complete' if not errors else 'partial',error='; '.join(errors),
                         updated_at=now(),attempts=int(entry.get('attempts',0))+1)
            atomic_csv(pd.DataFrame(statuses.values()),status_path)
            self.report(f'[{index}/{len(selected)}] {event["homeTeam"]["name"]} vs {event["awayTeam"]["name"]}: {entry["status"]}'+(f' ({entry["error"]})' if errors else ''))
            if self._sofascore_blocked:
                self.report('SofaScore request safety stop triggered; saved the current checkpoint and stopped collection.')
                break
        atomic_csv(pd.DataFrame(statuses.values(),columns=['match_id','competition','status','attempts','updated_at','error']),status_path)
        if build_csvs:
            self.report(f'[{competition.name} {canonical}] Rebuilding combined exports...')
            self._export(events)
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
            'failed_or_pending':int(result.status.isin(['failed','pending']).sum()),
            'baseline_provider':self.baseline_provider,'providers':list(self.providers),
            'csvs_built':bool(build_csvs)})
        return result


    def _export(self,events):
        """Rebuild player statistics and identity audit tables without network calls."""
        started=time.monotonic();clear_json_cache()
        config=json.loads((self.directory/'config.json').read_text())
        competition=competition_by_name(config['competition'])
        events=with_reviewed_schedule_results(competition,config['season'],config['season_id'],events)
        events=sorted(events,key=lambda event:(event['startTimestamp'],event['id']))
        schedule_path=self.directory/'schedule.json'
        fetched_at=(json.loads(schedule_path.read_text()).get('fetched_at') if schedule_path.exists() else now())
        atomic_json(schedule_path,{'fetched_at':fetched_at,'events':events})
        export_events=[event for event in events if not is_qualification_event(event)]
        self.report(f'[export 1/5] Reading {len(export_events)} match checkpoints...')
        frames=[]
        for event in export_events:
            store=PandasStore(self.directory/'match_checkpoints'/str(event['id']))
            if store.exists:frames.append(store.read_frames())
        self.report('[export 2/5] Combining checkpoints into the season dataset...')
        if frames:
            merged={name:pd.concat([f[name] for f in frames],ignore_index=True).drop_duplicates(KEYS[name],keep='last') for name in frames[0]}
            self.dataset=PandasStore.from_frames(merged)
        else:self.dataset=FootballDataset()
        self._include_awarded_results(export_events)
        events_by_id={event['id']:event for event in export_events}
        self.report('[export 3/5] Refreshing player statistics from cached provider responses...')
        cached_fixtures=self._cached_provider_fixtures()
        for match in self.dataset.matches.values():
            event=events_by_id.get(match.id)
            if event is not None:self._recover_cached_enrichments(match,event,cached_fixtures)
            self._refresh_match_metrics(match)
        self.report('[export 4/5] Writing the relational dataset checkpoint...')
        PandasStore(self.directory/'dataset').to_csv(self.dataset)
        context_columns=['match_id','date','season','competition','team','opponent','player_id','player_name','starter','position','minutes_played']
        rows=[];identities=[];identity_mismatches=[];lineup_differences=[]
        self.report('[export 5/5] Building player_match_stats.csv and identity audits...')
        for match in sorted(self.dataset.matches.values(),key=lambda m:(m.kickoff,m.id)):
            match_context={'match_id':match.id,'date':match.kickoff.date().isoformat(),
                           'season':season_label(match.season.name),'competition':match.season.competition.name}
            for provider,mismatches in match.metadata.get('identity_mismatches',{}).items():
                identity_mismatches.extend({**match_context,**mismatch} for mismatch in mismatches)
            for provider,items in match.metadata.get('lineup_differences',{}).items():
                lineup_differences.extend({**match_context,**item} for item in items)
            for lineup in match.lineups:
                if lineup is None:continue
                for p in lineup.players:
                    context={**match_context,'team':p.team.name,'opponent':p.opponent.name,
                             'player_id':p.player.id,'player_name':p.player.name,
                             'starter':p.starter,'position':p.position,'minutes_played':p.minutes_played}
                    row={**context,**p.stats.raw.get('_combined',{}),'appeared':p.starter or p.did_play}
                    fot=p.stats.raw.get('_providers',{}).get('fotmob',{});profile=fot.get('lineup_profile',{})
                    row.update(fotmob_player_stats_status=fot.get('data_status'),shirt_number=p.shirt_number,
                               fotmob_opta_id=fot.get('opta_id'),fotmob_position_id=profile.get('positionId'),
                               fotmob_age_reported=profile.get('age'),fotmob_country=profile.get('countryName'),
                               fotmob_market_value=profile.get('marketValue'))
                    for name,action in p.stats.actions.items():row[name+'_completion_rate']=action.rate
                    rows.append({**row,**extract_heatmap(match,p,self.directory)})
                    for provider,entry in p.stats.raw.get('_providers',{}).items():
                        identities.append({**context,'provider':provider,'provider_player_id':entry['id'],
                                           'provider_player_name':entry['name'],
                                           'provider_match_id':match.metadata['provider_match_ids'][provider],
                                           'identity_method':entry['identity_method']})
        atomic_csv(pd.DataFrame(rows) if rows else pd.DataFrame(columns=context_columns+['appeared','touch_count','heatmap_xy']),
                   self.directory/'player_match_stats.csv')
        atomic_csv(pd.DataFrame(identities,columns=context_columns+['provider','provider_player_id',
                   'provider_player_name','provider_match_id','identity_method']),self.directory/'identities.csv')
        audit_columns=['match_id','date','season','competition','provider','provider_match_id','side',
                       'provider_player_id','provider_player_name','baseline_player_id','baseline_player_name','issue']
        atomic_csv(pd.DataFrame(identity_mismatches,columns=audit_columns),self.directory/'identity_mismatches.csv')
        atomic_csv(pd.DataFrame(lineup_differences,columns=audit_columns),self.directory/'lineup_differences.csv')
        status_path=self.directory/'collection.csv'
        if status_path.exists():
            status=pd.read_csv(status_path,keep_default_na=False);status['competition']=config['competition']
            atomic_csv(status[['match_id','competition']+[c for c in status.columns if c not in ('match_id','competition')]],status_path)
        # Previous outputs remain on disk; rebuilding no longer reads or creates them.
        clear_json_cache()
        self.report(f'[export complete] Wrote player statistics and identity audits in {time.monotonic()-started:.1f}s.')

    def _include_awarded_results(self,events):
        """Add schedule-only awarded fixtures to result exports without inventing detail data."""
        awarded=[event for event in events if _is_awarded(event)]
        if not awarded:return
        config=json.loads((self.directory/'config.json').read_text())
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
        return result

    def _recover_cached_enrichments(self,match,event,cached_fixtures):
        """Recover good player rows from provider games that once failed as a unit."""
        if all(lineup is None for lineup in match.lineups):return
        loaders={'fotmob':FotMobLoader(identity_resolver=self.identity_resolver),
                 'whoscored':WhoScoredLoader(identity_resolver=self.identity_resolver)}
        for provider,loader in loaders.items():
            try:
                fixture=match_fixture(event,cached_fixtures.get(provider,[]))
                relative=match.metadata.get('raw_response_files',{}).get(provider)
                path=(self.directory/relative if relative else
                      self.cache.path(provider,f'match/{fixture["id"]}'))
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
