"""Shared standalone loading and transactional enrichment of a baseline match."""
from copy import deepcopy
from .matchLoader import MatchLoader
from .identity import IdentityResolver, IdentityError, name_key, team_key
from .statistics import synthesize


class ProviderLoader(MatchLoader):
    provider = None

    def __init__(self, client=None, dataset=None, identity_resolver=None):
        super().__init__(client,dataset)
        self.identity_resolver=identity_resolver or IdentityResolver()

    def decode(self, payload, event=None):
        raise NotImplementedError

    def load_match(self, payload, season, event=None):
        decoded=self.decode(payload,event)
        for side in ('home','away'):
            for entry in decoded[side]['players']:
                synthesize({}, {self.provider:entry},entry['appeared'],True)
        ev=decoded['event'];ev['season']={'id':season.id}
        lineups={'confirmed':True}
        for side in ('home','away'):
            lineups[side]={'formation':decoded[side].get('formation'),'players':[]}
            for p in decoded[side]['players']:
                lineups[side]['players'].append({'player':{'id':p['id'],'name':p['name'],'position':p.get('position')},
                    'position':p.get('position'),'shirtNumber':p.get('shirt_number'),'substitute':not p['starter'],
                    'statistics':{'minutesPlayed':p['metrics'].get('minutes_played')}})
        if ev['id'] in self.dataset.matches: return self.dataset.matches[ev['id']]
        match=self._build_match(season,ev,lineups)
        self.enrich(match,payload,event,decoded=decoded,standalone=True)
        match.metadata['id_namespace']=self.provider
        return match

    def iter_matches(self, season, events):
        for event in sorted(events,key=lambda e:e['startTimestamp']):
            yield self.load_match(self.client.match_details(event['id']),season,event)

    def enrich(self, match, payload, event=None, *, decoded=None, standalone=False,
               allow_partial=False):
        if not standalone:
            namespace=match.metadata.get('id_namespace','sofascore')
            if namespace not in ('sofascore','fotmob','whoscored'):
                raise IdentityError(f'Unknown baseline ID namespace: {namespace}')
            if namespace==self.provider:
                raise IdentityError(f'{self.provider}: baseline data cannot be enriched twice')
        decoded=decoded or self.decode(payload,event)
        ev=decoded['event']; pending=[];mismatches=[];lineup_differences=[]
        if ev['startTimestamp']//86400!=int(match.kickoff.timestamp())//86400:
            raise IdentityError(f'{self.provider}: match date mismatch')
        if not standalone and match.metadata.get('id_namespace','sofascore')=='sofascore':
            from .identityReview import suggest_teams
            baseline={'id':match.id,'startTimestamp':int(match.kickoff.timestamp()),
                      'homeTeam':{'name':match.home_team.name},'awayTeam':{'name':match.away_team.name}}
            if any(team_key(baseline[s+'Team']['name'],self.identity_resolver.review_root)!=
                   team_key(ev[s+'Team']['name'],self.identity_resolver.review_root) for s in ('home','away')):
                suggest_teams(self.identity_resolver.review_root,self.provider,baseline,[ev])
        for side in ('home','away'):
            lineup=getattr(match,side+'_lineup')
            if lineup is None or team_key(lineup.team.name,self.identity_resolver.review_root)!=team_key(ev[side+'Team']['name'],self.identity_resolver.review_root):
                raise IdentityError(f'{self.provider}: match team mismatch')
            score=getattr(match,side+'_score')
            if ev.get(side+'Score',{}).get('current') != score:
                raise IdentityError(f'{self.provider}: match score mismatch')
            used=set()
            for entry in decoded[side]['players']:
                appearance=None
                try:
                    if standalone:
                        appearance=next(p for p in lineup.players if p.player.id==entry['id']);method='provider_native_id'
                    else:
                        try:appearance,method=self.identity_resolver.player(self.provider,entry,lineup.players,provider_match_id=ev['id'])
                        except IdentityError:
                            verifier=self.identity_resolver.investigator
                            if verifier and match.metadata.get('id_namespace','sofascore')=='sofascore' and entry.get('appeared',False):
                                from .identityReview import suggest_players
                                proposals=suggest_players(self.identity_resolver.review_root,self.provider,entry,
                                    [p for p in lineup.players if p.player.id not in used],
                                    {'match_id':str(match.id),'provider_match_id':str(ev['id']),'side':side,
                                     'competition':match.season.competition.name,'season':match.season.name,
                                     'date':match.kickoff.date().isoformat(),'match':match.home_team.name+' vs '+match.away_team.name})
                                verifier.investigate(proposals)
                                appearance,method=self.identity_resolver.player(self.provider,entry,lineup.players,provider_match_id=ev['id'])
                            else:raise
                    if appearance.player.id in used: raise IdentityError('Duplicate player match')
                    if appearance.starter != entry['starter']:
                        raise IdentityError(f'{self.provider}: starter mismatch for {entry["name"]}')
                except (IdentityError,StopIteration) as exc:
                    if not allow_partial:
                        if isinstance(exc,StopIteration):
                            raise IdentityError(f'{self.provider}: native player ID {entry["id"]} is absent') from exc
                        raise
                    likely=[p for p in lineup.players
                            if name_key(p.player.name)==name_key(entry['name'])]
                    candidate=likely[0] if len(likely)==1 else appearance
                    record={'provider':self.provider,'provider_match_id':ev['id'],
                        'side':side,'provider_player_id':entry['id'],
                        'provider_player_name':entry['name'],
                        'baseline_player_id':candidate.player.id if candidate else None,
                        'baseline_player_name':candidate.player.name if candidate else None,
                        'issue':str(exc) or type(exc).__name__}
                    # Providers frequently publish different unused benches. No
                    # player statistics are lost, so retain the discrepancy for
                    # auditing without making the enrichment permanently partial.
                    if not entry.get('appeared',False):
                        record['issue']=f'{self.provider}: unused provider substitute differs from baseline lineup'
                        lineup_differences.append(record)
                        continue
                    if 'cannot uniquely match' in str(exc) and match.metadata.get('id_namespace','sofascore')=='sofascore':
                        from .identityReview import suggest_players
                        suggest_players(self.identity_resolver.review_root,self.provider,entry,
                            [p for p in lineup.players if p.player.id not in used],
                            {'match_id':str(match.id),'provider_match_id':str(ev['id']),'side':side,
                             'competition':match.season.competition.name,'season':match.season.name,
                             'date':match.kickoff.date().isoformat(),
                             'match':match.home_team.name+' vs '+match.away_team.name,'issue':str(exc)})
                    mismatches.append(record)
                    continue
                used.add(appearance.player.id)
                providers=deepcopy(appearance.stats.raw.get('_providers',{}))
                providers[self.provider]={**entry,'identity_method':method}
                combined=synthesize(appearance.stats.raw,providers,appearance.starter or appearance.did_play or entry.get('appeared',False),standalone)
                pending.append((appearance,providers,combined))
            missing=[p for p in lineup.players if p.player.id not in used]
            if missing and not allow_partial:
                raise IdentityError(f'{self.provider}: incomplete squad matching')
            represented={m.get('baseline_player_id') for m in mismatches+lineup_differences if m['side']==side}
            for appearance in missing:
                if appearance.player.id in represented:continue
                record={'provider':self.provider,'provider_match_id':ev['id'],
                    'side':side,'provider_player_id':None,'provider_player_name':None,
                    'baseline_player_id':appearance.player.id,
                    'baseline_player_name':appearance.player.name,
                    'issue':f'{self.provider}: baseline player is absent from provider lineup'}
                if not appearance.starter and not appearance.did_play:
                    record['issue']=f'{self.provider}: unused baseline substitute differs from provider lineup'
                    lineup_differences.append(record)
                else:
                    mismatches.append(record)
        # Commit only after every identity and every action pair validates.
        for appearance,providers,combined in pending:
            appearance.stats.raw['_providers']=providers
            appearance.stats.raw['_combined']=combined['metrics']
            appearance.stats.raw['_combined_sources']=combined['sources']
            appearance.stats.raw['_source_differences']=combined['differences']
            appearance.stats.actions=combined['actions']
            if appearance.minutes_played is None:
                appearance.minutes_played=combined['metrics'].get('minutes_played')
        match.metadata.setdefault('provider_match_ids',{})[self.provider]=ev['id']
        match.metadata.setdefault('identity_mismatches',{})[self.provider]=mismatches
        match.metadata.setdefault('lineup_differences',{})[self.provider]=lineup_differences
        return match
