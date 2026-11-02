"""Add only Main's missing Fast-mode resources, without claiming remote data local."""
from copy import deepcopy

from providers import SofaScoreLoader, FotMobLoader, WhoScoredLoader
from providers.identity import match_fixture
from providers.matchComparison import provider_error
from .whoScoredData import integrated_error

RESOURCES=frozenset(('sofascore_stats','match_statistics','incidents','momentum',
                     'fotmob_enrichment','whoscored_stats','whoscored_events'))


def collect(runner,event,season,competition,store,wanted,fixtures,disabled):
    """Return an additive checkpoint. Inventory evidence stays with its actual holder."""
    from .seasonRunner import DATA_ERRORS
    mid=event['id'];wanted=set(wanted);sofa=runner.clients['sofascore'];errors=[]
    if store.exists and not runner.refresh:
        dataset=store.from_csv();match=dataset.matches[mid]
    else:
        loader=SofaScoreLoader(sofa)
        selected=loader.select_season(competition.sofascore_id,competition.name,season['id'],season['name'])
        match=loader._build_match(selected,event,{})
        dataset=loader.dataset
    metadata=match.metadata;statuses=metadata.setdefault('provider_status',{})
    metadata.setdefault('provider_match_ids',{})['sofascore']=mid
    metadata['cleanup_resources']=sorted(wanted)
    details=metadata.setdefault('browser_details',{'complete':False,'coverage':{},'errors':[]})
    coverage=details.setdefault('coverage',{})
    files=metadata.setdefault('raw_response_files',{})

    # Lineups are supporting identity data only when new player/provider data is needed.
    player_work=('sofascore_stats' in wanted and statuses.get('sofascore')!='complete' or
        'fotmob_enrichment' in wanted and statuses.get('fotmob')!='complete' or
        bool(wanted&{'whoscored_stats','whoscored_events'}) and statuses.get('whoscored')!='complete')
    if player_work and any(lineup is None for lineup in match.lineups):
        payload=runner._fetch('sofascore',f'lineups/{mid}',lambda:sofa.lineups(mid))
        loader=SofaScoreLoader(sofa)
        selected=loader.select_season(competition.sofascore_id,competition.name,season['id'],season['name'])
        rebuilt=loader._build_match(selected,event,payload)
        rebuilt.metadata.update(deepcopy(metadata));match=rebuilt;dataset=loader.dataset;metadata=match.metadata
        statuses=metadata['provider_status'];details=metadata['browser_details'];coverage=details['coverage'];files=metadata['raw_response_files']
    if 'sofascore_stats' in wanted and statuses.get('sofascore')!='complete':
        if getattr(sofa,'captures_details',False):
            errors.extend(runner._browser_match_details(sofa,event,match,resources={'sofascore_stats'}))
            details=metadata['browser_details'];coverage=details.setdefault('coverage',{})
        statuses['sofascore']='complete' if not errors else 'partial'

    # These endpoints don't require player profiles or any other provider's payload.
    for resource,label,method,field in (
            ('match_statistics','statistics','statistics','statistics'),
            ('incidents','incidents','incidents','incidents'),
            ('momentum','momentum','graph','graphPoints')):
        if resource not in wanted or coverage.get(label)=='complete':continue
        runner.timing.set_provider('sofascore')
        try:
            payload=runner._fetch('sofascore',f'{label}/{mid}',lambda:getattr(sofa,method)(mid))
            if not isinstance(payload,dict) or not isinstance(payload.get(field),list):
                raise ValueError(f'{label}: invalid response')
            coverage[label]='complete'
            files['sofascore_'+label]=str(runner.cache.path('sofascore',f'{label}/{mid}').relative_to(runner.directory))
        except DATA_ERRORS as exc:
            coverage[label]='unavailable' if getattr(exc,'status_code',None)==404 else 'failed'
            errors.append(provider_error('sofascore',exc))
            if getattr(exc,'status_code',None) in (401,403,429) or getattr(sofa,'requires_manual_attention',False) is True:
                runner._sofascore_blocked=True;break
    details['errors']=errors.copy()
    # A match-only supplement must never certify absent player statistics.
    details['complete']=statuses.get('sofascore')=='complete' and all(
        coverage.get(k)=='complete' for k in ('statistics','incidents','momentum')) and not errors

    for provider,resource,loader_type in (('fotmob','fotmob_enrichment',FotMobLoader),
                                         ('whoscored','whoscored_stats',WhoScoredLoader)):
        need_events=provider=='whoscored' and 'whoscored_events' in wanted
        if resource not in wanted and not need_events:continue
        if statuses.get(provider)=='complete' and not need_events:continue
        if provider in disabled:errors.append(provider_error(provider,disabled[provider]));continue
        runner.timing.set_provider(provider)
        try:
            fixture=match_fixture(event,fixtures[provider],provider=provider,review_root=runner.identity_resolver.review_root)
            pid=fixture['id'];client=runner.clients[provider]
            if provider=='whoscored' and not runner.refresh and not runner.cache.path(provider,f'match/{pid}').exists():
                from .whoScoredData import reuse_match
                reuse_match(runner.directory,pid,fixture)
            payload=(runner._fetch_fotmob_match(pid,client) if provider=='fotmob' else
                     runner._fetch(provider,f'match/{pid}',lambda:client.match_details(pid)))
            loader_type(identity_resolver=runner.identity_resolver).enrich(match,payload,fixture,allow_partial=True)
            mismatches=metadata.get('identity_mismatches',{}).get(provider,[])
            statuses[provider]='partial' if mismatches else 'complete'
            if resource in wanted and mismatches:errors.append(f'{provider}: {len(mismatches)} player identity mismatch(es)')
            files[provider]=str(runner.cache.path(provider,f'match/{pid}').relative_to(runner.directory))
            if need_events:
                reason=integrated_error(match,payload,root=runner.identity_resolver.review_root)
                if reason:errors.append('whoscored events: '+reason)
        except DATA_ERRORS as exc:
            errors.append(provider_error(provider,exc));statuses[provider]='failed'
            if getattr(exc,'fixture_comparison',None):metadata.setdefault('provider_comparisons',{})[provider]=exc.fixture_comparison
            if getattr(exc,'status_code',None) in (401,403,429):disabled[provider]=str(exc)
    return dataset,match,errors
