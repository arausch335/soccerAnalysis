"""Provider profile evidence for identity resolution; no name-only automatic merges."""
from datetime import date,datetime,timedelta,timezone
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from .httpClient import ProviderError

HOSTS={'sofascore':'www.sofascore.com','fotmob':'www.fotmob.com','whoscored':'www.whoscored.com'}


def _object(value,label):
    if value is None:return {}
    if not isinstance(value,dict):raise ProviderError(label+' is not an object')
    return value


def day(value):
    if value in (None,''):return None
    try:
        if type(value) in (int,float):return datetime.fromtimestamp(value,timezone.utc).date().isoformat()
        if isinstance(value,dict):value=value.get('utcTime')
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError,TypeError,OverflowError,OSError):return None


def checked_profile(value,provider,pid):
    if not isinstance(value,dict) or value.get('provider')!=provider or str(value.get('id'))!=str(pid):
        raise ValueError('Profile identity differs from requested player')
    if len(json.dumps(value))>30000:raise ValueError('Profile evidence too large')
    if value.get('club') is not None and (not isinstance(value['club'],str) or len(value['club'])>150):
        raise ValueError('Invalid current club')
    sources=value.get('sources',[])
    if not sources or len(sources)>3:raise ValueError('Profile source URLs are required')
    for source in sources:
        url=urlsplit(source)
        if url.scheme!='https' or url.hostname!=HOSTS[provider] or url.username or url.password or url.port or url.query:
            raise ValueError('Profile source left its provider')
        pattern=(rf'(?:/api/v1/player/{pid}(?:/transfer-history)?|/football/player/[^/?#]+/{pid})' if provider=='sofascore' else
                 rf'/players/{pid}(?:/[^/?#]+)?' if provider=='fotmob' else rf'/players/{pid}/(?:show|history)(?:/[^/?#]+)?')
        if not re.fullmatch(pattern,url.path,re.I):raise ValueError('Profile URL identifies another player')
    career=value.get('career',[])
    if not isinstance(career,list) or len(career)>60:raise ValueError('Invalid career history')
    for row in career:
        if not isinstance(row,dict) or not isinstance(row.get('club'),str) or len(row['club'])>150:
            raise ValueError('Invalid career club')
        for key in ('start','end'):
            if row.get(key) is not None and day(row[key])!=row[key]:raise ValueError('Invalid career date')
        if row.get('start') and row.get('end') and row['start']>row['end']:raise ValueError('Reversed career dates')
    if value.get('birth_date') and day(value['birth_date'])!=value['birth_date']:raise ValueError('Invalid birthday')
    return value


def sofa_profile(pid,payload,history):
    p=_object(_object(payload,'Sofascore profile').get('player'),'Sofascore player')
    if str(p.get('id'))!=str(pid):raise ProviderError('Sofascore profile belongs to another player')
    transfers=_object(history,'Sofascore history').get('transferHistory')
    if not isinstance(transfers,list):raise ProviderError('Sofascore career history unavailable')
    changes=[]
    for t in transfers:
        t=_object(t,'Sofascore transfer')
        if str(_object(t.get('player'),'Sofascore transfer player').get('id'))!=str(pid):raise ProviderError('Sofascore transfer belongs to another player')
        start=day(t.get('transferDateTimestamp'));club=_object(t.get('transferTo'),'Sofascore transfer destination').get('name') or t.get('toTeamName')
        if start and club:changes.append((start,club))
    changes=sorted(set(changes))
    career=[{'club':club,'start':start,'end':changes[i+1][0] if i+1<len(changes) else None}
            for i,(start,club) in enumerate(changes)]
    return {'provider':'sofascore','id':str(pid),'name':p.get('name',''),'birth_date':day(p.get('dateOfBirthTimestamp')),
            'club':_object(p.get('team'),'Sofascore current team').get('name'),'career':career,
            'sources':[f'https://www.sofascore.com/api/v1/player/{pid}',f'https://www.sofascore.com/api/v1/player/{pid}/transfer-history']}


