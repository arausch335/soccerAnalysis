"""Compare dated club histories using existing identities, never fuzzy club names."""
from datetime import date,timedelta
from functools import lru_cache

VERSION=2
THRESHOLD=80
DATE_TOLERANCE=30


@lru_cache(maxsize=4)
def _registry(root,signature):
    from concordance.sync import load
    return load(root)


def club_matcher(root=None):
    from .identityReview import review_root
    from .identity import team_key,REVIEWED_TEAM_IDS
    from .teamProfiles import same_reviewed_team,canonical_team_id,canonical_team_name
    from .reep import index
    from concordance.sync import local_path,snapshot_path
    root=review_root(root)
    signature=tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in
                    (local_path(root),snapshot_path(root)) if p.is_file())
    registry=_registry(str(root.resolve()),signature) if signature else None
    bridges=index(root)

    def same(left,right,left_provider,right_provider):
        a=left.get('club_id');b=right.get('club_id')
        if a is not None and b is not None:
            a=canonical_team_id(left_provider,a);b=canonical_team_id(right_provider,b)
            if same_reviewed_team(left_provider,a,right_provider,b):return True
            if left_provider==right_provider and a==b:return True
            for p,native,q,peer in ((left_provider,a,right_provider,b),(right_provider,b,left_provider,a)):
                if p=='sofascore' and (q,native) in REVIEWED_TEAM_IDS:
                    return REVIEWED_TEAM_IDS[q,native]==peer
            if registry:
                first=registry.lookup('team',left_provider,a);second=registry.lookup('team',right_provider,b)
                if first and second:return first==second
            linked=bridges.same('team',left_provider,a,right_provider,b)
            if linked is not None:return linked
            if left_provider==right_provider:return False
        # Older saved evidence may have names only. Use explicit approved aliases.
        names=[canonical_team_name(provider,info.get('club_id'),info.get('club',''))
               for info,provider in ((left,left_provider),(right,right_provider))]
        return bool(names[0] and names[1]) and team_key(names[0],root)==team_key(names[1],root)
    return same


def _union(intervals):
    merged=[]
    for start,end in sorted(intervals):
        if end<=start:continue
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    return merged


def _days(intervals):return sum((end-start).days for start,end in _union(intervals))


def compare_careers(left,right,*,root=None,as_of=None,same_club=None):
    """Weighted agreement in the shared coverage window, with 30-day date tolerance.

    Count each club/day once per provider. Concurrent reserve/loan entries count
    as separate evidence; absent early history and holes aren't contradictions.
    This percentage measures history agreement, not probability of identity.
    """
    today=as_of or date.today();same=same_club or club_matcher(root)
    def prepare(profile):
        rows=[];seen=set()
        for row in profile.get('career',[]):
            if not row.get('start'):continue
            start=date.fromisoformat(row['start']);end=min(date.fromisoformat(row['end']) if row.get('end') else today,today)
            token=(row.get('club_id'),row['club'],start,end)
            if (end-start).days>7 and token not in seen:
                rows.append((row,start,end));seen.add(token)
        return rows
    a=prepare(left);b=prepare(right)
    result={'similarity_percent':None,'threshold_percent':THRESHOLD,'compared_days':0,
            'matched_days':0,'date_tolerance_days':DATE_TOLERANCE,'state':'missing',
            'reason':'No comparable dated career history.'}
    if not a or not b:return result
    lo=max(min(s for _,s,_ in a),min(s for _,s,_ in b));hi=min(max(e for _,_,e in a),max(e for _,_,e in b))
    if hi<=lo:return result
    total=matched=0;pad=timedelta(days=DATE_TOLERANCE)
    for first,second,p,q in ((a,b,left['provider'],right['provider']),(b,a,right['provider'],left['provider'])):
        # Fold repeated rows/competition duplicates for the same identified club.
        groups=[]
        for row,start,end in first:
            group=next((g for g in groups if same(row,g[0],p,p)),None)
            if group is None:group=[row,[]];groups.append(group)
            group[1].append((max(start,lo),min(end,hi)))
        for row,intervals in groups:
            for start,end in _union(intervals):
                covered=[(max(start,s),min(end,e)) for _,s,e in second]
                comparable=_union(covered);total+=_days(comparable)
                agreements=[(max(start,s-pad),min(end,e+pad)) for other,s,e in second if same(row,other,p,q)]
                matched+=_days((max(s,x),min(e,y)) for s,e in comparable for x,y in agreements)
    result.update(compared_days=total,matched_days=matched,coverage_start=lo.isoformat(),coverage_end=hi.isoformat())
    if total<60:
        result['reason']='Less than 30 days of shared dated history.';return result
    score=100*matched/total
    result.update(similarity_percent=round(score,1),state='match' if score>=THRESHOLD else 'conflict',
        reason=f'Dated club history agrees for {score:.1f}% of comparable club-days (minimum {THRESHOLD}%).')
    return result
