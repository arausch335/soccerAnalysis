"""Single-match access checks using each provider's existing safe client."""
from .httpClient import ProviderError, ProviderRequestError
from .sofaScoreProbe import probe_match as probe_sofascore


LABELS = {'sofascore':'Sofascore','whoscored':'WhoScored','fotmob':'FotMob'}


def probe_provider(client, provider, match_id):
    if provider not in LABELS:
        raise ValueError(f'Unknown provider: {provider}')
    if type(match_id) is not int or match_id < 1:
        raise ValueError('match ID must be a positive integer')
    if provider == 'sofascore':
        return probe_sofascore(client,match_id)
    label = LABELS[provider]
    refresh = client.refresh_cache
    client.refresh_cache = True
    try:
        try:
            payload = client.match_details(match_id)
            if provider == 'fotmob':
                valid = str(payload.get('general',{}).get('matchId')) == str(match_id)
                teams = payload.get('header',{}).get('teams',[])
                names = [team.get('name') for team in teams]
            else:
                valid = payload.get('match_id') == match_id
                centre = payload.get('matchCentreData',{})
                valid = valid and isinstance(centre.get('events'),list) and bool(centre['events'])
                names = [centre.get(side,{}).get('name') for side in ('home','away')]
            if not valid or len(names) != 2 or not all(isinstance(name,str) and name for name in names):
                return False,f'NOT GOOD: {label} response is missing the requested match {match_id} or its data.'
        except ProviderRequestError as exc:
            if exc.status_code in (401,403,429) and exc.attempts == 0:
                return False,f'NOT TESTED: {label} cooldown is active; no request sent. {exc}'
            return False,f'NOT GOOD: {label} match {match_id} failed: {exc}'
        except (ProviderError,ValueError,TypeError,KeyError,AttributeError) as exc:
            return False,f'NOT GOOD: invalid {label} response for match {match_id}: {exc}'
        return True,(f'GOOD: {label} match {match_id} is accessible: {names[0]} v {names[1]}. '
                     'One match-details request succeeded; other endpoints were not tested.')
    finally:
        client.refresh_cache = refresh
