"""Completion follows saved provider data, independently of the next transport."""


def required_detail_errors(details, *, require_momentum=False):
    return [error for error in details.get('errors',[])
            if require_momentum or not error.startswith('momentum:')]


def sofascore_stats_complete(match, *, require_momentum=False):
    if match.metadata.get('provider_status', {}).get('sofascore') != 'complete':
        return False
    # Old API checkpoints have no detail bundle. Explicit failures still retry,
    # except for an optional graph with independently verified required data.
    details=match.metadata.get('browser_details')
    if details is None or details.get('complete') is True:return True
    if require_momentum or required_detail_errors(details):return False
    coverage=details.get('coverage',{})
    required=[state for key,state in coverage.items() if key!='momentum']
    return (coverage.get('statistics')=='complete' and coverage.get('incidents')=='complete'
            and bool(required) and all(state=='complete' for state in required))
