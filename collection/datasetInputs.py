"""Saved inputs eligible for an explicit season export; absence is not zero coverage."""
from pathlib import Path
from .paths import collection_directory,collection_name,discover_collections

REQUIRED=('job.json','config.json','schedule.json')


def selected_inputs(root,names=None):
    root=Path(root)
    selected=list(dict.fromkeys(names if names else [collection_name(p) for p in discover_collections(root,'job.json')]))
    ready=[];deferred=[]
    for name in selected:
        source=collection_directory(root,name)
        if not (source/'job.json').is_file():raise ValueError('Unknown saved season: '+name)
        missing=[key for key in REQUIRED if not (source/key).is_file()]
        if missing:deferred.append({'collection':name,'season_path':source.relative_to(root).as_posix(),'missing_inputs':missing,'state':'waiting_inputs'})
        else:ready.append(name)
    return ready,deferred