def fotmob_profile(pid,p):
    p=_object(p,'FotMob profile')
    if str(p.get('id'))!=str(pid):raise ProviderError('FotMob profile belongs to another player')
    history=_object(p.get('careerHistory'),'FotMob career history')
    items=_object(history.get('careerItems'),'FotMob career items')
    senior=_object(items.get('senior'),'FotMob senior career')
    rows=senior.get('teamEntries') or []
    if not isinstance(rows,list):raise ProviderError('FotMob career entries are not a list')
    career=[]
    for row in rows:
        row=_object(row,'FotMob career entry')
        participant=row.get('participantId')
        if participant not in (None,'') and str(participant)!=str(pid):
            raise ProviderError('FotMob career row identifies another player')
        if row.get('team') and not row.get('hasUncertainData'):
            career.append({'club':row['team'],'start':day(row.get('startDate')),'end':day(row.get('endDate'))})
    birthday=day(p.get('birthDate'))
    if birthday=='0001-01-01':birthday=None  # Provider placeholder for an unknown birthday.
    return {'provider':'fotmob','id':str(pid),'name':p.get('name',''),'birth_date':birthday,
            'club':_object(p.get('primaryTeam'),'FotMob current team').get('teamName'),'career':career,
            'sources':[f'https://www.fotmob.com/players/{pid}']}


class _Node:
    def __init__(self,tag='',attrs=(),parent=None):self.tag=tag;self.attrs=dict(attrs);self.parent=parent;self.children=[]
    def text(self):return ' '.join(c.text() if isinstance(c,_Node) else c for c in self.children)
    def nodes(self,tag):
        for c in self.children:
            if isinstance(c,_Node):
                if c.tag==tag:yield c
                yield from c.nodes(tag)


class _Document(HTMLParser):
    def __init__(self):super().__init__();self.root=_Node();self.current=self.root
    def handle_starttag(self,tag,attrs):
        n=_Node(tag,attrs,self.current);self.current.children.append(n)
        if tag not in ('img','meta','link','br','hr','input','source','wbr','area','base','embed','param'):self.current=n
    def handle_endtag(self,tag):
        n=self.current
        while n.parent and n.tag!=tag:n=n.parent
        if n.parent:self.current=n.parent
    def handle_data(self,data):self.current.children.append(data.strip())


def whoscored_profile(pid,text):
    dom=_Document();dom.feed(text)
    image=next((n for n in dom.root.nodes('img') if re.search(rf'/players/{pid}\.(?:png|jpg)',n.attrs.get('src',''))),None)
    heading=next((n for n in dom.root.nodes('h1') if 'header-name' in n.attrs.get('class','')),None)
    if image is None or heading is None:raise ProviderError('WhoScored profile identity unavailable')
    header=heading.parent;body=header.text()
    birthday=re.search(r'Age:.*?\(\s*(\d{2})-(\d{2})-(\d{4})\s*\)',body)
    club=next((n.text().strip() for n in header.nodes('a') if 'team-link' in n.attrs.get('class','') and n.text().strip()),None)
    career=[]
    table=next((n for n in dom.root.nodes('table') if n.attrs.get('id')=='top-player-stats-summary-grid'),None)
    if table:
        for row in table.nodes('tr'):
            links=list(row.nodes('a'))
            if any('/international-' in n.attrs.get('href','').lower() for n in links):continue
            team=next((n for n in links if re.match(r'/teams/\d+/',n.attrs.get('href',''),re.I)),None)
            season=re.search(r'(?<!\d)(20\d{2})(?:/(20\d{2}|\d{2}))?',row.text())
            if team is None or season is None:continue
            year=int(season[1]);second=season[2]
            endyear=int(second) if second and len(second)==4 else year+1 if second else year
            career.append({'club':team.text().strip(),'start':f'{year}-07-01' if second else f'{year}-01-01',
                           'end':f'{endyear}-06-30' if second else f'{year}-12-31','precision':'season'})
    # History repeats a club/season for every tournament it covered.
    career=list({(row['club'],row['start'],row['end']):row for row in career}.values())
    return {'provider':'whoscored','id':str(pid),'name':heading.text().strip(),
            'birth_date':day(f'{birthday[3]}-{birthday[2]}-{birthday[1]}') if birthday else None,
            'club':club,'career':career,'sources':[f'https://www.whoscored.com/players/{pid}/history']}


