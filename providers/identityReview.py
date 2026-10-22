"""Human-reviewed identity links and bounded suggestions shared through direct reporting."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from storage.sqlitePaths import readonly_uri

_ROOT=ContextVar('identity_review_root',default=None)
PROVIDERS=('fotmob','whoscored')


def review_root(root=None):
    if root is not None:return Path(root)
    current=_ROOT.get()
    if current is not None:return current
    import config
    return Path(config.DATA_ROOT)


@contextmanager
def identity_scope(root):
    token=_ROOT.set(Path(root))
    try:yield
    finally:_ROOT.reset(token)


def stamp():return datetime.now(timezone.utc).isoformat()


def path_for(root=None):return review_root(root)/'_system'/'identity'/'review.sqlite'


@lru_cache(maxsize=16)
def _decisions(path,mtime):
    with sqlite3.connect(readonly_uri(Path(path).resolve()),uri=True) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM decisions')]


def decisions(root=None):
    path=path_for(root)
    if not path.exists():return []
    return _decisions(str(path),path.stat().st_mtime_ns)


def player_link(provider,pid,root=None):
    return next((d['target_id'] for d in decisions(root) if d['action']=='confirm' and
                 d['kind']=='player' and d['provider']==provider and d['source_id']==str(pid)),None)


def rejected_player_links(root=None):
    return {(d['provider'],d['source_id'],d['target_id']) for d in decisions(root)
            if d['action']=='reject' and d['kind']=='player'}


def team_aliases(root=None):
    return {d['source_id']:d['target_id'] for d in decisions(root)
            if d['action']=='confirm' and d['kind']=='team'}


def revision(root=None):
    links=sorted(tuple(d[k] for k in ('kind','provider','source_id','target_id')) for d in decisions(root) if d['action']=='confirm')
    from .reep import version
    reep=version(root)
    return hashlib.sha256(json.dumps([links,reep]).encode()).hexdigest() if links or reep else ''


def validate(proposal):
    proposal=dict(proposal) if isinstance(proposal,dict) else proposal
    if not isinstance(proposal,dict) or proposal.get('kind') not in ('player','team') or proposal.get('provider') not in PROVIDERS:
        raise ValueError('Invalid identity suggestion')
    for key in ('source_id','target_id','source_name','target_name'):
        if not isinstance(proposal.get(key),str) or not 0<len(proposal[key])<=200:
            raise ValueError('Identity IDs and names are required')
    if proposal['kind']=='player':
        for key in ('source_id','target_id'):
            if not re.fullmatch(r'[0-9]+(?:\.0+)?',proposal[key]):
                raise ValueError('Player mappings require stable provider IDs')
            proposal[key]=str(int(proposal[key].split('.')[0]))
    contexts=proposal.get('contexts',[])
    if not isinstance(contexts,list) or len(contexts)>5 or any(not isinstance(c,dict) or len(json.dumps(c))>2500 for c in contexts):
        raise ValueError('Invalid identity evidence')
    score=proposal.get('score',0)
    if type(score) not in (int,float) or not 0<=score<=1:raise ValueError('Invalid suggestion score')
    investigation=proposal.get('investigation')
    if investigation is not None:
        if not isinstance(investigation,dict) or len(json.dumps(investigation))>65000:
            raise ValueError('Invalid profile investigation')
        if not isinstance(investigation.get('checked_at'),str) or not investigation.get('checked_at'):
            raise ValueError('Profile lookup timestamp is required')
        errors=investigation.get('errors',[])
        if not isinstance(errors,list) or len(errors)>5 or any(not isinstance(e,str) or len(e)>400 for e in errors):raise ValueError('Invalid profile lookup errors')
        from .playerProfiles import compare_profiles,checked_profile
        for key,provider,pid in (('source',proposal['provider'],proposal['source_id']),('baseline','sofascore',proposal['target_id'])):
            if key in investigation:checked_profile(investigation[key],provider,pid)
        compare_profiles(proposal)
    key='|'.join(proposal[k] for k in ('kind','provider','source_id','target_id'))
    return {**proposal,'id':hashlib.sha256(key.encode()).hexdigest()[:24]}


class ReviewStore:
    def __init__(self,root):
        self.root=Path(root);self.path=path_for(root);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,payload TEXT);""")

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def ingest(self,proposals,worker='local'):
        if not isinstance(proposals,list) or len(proposals)>500:raise ValueError('Too many identity suggestions')
        proposals=[validate(p) for p in proposals]
        with self.connect() as db:
            for p in proposals:
                old=db.execute('SELECT payload FROM proposals WHERE id=?',(p['id'],)).fetchone()
                prior=json.loads(old[0]) if old else {}
                if prior.get('investigation',{}).get('checked_at','')>p.get('investigation',{}).get('checked_at',''):
                    p['investigation']=prior['investigation']
                contexts=prior.get('contexts',[])+[{**c,'worker':worker} for c in p.get('contexts',[])]
                unique={json.dumps(c,sort_keys=True):c for c in contexts}
                p={**p,'contexts':list(unique.values())[-5:],'first_seen':prior.get('first_seen',stamp()),'last_seen':stamp()}
                db.execute('INSERT OR REPLACE INTO proposals VALUES(?,?)',(p['id'],json.dumps(p)))
                reviewed=db.execute('SELECT payload FROM decisions WHERE id=?',(p['id'],)).fetchone()
                if reviewed and p.get('investigation'):
                    decision=json.loads(reviewed[0]);decision['investigation']=p['investigation']
                    db.execute('UPDATE decisions SET payload=? WHERE id=?',(json.dumps(decision),p['id']))

        if proposals:self.auto_confirm()

    def auto_confirm(self):
        from .playerProfiles import compare_profiles
        reviewed={d['id'] for d in decisions(self.root)};groups={}
        for p in self.proposals():
            if p['kind']=='player':groups.setdefault((p['provider'],p['source_id']),[]).append(p)
        for group in groups.values():
            if any(not p.get('investigation') for p in group):continue
            verified=[p for p in group if all(v=='match' for v in compare_profiles(p,self.root).values())]
            if len(verified)!=1 or verified[0]['id'] in reviewed:continue
            try:self.decide(verified[0]['id'],'confirm',automatic=True)
            except ValueError:continue  # Preserve human rejections and conflicting reviewed IDs.

    def proposals(self):
        with self.connect() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM proposals')]

    def snapshot(self):
        ds=decisions(self.root);by_id={d['id']:d for d in ds}
        confirmed={(d['kind'],d['provider'],d['source_id']) for d in ds if d['action']=='confirm'}
        pending=[p for p in self.proposals() if p['id'] not in by_id and (p['kind'],p['provider'],p['source_id']) not in confirmed]
        pending.sort(key=lambda p:(-p.get('score',0),p['source_name'],p['target_name']))
        return {'pending':pending[:200],'total':len(pending),'decisions':ds,'revision':revision(self.root)}

    def decide(self,pid,action,*,automatic=False):
        if action not in ('confirm','reject'):raise ValueError('Choose confirm or reject')
        with self.connect() as db:
            row=db.execute('SELECT payload FROM proposals WHERE id=?',(pid,)).fetchone()
            if not row:raise ValueError('Suggestion no longer exists; refresh the queue')
            p=validate(json.loads(row[0]))
            if action=='confirm' and p['kind']=='player' and not p.get('investigation'):
                raise ValueError('Birthday, club and career lookup is still queued')
            if automatic:
                from .playerProfiles import compare_profiles
                if not all(v=='match' for v in compare_profiles(p,self.root).values()):raise ValueError('Profile evidence is incomplete or conflicting')
                if db.execute('SELECT 1 FROM decisions WHERE id=?',(pid,)).fetchone():raise ValueError('Preserve the existing review decision')
            existing=[json.loads(r[0]) for r in db.execute('SELECT payload FROM decisions')]
            if action=='confirm':
                for d in existing:
                    same_source=d['kind']==p['kind'] and d['source_id']==p['source_id'] and (p['kind']=='team' or d['provider']==p['provider'])
                    if d['action']=='confirm' and same_source and d['target_id']!=p['target_id']:
                        raise ValueError('This identity already has a different confirmed link')
                if p['kind']=='player':
                    from .identity import REVIEWED_PLAYER_IDS, REJECTED_PLAYER_ID_LINKS
                    known=REVIEWED_PLAYER_IDS.get((p['provider'],p['source_id']))
                    if (known is not None and str(known)!=p['target_id']) or (p['provider'],p['source_id'],p['target_id']) in REJECTED_PLAYER_ID_LINKS:
                        raise ValueError('This link conflicts with an existing reviewed identity')
                else:
                    aliases=team_aliases(self.root)
                    target=p['target_id'];seen={p['source_id']}
                    while target in aliases:
                        if target in seen:raise ValueError('Team alias would create a cycle')
                        seen.add(target);target=aliases[target]
                    if target==p['source_id']:raise ValueError('Team alias would create a cycle')
            value={**p,'action':action,'reviewed_at':stamp(),'review_method':'profile_evidence' if automatic else 'manual'}
            db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?)',(pid,json.dumps(value)))
        _decisions.cache_clear()
        return value

    def replace_decisions(self,values):
        if not isinstance(values,list) or len(values)>20000:raise ValueError('Invalid shared identities')
        clean=[]
        for d in values:
            p=validate(d)
            if d.get('action') not in ('confirm','reject'):raise ValueError('Invalid identity decision')
            clean.append(p)
        with self.connect() as db:
            db.execute('DELETE FROM decisions')
            db.executemany('INSERT INTO decisions VALUES(?,?)',[(p['id'],json.dumps(p)) for p in clean])
        _decisions.cache_clear()


