"""Reuse legacy qualifying checkpoints without moving a running collector's data."""
from pathlib import Path
import re
from storage.pandasStore import PandasStore
from .storageTransfer import link_or_copy,digest
from .workerIdentity import read_origins,save_origins
from .ensembleProgress import job_key


def reuse_checkpoint(root,competition,season,mid):
    if not competition.parent:return False
    root=Path(root)
    slug=lambda name:re.sub('[^a-z0-9]+','-',name.lower()).strip('-')
    source=root/slug(competition.parent)/season;target=root/slug(competition.name)/season
    old=PandasStore(source/'match_checkpoints'/str(mid));new=PandasStore(target/'match_checkpoints'/str(mid))
    if new.exists or not old.exists:return False
    dataset=old.from_csv();match=dataset.matches[mid]
    def references(value):
        if isinstance(value,dict):
            for v in value.values():yield from references(v)
        elif isinstance(value,list):
            for v in value:yield from references(v)
        elif isinstance(value,str) and value.replace('\\','/').startswith('raw/'):yield value.replace('\\','/')
    for relative in set(references({key:match.metadata.get(key,{}) for key in ('raw_response_files','sofascore_spatial_files')})):
        path=Path(relative)
        if path.is_absolute() or '..' in path.parts:raise ValueError('Invalid saved raw reference')
        origin=source/path;destination=target/path
        if not origin.is_file():raise ValueError('Legacy qualifier raw response is missing: '+relative)
        if destination.exists():
            if digest(origin)!=digest(destination):raise ValueError('Qualifier raw response conflict: '+relative)
        else:link_or_copy(origin,destination)
    match.season.competition.name=competition.name
    evidence=match.metadata.setdefault('collection_evidence',{});evidence['qualifying']=True
    attempts=source/'collection.csv'
    if attempts.exists():
        import csv
        with attempts.open(encoding='utf-8',newline='') as stream:
            prior=next((r for r in csv.DictReader(stream) if str(r.get('match_id'))==str(mid)),{})
        archived=source/'qualifier-attempts.json'
        if not prior and archived.exists():
            import json
            prior=json.loads(archived.read_text(encoding='utf-8')).get(str(mid),{})
        evidence['attempts']=max(int(evidence.get('attempts',0)),int(prior.get('attempts') or 0))
        if prior.get('error'):evidence.setdefault('error',prior['error'])
    new.to_csv(dataset)
    origins=read_origins(root)
    for namespace,pid in [('sofascore',mid),('whoscored_events',match.metadata.get('provider_match_ids',{}).get('whoscored'))]:
        if pid is not None:
            prior=origins.get(job_key(namespace,competition.parent,season,pid))
            if prior:origins[job_key(namespace,competition.name,season,pid)]=prior
    save_origins(root,origins)
    from .ensembleProgress import ACTIVE
    progress=ACTIVE.get()
    if progress is not None:progress.origins.update(origins)
    return True