def compare_profiles(proposal,root=None):
    """Verify exact dates and clubs; inconsistent overlapping career stints veto a link."""
    from .identity import team_key
    evidence=proposal.get('investigation',{});left=evidence.get('source');right=evidence.get('baseline')
    checks={'birthday':'missing','club':'missing','career':'missing'}
    if not left or not right:return checks
    checked_profile(left,proposal['provider'],proposal['source_id']);checked_profile(right,'sofascore',proposal['target_id'])
    if left.get('birth_date') and right.get('birth_date'):
        checks['birthday']='match' if left['birth_date']==right['birth_date'] else 'conflict'
    if left.get('club') and right.get('club'):
        checks['club']='match' if team_key(left['club'],root)==team_key(right['club'],root) else 'conflict'
    a=[r for r in left['career'] if r.get('start')];b=[r for r in right['career'] if r.get('start')]
    if not a or not b:return checks
    lo=max(min(r['start'] for r in a),min(r['start'] for r in b));hi=date.today().isoformat()
    shared=set();conflict=False;missing=False
    for first,second in ((a,b),(b,a)):
        for row in first:
            start=max(row['start'],lo);end=min(row.get('end') or hi,hi)
            days=(date.fromisoformat(end)-date.fromisoformat(start)).days
            if days<=7:continue  # Administrative loan-return entries can last one day.
            matches=[r for r in second if team_key(row['club'],root)==team_key(r['club'],root)
                     and (date.fromisoformat(min(r.get('end') or hi,end))-date.fromisoformat(max(r['start'],start))).days>=min(30,days-7)]
            if matches:shared.add(team_key(row['club'],root))
            else:
                missing=True
                # A club outside the other source's coverage is missing evidence.
                overlapping=[r for r in second if r['start']<=end and (r.get('end') or hi)>=start]
                if overlapping and row.get('precision')!='season' and all(r.get('precision')!='season' for r in overlapping):conflict=True
    if conflict:checks['career']='conflict'
    elif len(shared)>=2 and not missing:checks['career']='match'
    return checks


class ProfileVerifier:
    def __init__(self,clients,root=None,report=print,on_provider=None):
        from .identityReview import review_root,ReviewStore
        self.clients=clients;self.root=review_root(root);self.store=ReviewStore(self.root);self.report=report;self.on_provider=on_provider
        with self.store.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS profiles(provider TEXT,id TEXT,payload TEXT,expires TEXT,PRIMARY KEY(provider,id))')

    def profile(self,provider,pid):
        from .identityReview import stamp
        with self.store.connect() as db:row=db.execute('SELECT payload,expires FROM profiles WHERE provider=? AND id=?',(provider,str(pid))).fetchone()
        if row and row[1]>stamp():return json.loads(row[0])
        client=self.clients.get(provider)
        if getattr(client,'requires_manual_attention',False) is True:return {'error':provider+' extension needs attention; lookup deferred to review'}
        fetch=getattr(client,'player_profile',None)
        if not callable(fetch):return {'error':provider+' player-profile lookup is unavailable'}
        if self.on_provider:self.on_provider(provider)
        self.report(f'[Identity lookup] {provider} player {pid}: checking birthday, club and career.')
        try:
            value={**checked_profile(fetch(int(pid)),provider,str(pid)),'fetched_at':stamp()}
            ttl=timedelta(days=7) if all(value.get(k) for k in ('birth_date','club','career')) else timedelta(hours=6)
        except (ProviderError,ValueError,KeyError,TypeError,AttributeError,IndexError,OSError) as exc:
            value={'error':str(exc)[:300]};ttl=timedelta(hours=6)
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',(provider,str(pid),json.dumps(value),(datetime.now(timezone.utc)+ttl).isoformat()))
        return value

    def investigate(self,proposals):
        from .identityReview import stamp
        completed=[]
        for p in proposals:
            if p['kind']!='player':continue
            left=self.profile(p['provider'],p['source_id']);right=self.profile('sofascore',p['target_id'])
            investigation={'workflow':'main-profile-review-v1','checked_at':stamp(),'status':'checked','errors':[v['error'] for v in (left,right) if 'error' in v]}
            if 'error' not in left:investigation['source']=left
            if 'error' not in right:investigation['baseline']=right
            for name,value in ((p['provider'],left),('sofascore',right)):
                if 'error' in value:continue
                missing=[label for key,label in (('birth_date','birthday'),('club','current club'),('career','career history')) if not value.get(key)]
                if missing:investigation['errors'].append(name+': '+', '.join(missing)+' unavailable; available profile evidence retained.')
            p={**p,'investigation':investigation};checks=compare_profiles(p,self.root)
            p['investigation']['checks']=checks
            p['investigation']['status']='verified' if all(v=='match' for v in checks.values()) else 'conflict' if 'conflict' in checks.values() else 'incomplete'
            completed.append(p)
        if completed:self.store.ingest(completed)
        return completed

    def pending(self,limit=1):
        values=self.store.snapshot()['pending'];groups={}
        for p in values:
            if p['kind']=='player' and not p.get('investigation'):
                groups.setdefault((p['provider'],p['source_id']),[]).append(p)
        for rows in list(groups.values())[:limit]:self.investigate(rows)