def similarity(a,b):
    from .identity import words
    a=''.join(words(a));b=''.join(words(b))
    return SequenceMatcher(None,a,b).ratio()


def suggest_players(root,provider,entry,candidates,context):
    if provider not in PROVIDERS or not entry.get('appeared',False):return []
    rows=[]
    for candidate in candidates:
        if candidate.starter!=entry.get('starter'):continue
        score=similarity(entry['name'],candidate.player.name)
        shirt=(entry.get('shirt_number') is not None and candidate.shirt_number is not None and str(entry['shirt_number'])==str(candidate.shirt_number))
        if score<.35 and not shirt:continue
        score=min(.99,.75*score+.25*shirt)
        rows.append(dict(kind='player',provider=provider,source_id=str(entry['id']),source_name=entry['name'],
            target_id=str(candidate.player.id),target_name=candidate.player.name,score=round(score,3),
            contexts=[{**context,'provider_shirt':entry.get('shirt_number'),'baseline_shirt':candidate.shirt_number}]))
    rows.sort(key=lambda p:-p['score'])
    if rows:ReviewStore(review_root(root)).ingest(rows[:3])
    return rows[:3]


def suggest_teams(root,provider,event,fixtures):
    from .identity import team_key
    if provider not in PROVIDERS:return
    wanted=[team_key(event[s+'Team']['name'],root) for s in ('home','away')]
    rows=[]
    for fixture in fixtures:
        if fixture['startTimestamp']//86400!=event['startTimestamp']//86400:continue
        actual=[team_key(fixture[s+'Team']['name'],root) for s in ('home','away')]
        # One independently matched opponent anchors a suggestion; no date-only links.
        if not any(a==b for a,b in zip(wanted,actual)):continue
        for i,side in enumerate(('home','away')):
            if wanted[i]==actual[i]:continue
            source=event[side+'Team']['name'];target=fixture[side+'Team']['name']
            score=similarity(source,target)
            if score<.35:continue
            rows.append(dict(kind='team',provider=provider,source_id=wanted[i],source_name=source,
                target_id=actual[i],target_name=target,score=round(score,3),contexts=[{
                    'match_id':str(event['id']),'provider_match_id':str(fixture['id']),
                    'match':event['homeTeam']['name']+' vs '+event['awayTeam']['name'],
                    'date':datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat(),'side':side}]))
    if rows:ReviewStore(review_root(root)).ingest(rows[:20])


