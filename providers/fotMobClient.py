"""FotMob's public JSON endpoints. Provider IDs are not Sofascore IDs."""
from copy import deepcopy

from .httpClient import HttpClient, ProviderError


def without_incident_stream(payload):
    """Drop FotMob's bundled incident timeline while retaining match data."""
    cleaned = deepcopy(payload)
    match_facts = cleaned.get('content', {}).get('matchFacts')
    if isinstance(match_facts, dict):
        match_facts.pop('events', None)
    return cleaned


class FotMobClient(HttpClient):
    BASE_URL = 'https://www.fotmob.com/api/data'
    REFERER = 'https://www.fotmob.com/'

    def league(self, competition_id, season=None):
        params = {'id':competition_id, 'ccode3':'USA'}
        if season is not None:
            params['season'] = season
        payload = self.get('leagues', params)
        if 'allAvailableSeasons' not in payload:
            raise ProviderError('FotMob league response has no season directory')
        return payload

    def seasons(self, competition_id):
        return self.league(competition_id)['allAvailableSeasons']

    def season_events(self, competition_id, season):
        payload = self.league(competition_id, season)
        if season not in payload['allAvailableSeasons'] or payload.get('overview', {}).get('selectedSeason') != season:
            raise ProviderError(f'FotMob did not select season {season}')
        matches = payload.get('fixtures', {}).get('allMatches')
        if not isinstance(matches, list):
            raise ProviderError('FotMob season response has no complete fixture list')
        return matches

    def match_details(self, match_id):
        payload = self.get('matchDetails', {'matchId':match_id})
        if str(payload.get('general', {}).get('matchId')) != str(match_id):
            raise ProviderError('FotMob returned a different match')
        return payload

    def event(self, match_id):
        return self.match_details(match_id)

    def scheduled_events(self, date):
        return self.get('matches', {'date':date.replace('-', '')})