def needs_profile_check(proposal):
    evidence=proposal.get('investigation')
    if not evidence:return True
    if not evidence.get('errors'):return False
    # Revisit failed checks inherited from the former worker workflow once.
    if evidence.get('workflow')!='main-profile-review-v1':return True
    try:
        checked=datetime.fromisoformat(evidence['checked_at'])
        if checked.tzinfo is None:return True
        return checked<datetime.now(timezone.utc)-timedelta(hours=6)
    except (KeyError,TypeError,ValueError):return True


LEGACY_EXTENSION_ERRORS=frozenset(('Unknown collector command.','Page returned no collection data.',
                                  'WhoScored profile needs its loaded career table',
                                  'FotMob career belongs to another player'))


def retry_legacy_extension_failures(root):
    """Retry incompatible-extension errors once a capable extension connects."""
    from .identityReview import ReviewStore
    store=ReviewStore(root);reset=0
    with store.connect() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone():
            for provider,pid,payload in db.execute('SELECT provider,id,payload FROM profiles').fetchall():
                if json.loads(payload).get('error') in LEGACY_EXTENSION_ERRORS:
                    db.execute('DELETE FROM profiles WHERE provider=? AND id=?',(provider,pid))
        for pid,payload in db.execute('SELECT id,payload FROM proposals').fetchall():
            proposal=json.loads(payload)
            if any(error in LEGACY_EXTENSION_ERRORS for error in proposal.get('investigation',{}).get('errors',[])):
                # Human decisions and successful cached profiles remain intact.
                proposal.pop('investigation',None)
                db.execute('UPDATE proposals SET payload=? WHERE id=?',(json.dumps(proposal),pid));reset+=1
    return reset


def investigate_queue(root,report=print):
    """Main checks queued profiles through its explicitly enabled extension session."""
    from contextlib import ExitStack
    from .identityReview import ReviewStore
    rows=ReviewStore(root).snapshot()['pending']
    todo=[p for p in rows if p['kind']=='player' and needs_profile_check(p)]
    if not todo:return False
    provider=todo[0]['provider']
    from .transports.sofascore import create_sofascore_client
    from .transports.fotmob import create_fotmob_client
    from .transports.whoscored import create_whoscored_client
    factories={'sofascore':create_sofascore_client,'fotmob':create_fotmob_client,'whoscored':create_whoscored_client}
    with ExitStack() as stack:
        clients={}
        for name in ('sofascore',provider):
            client=factories[name](root,report=report);stack.callback(client.close);clients[name]=client
        verifier=ProfileVerifier(clients,root,report)
        selected=[p for p in todo if (p['provider'],p['source_id'])==(provider,todo[0]['source_id'])]
        verifier.investigate(selected)
    return True
