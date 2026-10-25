"""Reviewed club profile groups, scoped to provider IDs rather than names.

The first ID is preferred for new references. Existing native IDs are retained.
"""
import hashlib
import json
from pathlib import Path

GROUPS=json.loads((Path(__file__).parent/'concordance/team_profiles.json').read_text())['groups']


def canonical_team_id(provider,native):
    native=str(native)
    for group in GROUPS:
        ids=group.get(provider,[])
        if native in ids:return ids[0]
    return native


def reviewed_team(provider,native):
    return any(str(native) in group.get(provider,[]) for group in GROUPS)


def same_reviewed_team(left_provider,left,right_provider,right):
    return any(str(left) in group.get(left_provider,[]) and str(right) in group.get(right_provider,[]) for group in GROUPS)


def version():
    return hashlib.sha256(json.dumps(GROUPS,sort_keys=True).encode()).hexdigest()
