"""Explicit raw-collection removals; fixture history and identity keys are separate."""
from datetime import datetime,timezone
import json
from pathlib import Path
from uuid import uuid4

from storage.pandasStore import atomic_json

POLICY=Path(__file__).resolve().parents[1]/'config/removed_raw_collections.json'


def removed_seasons():
    value=json.loads(POLICY.read_text(encoding='utf-8'))
    if value.get('format')!='soccer-removed-raw-collections-v1':raise ValueError('Invalid removed collection policy')
    seasons=value['seasons']
    from .paths import collection_name
    for season,reason in seasons.items():
        parts=Path(season).parts
        if len(parts)!=2 or any(part in ('.','..') for part in parts) or not reason:raise ValueError('Invalid removed raw season')
        collection_name(Path(season))
    return seasons


def raw_season(relative):
    parts=Path(relative.replace('\\','/')).parts
    if parts and parts[0] in ('whoscored','whoscored_events','fotmob_player_stats'):parts=parts[1:]
    return '/'.join(parts[:2]) if len(parts)>=2 else ''


def apply_removals(root,*,dataset=None,archive=None,stop=None,report=print):
    """Remove requested collections from active use, retaining local recovery copies.

    Runs on the data's owning computer. Never moves raw bytes over the network,
    edits source-provider IDs, or removes the independent fixture checkpoints.
    """
    from .maintenance import background
    root=Path(root);removed=[]
    for season,reason in removed_seasons().items():
        with background(root,season,stop) as check:
            check();moves=[]
            candidates=[(root/season,'combined')]+[(root/layout/season,layout) for layout in ('whoscored','whoscored_events','fotmob_player_stats')]
            if dataset is not None:candidates.append((Path(dataset)/season,'compiled'))
            for source,layout in candidates:
                if source.is_symlink():raise ValueError('Removed collection must not be a symbolic link')
                if not source.exists():continue
                target=root/'_system/removed_raw_collections'/season/layout/uuid4().hex
                target.parent.mkdir(parents=True,exist_ok=True)
                source.replace(target)
                moves.append({'source':str(source),'recovery':str(target)})
            if archive is not None:archive.remove_season(season)
            if moves:
                receipt=root/'_system/removed_raw_collections'/season/'removal.json'
                previous=json.loads(receipt.read_text()) if receipt.exists() else {}
                atomic_json(receipt,{'season':season,'reason':reason,'removed_at':datetime.now(timezone.utc).isoformat(),
                    'moves':[*previous.get('moves',[]),*moves]})
                removed.append(season)
                report('[Storage] Removed active raw collection '+season+'; recovery copy retained locally.')
    return removed
