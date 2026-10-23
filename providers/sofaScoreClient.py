"""Sofascore endpoints over the shared sequential HTTP transport."""
from typing import Iterator
# Re-exported for compatibility with callers and existing transport tests.
from .httpClient import HttpClient, ProviderError, ProviderRequestError, time, requests
SofaScoreError = ProviderError
SofaScoreRequestError = ProviderRequestError

class SofaScoreClient(HttpClient):
    BASE_URL = "https://api.sofascore.com/api/v1"
    REFERER = "https://www.sofascore.com/"

    def event(self, event_id: int) -> dict:
        """Match details, teams, score, kickoff and status."""
        return self._get(f"event/{event_id}")

    def lineups(self, event_id: int) -> dict:
        """Starting XI, substitutes, formation and available player statistics."""
        return self._get(f"event/{event_id}/lineups")

    def statistics(self, event_id: int) -> dict:
        """Team-level match statistics."""
        return self._get(f"event/{event_id}/statistics")

    def incidents(self, event_id: int) -> dict:
        return self._get(f"event/{event_id}/incidents")

    def graph(self, event_id: int) -> dict:
        return self._get(f"event/{event_id}/graph")

    def player_event_statistics(self, event_id: int, player_id: int) -> dict:
        return self._get(f"event/{event_id}/player/{player_id}/statistics")

    def player_heatmap(self, event_id: int, player_id: int) -> dict:
        return self._get(f"event/{event_id}/player/{player_id}/heatmap")

    def player_rating_breakdown(self, event_id: int, player_id: int) -> dict:
        """Player action maps (passes, carries, dribbles, defending), when available."""
        return self._get(f"event/{event_id}/player/{player_id}/rating-breakdown")

    def average_positions(self, event_id: int) -> dict:
        """Touch-derived average player positions for a match, when available."""
        return self._get(f"event/{event_id}/average-positions")

    def shotmap(self, event_id: int) -> dict:
        """All match shots with player coordinates, when available."""
        return self._get(f"event/{event_id}/shotmap")

    def player(self, player_id: int) -> dict:
        return self._get(f"player/{player_id}")

    def player_profile(self, player_id):
        from .playerProfiles import sofa_profile
        return sofa_profile(player_id,self.player(player_id),self._get(f'player/{int(player_id)}/transfer-history'))

    def team(self, team_id: int) -> dict:
        return self._get(f"team/{team_id}")

    def competition(self, competition_id: int) -> dict:
        """Competition information; use Sofascore's unique tournament ID."""
        return self._get(f"unique-tournament/{competition_id}")

    def seasons(self, competition_id: int) -> dict:
        return self._get(f"unique-tournament/{competition_id}/seasons")

    def scheduled_events(self, date: str, sport: str = "football") -> dict:
        """Matches for a YYYY-MM-DD date."""
        return self._get(f"sport/{sport}/scheduled-events/{date}")

    def season_events(self, competition_id: int, season_id: int,
                      page: int = 0, direction: str = "last") -> dict:
        """One page of past (last) or upcoming (next) season events."""
        if direction not in ("last", "next"):
            raise ValueError("direction must be 'last' or 'next'")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise ValueError("page must be a nonnegative integer")
        return self._get(
            f"unique-tournament/{competition_id}/season/{season_id}/events/{direction}/{page}"
        )

    def iter_season_events(self, competition_id: int, season_id: int, *,
                           include_upcoming: bool = False) -> Iterator[dict]:
        """Yield unique events lazily in provider page order, not kickoff order.

        SofaScoreLoader sorts these before loading match details sequentially.
        Stop on hasNextPage=False. After a completed past schedule, a 404 on
        next/0 means no upcoming fixtures. Other errors and malformed/repeated
        pages raise instead of treating an incomplete schedule as complete.
        """
        seen = set()
        directions = ("last", "next") if include_upcoming else ("last",)
        for direction in directions:
            direction_seen = set()
            page = 0
            while True:
                try:
                    payload = self.season_events(competition_id, season_id, page, direction)
                except SofaScoreRequestError as exc:
                    # The past direction has already completed before reaching
                    # next/0. SofaScore returns 404 here for historical seasons
                    # without upcoming fixtures, rather than an empty page.
                    if direction == 'next' and page == 0 and exc.status_code == 404:
                        break
                    raise
                events = payload.get("events")
                has_next = payload.get("hasNextPage")
                if (not isinstance(events, list) or not isinstance(has_next, bool)
                        or any(not isinstance(e, dict) or not isinstance(e.get("id"), int)
                               for e in events)):
                    raise SofaScoreError(f"Invalid season events response on {direction} page {page}")
                new_ids = {e['id'] for e in events} - direction_seen
                if has_next and not new_ids:
                    raise SofaScoreError(f"Pagination made no progress on {direction} page {page}")
                direction_seen.update(new_ids)
                for event in events:
                    if event['id'] not in seen:
                        seen.add(event['id'])
                        yield event
                if not has_next:
                    break
                page += 1

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """Request any API-relative endpoint, preserving the raw JSON object."""
        return self._get(endpoint, params=params)
