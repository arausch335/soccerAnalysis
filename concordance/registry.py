"""Stable Alex IDs, provider aliases and recorded upgrades to published Reep IDs."""
import hashlib
import json
from pathlib import Path

from providers.reep import PROVIDERS
from providers.teamProfiles import canonical_team_id
from storage.pandasStore import atomic_json

FORMAT='soccer-alex-ids-v1'
LETTERS={'match':'m','team':'t','player':'p'}


class IdentityConflict(ValueError):pass


class Registry:
    def __init__(self,path,*,seed=None,value=None):
        self.path=Path(path)
        saved=self.path if self.path.exists() else Path(seed) if seed is not None and Path(seed).exists() else None
        self.value=json.loads(json.dumps(value)) if value is not None else json.loads(saved.read_text(encoding='utf-8')) if saved is not None else {
            'format':FORMAT,'aliases':{},'published':{},'redirects':{},'anchors':{}}
        if self.value.get('format')!=FORMAT:raise ValueError('Unsupported Alex ID registry')
        if any(not isinstance(self.value.get(k),dict) for k in ('aliases','published','redirects','anchors')):
            raise ValueError('Invalid Alex ID registry structure')
        for token,alias in self.value['aliases'].items():
            parts=token.split('|',2)
            if (len(parts)!=3 or parts[0] not in LETTERS or parts[1] not in PROVIDERS or not parts[2]
                    or not isinstance(alias,str) or not alias.startswith('a'+LETTERS[parts[0]])):
                raise ValueError('Invalid Alex registry provider alias')
        self.issues=[]
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

    def select_alex_id(self,kind,aliases,*,preferred=None,approved=False):
        """Choose one equivalent project key; never join contradictory entities."""
        if kind not in LETTERS:raise ValueError('Unknown identity kind')
        roots={self.follow(value) for value in aliases}
        if not roots or any(not value.startswith('a'+LETTERS[kind]) for value in roots):
            raise ValueError('Alex ID selection requires one entity namespace')
        published={self.canonical(value) for value in roots if value in self.value['published']}
        if len(published)>1:raise IdentityConflict('Alex IDs have contradictory published identities')
        baselines=set()
        for value in roots:
            baselines.update(self.baselines.get(value,set()))
            if value in self.value['anchors']:
                anchor=json.loads(self.value['anchors'][value])
                if anchor[1]=='sofascore':baselines.add(anchor[2])
        same_published=bool(published) and all(self.canonical(value) in published for value in roots)
        if len(baselines)>1 and not (approved or same_published):
            raise IdentityConflict('Alex IDs belong to distinct baseline entities')
        preferred=self.follow(preferred) if preferred else None
        winner=preferred if preferred in roots else min(roots)
        for old in roots-{winner}:self.value['redirects'][old]=winner
        if published:self.value['published'][winner]=next(iter(published))
        self.baselines[winner]=baselines
        return winner

    def alex_aliases_resolved(self):
        return sum(old.startswith(('am','at','ap')) and self.follow(old).startswith(('am','at','ap'))
                   and old!=self.follow(old) for old in self.value['redirects'])

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
        if len(known)>1:raise IdentityConflict('Contradictory published identities in Alex registry')
        if existing:
            sofa=set().union(*(self.baselines.get(x,set()) for x in existing))
            introduced=len(existing)>1 or ('sofascore' in peers and peers['sofascore'] not in sofa)
            if 'sofascore' in peers:sofa.add(peers['sofascore'])
            if len(sofa)>1 and introduced and not published:
                raise IdentityConflict('Peer ID would merge distinct baseline entities without an approved bridge')
            primary=self.value['aliases'].get(f'{kind}|{anchor[0]}|{anchor[1]}')
            alex=self.follow(primary) if primary else min(existing)
            alex=self.select_alex_id(kind,existing,preferred=alex,approved=bool(published))
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

    def note(self,kind,provider,native,reason,**values):
        item={'kind':kind,'provider':provider,'native_id':str(native),'reason':reason,**values}
        self.issues.append(item)

    def withhold(self,kind,provider,native,reason):
        token=f'{kind}|{provider}|{native}'
        self.value.setdefault('withheld',{})[token]=reason
        self.value['aliases'].pop(token,None)

    def reconcile(self,kind,native,published='',*,conflict=False,reject=(),verified=()):
        """Retain the baseline identity and withhold only disputed peer joins."""
        peers={p:str(native.get(p+'_id') or '') for p in PROVIDERS}
        if kind=='team':peers={p:canonical_team_id(p,v) if v else '' for p,v in peers.items()}
        anchor=next(((p,v) for p,v in peers.items() if v),None)
        if anchor is None:raise ValueError('An entity requires at least one provider ID')
        p,source=anchor;anchor_native={p+'_id':source};rejected={}
        existing=self.lookup(kind,p,source)
        disputed=False;published_conflict=False
        if published and existing and not existing.startswith(('am','at','ap')) and self.canonical(published)!=existing:
            self.note(kind,p,source,'published_id_disagreement',candidate_id=published,entity_id=existing)
            published='';disputed=True;published_conflict=True
        assigned=self.assign(kind,anchor_native,published,conflict=True)
        for provider,value in peers.items():
            if not value or provider==p:continue
            reason=''
            token=(kind,provider,value)
            peer=self.value['aliases'].get('|'.join(token))
            peer=self.follow(peer) if peer else None
            own=self.follow(assigned['alex_id'])
            peer_sofa=self.baselines.get(peer,set())
            own_sofa=self.baselines.get(own,set())
            same_published=bool(peer and self.canonical(peer)==assigned['entity_id'] and assigned['reep_id'])
            if token in verified:self.value.setdefault('withheld',{}).pop('|'.join(token),None)
            if published_conflict:reason='published_id_disagreement'
            elif (conflict and token not in verified) or token in reject or '|'.join(token) in self.value.get('withheld',{}):reason='ambiguous_provider_link'
            elif peer_sofa and own_sofa and peer_sofa!=own_sofa and not same_published:
                reason='distinct_baseline_entities'
            if not reason:
                try:assigned=self.assign(kind,{**anchor_native,provider+'_id':value},published)
                except IdentityConflict:reason='registry_provider_disagreement'
            if reason:
                rejected[provider]=value;disputed=True
                if provider!='sofascore' and token in reject:self.withhold(kind,provider,value,reason)
                self.note(kind,provider,value,reason,entity_id=assigned['entity_id'])
        assigned=self.assign(kind,anchor_native,conflict=True)
        return {**assigned,'rejected_ids':rejected,'safe_for_cross_provider_joins':not (disputed or conflict)}

    def merge(self,value):
        """Merge observations into this registry; existing authority wins ties."""
        incoming=Registry(self.path,value=value)
        for token,reason in incoming.value.get('withheld',{}).items():
            kind,provider,native=token.split('|',2)
            if provider!='sofascore':self.withhold(kind,provider,native,reason)
        groups={};translated={}
        for token,alias in incoming.value['aliases'].items():
            kind,provider,native=token.split('|',2)
            if kind not in LETTERS or provider not in PROVIDERS:raise ValueError('Invalid registry provider token')
            groups.setdefault((kind,incoming.follow(alias)),[]).append((provider,native))
        for (kind,old),tokens in sorted(groups.items()):
            tokens=sorted(tokens,key=lambda v:(PROVIDERS.index(v[0]),v[1]))
            # An entirely new group retains its already-minted project alias.
            # Existing Main mappings still win when any provider overlaps.
            if (old not in self.value['anchors'] and old not in self.value['redirects']
                    and not any(f'{kind}|{p}|{n}' in self.value['aliases'] or f'{kind}|{p}|{n}' in self.value.get('withheld',{}) for p,n in tokens)):
                self.value['anchors'][old]=incoming.value['anchors'].get(old,json.dumps([kind,*tokens[0]],separators=(',',':')))
                for p,n in tokens:self.value['aliases'][f'{kind}|{p}|{n}']=old
                published=incoming.canonical(old)
                if not published.startswith(('am','at','ap')):self.value['published'][old]=published
                self.baselines[old]={n for p,n in tokens if p=='sofascore'}
                translated[old]=old
                continue
            provider,native=tokens[0];anchor={provider+'_id':native}
            published=incoming.canonical(old)
            if published.startswith(('am','at','ap')):published=''
            results=[]
            for peer,number in tokens:
                peers={peer+'_id':number} if peer==provider else {**{peer+'_id':number},**anchor}
                result=self.reconcile(kind,peers,published)
                results.append(result)
            target=self.follow(results[0]['alex_id'])
            # A split identity cannot redirect all its old references to one
            # guessed destination. Keep it explicit in the conflict report.
            if any(r['rejected_ids'] or self.canonical(r['alex_id'])!=self.canonical(target) for r in results):
                self.note(kind,provider,native,'registry_group_split',candidate_id=old,entity_id=self.canonical(target))
            else:
                translated[old]=target
            if old!=target and old in translated:
                if old not in self.value['anchors'] and old not in self.value['redirects']:
                    self.value['redirects'][old]=target
                elif self.follow(old)!=target:
                    try:self.select_alex_id(kind,(old,target),preferred=target)
                    except IdentityConflict:
                        self.note(kind,provider,native,'registry_alias_disagreement',candidate_id=old,entity_id=self.canonical(target))
        for old in set(incoming.value['redirects'])|set(incoming.value['published']):
            if not old.startswith(('am','at','ap')):continue
            target=translated.get(incoming.follow(old))
            if target and old!=target and old not in self.value['anchors'] and old not in self.value['redirects']:
                self.value['redirects'][old]=target
        return self

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
