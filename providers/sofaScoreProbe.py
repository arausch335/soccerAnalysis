"""Check one match through the normal transport with at most one HTTP attempt."""
from .httpClient import ProviderError, ProviderRequestError


# Manchester United v Fulham, Premier League, 16 August 2024.
DEFAULT_MATCH_ID = 12436870


def probe_match(client, match_id):
    if isinstance(match_id,bool) or not isinstance(match_id,int) or match_id < 1:
        raise ValueError('match ID must be a positive integer')
    # A cached success is not evidence that Sofascore is accessible now.
    refresh = client.refresh_cache
    client.refresh_cache = True
    try:
        try:
            payload = client.event(match_id)
        except ProviderRequestError as exc:
            if exc.status_code == 429 and exc.attempts == 0:
                message = f'NOT TESTED: Sofascore cooldown is active; no request sent. {exc}'
            elif exc.status_code in (401,403,429):
                message = f'NOT GOOD: Sofascore refused match {match_id}: {exc}'
            else:
                message = f'NOT GOOD: could not retrieve Sofascore match {match_id}: {exc}'
            return False,message
        except ProviderError as exc:
            return False,f'NOT GOOD: invalid Sofascore response: {exc}'
        event = payload.get('event') if isinstance(payload,dict) else None
        if (not isinstance(event,dict) or event.get('id') != match_id
                or not isinstance(event.get('homeTeam'),dict)
                or not isinstance(event.get('awayTeam'),dict)
                or not event['homeTeam'].get('name') or not event['awayTeam'].get('name')):
            return False,f'NOT GOOD: response did not contain the requested match {match_id} and its teams.'
        return True,(f"GOOD: Sofascore match {match_id} is accessible: "
                     f"{event['homeTeam']['name']} v {event['awayTeam']['name']}. "
                     'One match-details request succeeded; other endpoints were not tested.')
    finally:
        client.refresh_cache = refresh