def seed_existing(root):
    """Read exported mismatch tables and only the affected saved lineups; no requests."""
    import csv
    from storage import PandasStore
    store=ReviewStore(root)
    for table in Path(root).glob('*/*/identity_mismatches.csv'):
        with table.open(encoding='utf-8',newline='') as stream:rows=list(csv.DictReader(stream))
        matches={}
        for row in rows:
            if not row.get('provider_player_id') or 'cannot uniquely match' not in row.get('issue',''):continue
            mid=row['match_id']
            try:
                if mid not in matches:matches[mid]=PandasStore(table.parent/'match_checkpoints'/mid).from_csv().matches[int(mid)]
                match=matches[mid];lineup=getattr(match,row['side']+'_lineup')
                # Historical exports lack provider shirt/start flags. Rank names within the exact side.
                proposals=[]
                unresolved={str(r.get('baseline_player_id','')).split('.')[0] for r in rows
                            if r.get('match_id')==mid and r.get('provider')==row['provider']
                            and r.get('side')==row['side'] and r.get('baseline_player_id')
                            and 'absent from provider lineup' in r.get('issue','')}
                candidates=[p for p in lineup.players if not unresolved or str(p.player.id) in unresolved] if lineup else []
                for p in candidates:
                    score=similarity(row['provider_player_name'],p.player.name)
                    if score<.4:continue
                    proposals.append(dict(kind='player',provider=row['provider'],source_id=row['provider_player_id'],
                        source_name=row['provider_player_name'],target_id=str(p.player.id),target_name=p.player.name,
                        score=round(score*.75,3),contexts=[{'match_id':mid,'competition':row.get('competition'),
                        'season':row.get('season'),'match':match.home_team.name+' vs '+match.away_team.name,
                        'side':row['side'],'date':row.get('date'),'issue':row['issue']}]))
                proposals.sort(key=lambda p:-p['score']);store.ingest(proposals[:3])
            except (OSError,ValueError,KeyError,AttributeError):continue
    return store
