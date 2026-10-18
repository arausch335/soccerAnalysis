"""Completion follows saved provider data, independently of the next transport."""


def sofascore_stats_complete(match):
    if match.metadata.get('provider_status', {}).get('sofascore') != 'complete':
        return False
    # Older HTTP checkpoints stored full player statistics in lineups and never
    # had browser coverage. An explicit failed browser attempt is different.
    details = match.metadata.get('browser_details')
    return details is None or details.get('complete') is True
