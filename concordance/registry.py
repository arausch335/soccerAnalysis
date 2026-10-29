"""Stable Alex IDs, provider aliases and recorded upgrades to published Reep IDs."""
import hashlib
import json
from pathlib import Path

from providers.reep import PROVIDERS
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
            anchors=[json.loads(self.value['anchors'][x]) for x in existing if x in self.value['anchors']]
            sofa={row[2] for row in anchors if row[1]=='sofascore'}
            if len(sofa)>1 and not known:
                raise ValueError('Peer ID would merge distinct baseline entities without an approved bridge')
            alex=min(existing)
            for old in existing-{alex}:
                self.value['redirects'][old]=alex
                if old in self.value['published']:self.value['published'][alex]=self.value['published'][old]
        else:
            seed=json.dumps([kind,*anchor],separators=(',',':'))
            alex='a'+LETTERS[kind]+hashlib.sha256(seed.encode()).hexdigest()[:24]
            if alex in self.value['anchors'] and self.value['anchors'][alex]!=seed:
                raise ValueError('Alex ID hash collision')
            self.value['anchors'][alex]=seed
        for token in tokens:self.value['aliases'][token]=alex
        reep=next(iter(known),'')
        if reep:self.value['published'][alex]=reep
        return {'entity_id':reep or alex,'reep_id':reep,'alex_id':alex,
                'id_source':'reep' if reep else 'alex'}

    def save(self):atomic_json(self.path,self.value,sort_keys=True)

    def redirects(self):
        result=[]
        for old in sorted(set(self.value['redirects'])|set(self.value['published'])):
            target=self.follow(old);target=self.value['published'].get(target,target)
            if old!=target:result.append({'from_id':old,'to_id':target})
        return result
