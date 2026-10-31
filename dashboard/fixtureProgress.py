"""Fixture status stays separate from match-stat timing and coverage."""
from datetime import datetime,timezone
from fixtures.progress import ACTIVE


def view(value,*,receipt=None,clock=None):
    result=dict(value);current=clock or datetime.now(timezone.utc)
    stamps=[datetime.fromisoformat(value['heartbeat_at'])]
    if value.get('received_at'):stamps.append(datetime.fromisoformat(value['received_at']))
    # A delayed offline report must not revive a fixture process that exited.
    age=max(0,*((current-stamp).total_seconds() for stamp in stamps))
    result['connection']='connected' if age<=45 else 'disconnected'
    result['active']=value.get('state') in ACTIVE and age<=45
    result['heartbeat_age_seconds']=age
    if (receipt and receipt.get('worker')==value.get('worker_id')
            and receipt.get('id')==value.get('delivery',{}).get('id')):
        result['delivery']={**value['delivery'],'state':'installed_on_main','received_at':receipt.get('received_at')}
    return result
