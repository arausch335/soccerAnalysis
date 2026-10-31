"""Stable Alex IDs, provider aliases and recorded upgrades to published Reep IDs."""
import hashlib
import json
from pathlib import Path

from providers.reep import PROVIDERS
from providers.teamProfiles import canonical_team_id
from storage.pandasStore import atomic_json

FORMAT='soccer-alex-ids-v1'
LETTERS={'match':'m','team':'t','player':'p'}


class Registry:
    def __init__(self,path,*,seed=None):
        self.path=Path(path)
        saved=self.path if self.path.exists() else Path(seed) if seed is not None and Path(seed).exists() else None
        self.value=json.loads(saved.read_text(encoding='utf-8')) if saved is not None else {
            'format':FORMAT,'aliases':{},'published':{},'redirects':{},'anchors':{}}
        if self.value.get('format')!=FORMAT:raise ValueError('Unsupported Alex ID registry')
        self.baselines={}
        for token,alias in self.value['aliases'].items():
            kind,provider,native=token.split('|',2)
            if provider=='sofascore':self.baselines.setdefault(self.follow(alias),set()).add(native)

    def canonical(self,value):
        value=self.follow(value)
        return self.follow(self.value['published'].get(value,value))

    def lookup(self,kind,provider,native):
        if kind=='team':native=canonical_team_id(provider,native)
        alias=self.value['aliases'].get(f'{kind}|{provider}|{native}')
        return self.canonical(alias) if alias else ''

    def follow(self,value):
        seen=set()
        while value in self.value['redirects']:
            if value in seen:raise ValueError('Cyclic Alex ID redirect')
            seen.add(value);value=self.value['redirects'][value]
        return value

    def assign(self,kind,native,published='',*,conflict=False):
        if kind not in LETTERS:raise ValueError('Unknown identity kind')
        peers={p:str(native.get(p+'_id') or '') for p in PROVIDERS}
        peers={p:v for p,v in peers.items() if v}
        if not peers:raise ValueError('An entity requires at least one provider ID')
        if published.startswith(('am','at','ap')):raise ValueError('An Alex ID is not a published Reep ID')
        anchor=next((p,peers[p]) for p in PROVIDERS if p in peers)
        # A conflicted observation cannot bind peer IDs into a new equivalence.
        tokens=[f'{kind}|{p}|{v}' for p,v in ([(anchor[0],anchor[1])] if conflict else peers.items())]
        existing={self.follow(self.value['aliases'][t]) for t in tokens if t in self.value['aliases']}
        known={self.follow(self.value['published'][x]) for x in existing if x in self.value['published']}
        if published:known.add(self.follow(published))
        if len(known)>1:raise ValueError('Contradictory published identities in Alex registry')
        if existing:
            sofa=set().union(*(self.baselines.get(x,set()) for x in existing))
            introduced=len(existing)>1 or ('sofascore' in peers and peers['sofascore'] not in sofa)
            if 'sofascore' in peers:sofa.add(peers['sofascore'])
            if len(sofa)>1 and introduced and not published:
                raise ValueError('Peer ID would merge distinct baseline entities without an approved bridge')
            primary=self.value['aliases'].get(f'{kind}|{anchor[0]}|{anchor[1]}')
            alex=self.follow(primary) if primary else min(existing)
            for old in existing-{alex}:
                self.value['redirects'][old]=alex
                if old in self.value['published']:self.value['published'][alex]=self.value['published'][old]
        else:
            seed=json.dumps([kind,*anchor],separators=(',',':'))
            alex='a'+LETTERS[kind]+hashlib.sha256(seed.encode()).hexdigest()[:24]
            if alex in self.value['anchors'] and self.value['anchors'][alex]!=seed:
                raise ValueError('Alex ID hash collision')
            self.value['anchors'][alex]=seed
        baseline=set().union(*(self.baselines.get(x,set()) for x in existing))
        if 'sofascore' in peers:baseline.add(peers['sofascore'])
        self.baselines[alex]=baseline
        for token in tokens:self.value['aliases'][token]=alex
        reep=next(iter(known),'')
        if reep:self.value['published'][alex]=reep
        return {'entity_id':reep or alex,'reep_id':reep,'alex_id':alex,
                'id_source':'reep' if reep else 'alex'}

    def validate_rows(self,kind,rows,*,prefix=''):
        """Check provider-to-canonical joins; disputed peers are not evidence."""
        checked=0
        for row in rows:
            entity=row.get(prefix+'entity_id','')
            peers=[(p,str(row.get(prefix+p+'_id') or '')) for p in PROVIDERS]
            peers=[(p,v) for p,v in peers if v]
            safe=str(row.get(prefix+'safe_for_cross_provider_joins',False)).lower()=='true'
            published=row.get(prefix+'reep_id','')
            if safe and published and self.canonical(published)!=entity:
                raise ValueError('Published Reep/canonical ID disagreement')
            for provider,native in peers if safe else peers[:1]:
                if self.lookup(kind,provider,native)!=entity:
                    raise ValueError('Provider/canonical ID disagreement: '+str((kind,provider,native,entity)))
                checked+=1
            if safe and row.get(prefix+'provider_aliases'):
                aliases=json.loads(row[prefix+'provider_aliases'])
                for p in PROVIDERS:
                    for native in aliases.get(p+'_id',[]):
                        if self.lookup(kind,p,native)!=entity:
                            raise ValueError('Provider alias/canonical ID disagreement: '+str((kind,p,native)))
        return checked

    def save(self):atomic_json(self.path,self.value,sort_keys=True)

    def redirects(self):
        result=[]
        for old in sorted(set(self.value['redirects'])|set(self.value['published'])):
            target=self.follow(old);target=self.value['published'].get(target,target)
            if old!=target:result.append({'from_id':old,'to_id':target})
        return result
