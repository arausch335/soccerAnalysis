"""Portable evidence of completed work; this never represents local raw data."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from storage.pandasStore import atomic_json, PandasStore
from .selection import season_label, competition_by_name

FORMAT = 'soccer-analysis-progress-v1'
ACTIVE = ContextVar('ensemble_progress', default=None)


def job_key(namespace, competition, season, mid):
    slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
    return f'{namespace}|{slug}|{season_label(season)}|{mid}'


class PortableProgress:
    def __init__(self, path, root):
        self.path=Path(path) if path is not None else None;self.root=Path(root);self.scanning=False;self.skip_remote=True
        self.records={}
        if self.path is not None and self.path.exists():
            saved=json.loads(self.path.read_text(encoding='utf-8'))
            if saved.get('format')!=FORMAT or not isinstance(saved.get('records'),dict):
                raise ValueError('Unsupported ensemble provenance file; leave it in place and inspect it')
            if any(not isinstance(row,dict) or not isinstance(row.get('capabilities',[]),list)
                   or not isinstance(row.get('origins',[]),list) for row in saved['records'].values()):
                raise ValueError('Invalid portable provenance records')
            self.records=saved['records']
        if self.path is None:self.origin='validation';return
        identity=self.root/'.ensemble-computer.json'
        if identity.exists():self.origin=json.loads(identity.read_text(encoding='utf-8'))['id']
        else:
            self.origin=uuid.uuid4().hex
            atomic_json(identity,{'id':self.origin})

    def done(self, namespace, competition, season, mid, required=()):
        row=self.records.get(job_key(namespace,competition,season,mid),{})
        return (row.get('status')=='complete' and
                set(required)<=set(row.get('capabilities',[])))

    def record(self, namespace, competition, season, mid, capabilities=()):
        key=job_key(namespace,competition,season,mid)
        old=self.records.get(key,{})
        self.records[key]={'namespace':namespace,'competition':competition,'season':season_label(season),
            'match_id':str(mid),'status':'complete',
            'capabilities':sorted(set(old.get('capabilities',[]))|set(capabilities)),
            'origins':sorted(set(old.get('origins',[]))|{self.origin}),
            'updated_at':datetime.now(timezone.utc).isoformat()}
        if not self.scanning:self.save()

    def save(self):
        if self.path is None:return
        atomic_json(self.path,{'format':FORMAT,'records':self.records,
            'note':'Completed work only. Raw data and local completeness must be checked separately.'})

    def scan(self):
        self.scanning=True
        try:self._scan()
        finally:self.scanning=False
        self.save()

    def _scan(self):
        # Validate actual artifacts rather than trusting a copied status CSV.
        from review_data import who_valid
        from providers.cache import ResponseCache
        from .fotmobOnly import player_rows
        for marker in self.root.glob('*/*/match_checkpoints/*/CURRENT.json'):
            dataset=PandasStore(marker.parent).from_csv()
            for match in dataset.matches.values():
                directory=marker.parent.parent.parent
                slug=re.sub(r'[^a-z0-9]+','-',match.season.competition.name.lower()).strip('-')
                if (marker.parent.name!=str(match.id) or directory.parent.name!=slug
                        or directory.name!=season_label(match.season.name)):
                    raise ValueError(f'Checkpoint does not belong to its match/competition/season path: {marker}')
                self.record_match(match)
        for subtree,namespace,provider in (('whoscored_events','whoscored_events','whoscored'),
                                          ('fotmob_player_stats','fotmob_players','fotmob')):
            for schedule in self.root.glob(f'{subtree}/*/*/schedule.json'):
                directory=schedule.parent;job=directory/'job.json'
                if not job.exists():continue
                settings=json.loads(job.read_text(encoding='utf-8'))
                competition=competition_by_name(settings['competition']).name;season=settings['season']
                slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
                if (settings.get('provider')!=provider or directory.parent.name!=slug
                        or directory.name!=season_label(season)):
                    raise ValueError(f'Provider collection namespace/selection mismatch: {directory}')
                cache=ResponseCache(directory/'raw')
                for event in json.loads(schedule.read_text(encoding='utf-8'))['events']:
                    mid=event['id'];path=cache.path(provider,f'match/{mid}')
                    if not path.exists():continue
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    if saved.get('provider')!=provider or saved.get('key')!=f'match/{mid}':
                        raise ValueError('Imported cache identity differs from its path')
                    payload=saved['payload']
                    if provider=='whoscored':
                        if not who_valid(payload,mid,event):raise ValueError(f'Invalid WhoScored raw events: {path}')
                    else:player_rows(payload,competition,season,event)
                    self.record(namespace,competition,season,mid)

    def record_match(self, match):
        if match.metadata.get('id_namespace','sofascore')!='sofascore':return
        capabilities=['provider:'+key for key,value in match.metadata.get('provider_status',{}).items()
                      if value=='complete']
        if match.metadata.get('browser_details',{}).get('complete'):capabilities.append('browser_details')
        spatial=match.metadata.get('sofascore_spatial_status',{})
        terminal={'complete','unavailable','deferred'}
        for category in ('average_positions','shotmap'):
            if spatial.get(category) in terminal:capabilities.append('spatial:'+category)
        played=[p for lineup in match.lineups if lineup for p in lineup.players if p.did_play]
        for category in ('heatmap','rating_breakdown'):
            if played and all(spatial.get(str(p.player.id),{}).get(category) in terminal for p in played):
                capabilities.append('spatial:'+category)
        if capabilities:
            self.record('sofascore',match.season.competition.name,match.season.name,match.id,capabilities)


def remote_done(namespace, competition, season, mid, required=()):
    progress=ACTIVE.get()
    return progress is not None and progress.skip_remote and progress.done(namespace,competition,season,mid,required)


def record_complete(namespace, competition, season, mid):
    progress=ACTIVE.get()
    if progress is not None:progress.record(namespace,competition,season,mid)


def record_match(match):
    progress=ACTIVE.get()
    if progress is not None:progress.record_match(match)


@contextmanager
def progress_session(progress):
    token=ACTIVE.set(progress)
    try:yield progress
    finally:ACTIVE.reset(token)
