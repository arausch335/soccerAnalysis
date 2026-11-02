"""Explain temporary registrations without treating different clubs as one identity."""


def loan_status(value):
    for key in ('isLoan','onLoan'):
        if type(value.get(key)) is bool:return value[key]
    for key in ('transferType','type'):
        raw=value.get(key)
        values=[raw.get(k) for k in ('localizationKey','text','name')] if isinstance(raw,dict) else [raw]
        for text in values:
            if not isinstance(text,str):continue  # Numeric provider enums stay uninterpreted.
            text=''.join(c for c in text.casefold() if c.isalnum())
            if text in ('loan','onloan','loantransfer','transfertypeloan'):return True
            if text in ('permanent','permanenttransfer','free','freetransfer','loanreturn','returnfromloan','backfromloan','transfertypebackfromloan'):return False
    return None


def temporary_spells(rows,provider,same):
    """Recognize explicit loans or a dated A→B→A return within two years.

    A round trip can also be a short permanent move back: label that evidence as
    a temporary spell, not a confirmed loan. Explicit permanent moves aren't inferred.
    """
    result=[]
    for row,start,end in rows:
        if row.get('is_loan') is False:continue
        parent={'club':row.get('loan_parent_club') or row.get('from_club',''),
                'club_id':row.get('loan_parent_club_id') or row.get('from_club_id')}
        if row.get('is_loan') is True and parent['club']:
            if not same(row,parent,provider,provider):result.append((row,parent,start,end,'explicit_loan'))
            continue
        previous=[other for other,s,e in rows if abs((e-start).days)<=30 and s<start]
        if row.get('is_loan') is True and len(previous)==1 and not same(row,previous[0],provider,provider):
            before=previous[0]
            result.append((row,{'club':before['club'],'club_id':before.get('club_id')},start,end,'explicit_loan'));continue
        if not row.get('end') or not 7<(end-start).days<=730:continue
        following=[other for other,s,e in rows if abs((s-end).days)<=30 and s>start]
        def exclusive(parent):
            return not any((min(end,e)-max(start,s)).days>7 and
                not same(row,other,provider,provider) and not same(parent,other,provider,provider)
                for other,s,e in rows)
        # Transfer-from plus a reverse transfer proves the parent even if its
        # earlier stint falls outside the provider's history coverage.
        if parent['club']:
            returning=[other for other in following if same(parent,other,provider,provider) and
                       same(row,{'club':other.get('from_club',''),'club_id':other.get('from_club_id')},provider,provider)]
            if returning and not same(row,parent,provider,provider) and exclusive(parent):
                result.append((row,parent,start,end,'return_to_same_club'));continue
        for before in previous:
            if same(row,before,provider,provider):continue
            if exclusive(before) and any(same(before,after,provider,provider) for after in following):
                result.append((row,{'club':before['club'],'club_id':before.get('club_id')},start,end,'return_to_same_club'));break
    return result


def subtract(intervals,removed):
    result=[]
    for start,end in intervals:
        pending=[(start,end)]
        for lo,hi in removed:
            following=[]
            for a,b in pending:
                if b<=lo or a>=hi:following.append((a,b));continue
                if a<lo:following.append((a,lo))
                if b>hi:following.append((hi,b))
            pending=following
        result.extend(pending)
    return result


def loan_adjustments(a,b,left_provider,right_provider,same):
    """Exclude only borrower/lender rows explained by an omitted temporary spell.

    These days are missing coverage, never extra agreement. Other parallel club
    rows and explicitly different loans remain in the comparison.
    """
    spells=[temporary_spells(a,left_provider,same),temporary_spells(b,right_provider,same)]
    adjustments=[[],[]];evidence=[];conflicts=[]
    for first,parent,start,end,_ in spells[0]:
        if first.get('is_loan') is not True:continue
        for second,other_parent,s,e,_ in spells[1]:
            if second.get('is_loan') is not True:continue
            lo=max(start,s);hi=min(end,e)
            if (hi-lo).days>30 and same(parent,other_parent,left_provider,right_provider) and not same(first,second,left_provider,right_provider):
                conflicts.append({'left_borrower':first['club'],'right_borrower':second['club'],
                                  'start':lo.isoformat(),'end':hi.isoformat()})
    for side,first,second,p,q in ((0,a,b,left_provider,right_provider),(1,b,a,right_provider,left_provider)):
        for borrower,parent,start,end,kind in spells[side]:
            # A provider that already records this borrower hasn't omitted it.
            recorded=[(s,e) for row,s,e in second if same(borrower,row,p,q)]
            # A different documented borrowing club is contradictory evidence.
            contradictory=[(s,e) for row,_,s,e,_ in spells[1-side] if not same(borrower,row,p,q)]
            neutral=[];parent_neutral=[]
            parent_recorded=[(s,e) for row,s,e in first if same(parent,row,p,p)]
            for row,s,e in second:
                if not same(parent,row,p,q):continue
                periods=subtract([(max(start,s),min(end,e))],recorded+contradictory)
                periods=[(x,y) for x,y in periods if y>x]
                neutral.extend(periods)
                parent_periods=subtract([(max(start,s),min(end,e))],parent_recorded+contradictory)
                parent_periods=[(x,y) for x,y in parent_periods if y>x]
                adjustments[1-side].append((row,parent_periods));parent_neutral.extend(parent_periods)
            if neutral or parent_neutral:
                adjustments[side].append((borrower,neutral))
                evidence.append({'provider':p,'borrower':borrower['club'],'parent':parent['club'],
                                 'start':start.isoformat(),'end':end.isoformat(),'evidence':kind})
    return adjustments,evidence,conflicts
