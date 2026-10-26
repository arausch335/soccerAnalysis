"""Goals through the end of play, including extra time and excluding shootouts."""


def final_score(event,side):
    values=event.get(side+'Score') or {}
    awarded=event.get('isAwarded') or event.get('status',{}).get('awarded')
    # Sofascore display is the on-field total; current can include shootout
    # kicks. Prefer display over inconsistent historical overtime fields.
    keys=('display','normaltime','current') if awarded else ('display',)
    for key in keys:
        if values.get(key) is not None:return values[key]
    # Normalized WhoScored/FotMob events use current for the on-field total.
    if values.get('penalties') is None and values.get('current') is not None:return values['current']
    normal=values.get('normaltime');extra=values.get('overtime')
    if values.get('extra1') is not None and values.get('extra2') is not None:
        extra=values['extra1']+values['extra2']
    if normal is not None:return normal+(extra or 0)
    if values.get('current') is not None:return values['current']
    return None


def whoscored_scores(values):
    normal=values.get('fulltime')
    extra=values.get('extratime',values.get('extraTime'))
    # Match-centre running is the on-field total, including extra time but
    # excluding shootouts. Extra-time fields can be absent for one team.
    total=values.get('running')
    if total is not None and normal is not None and total<normal:
        raise ValueError('WhoScored running score is below its regulation score')
    if total is None:
        totals=[v for v in (normal,extra) if v is not None]
        # These are cumulative scores: a later period cannot erase goals.
        total=max(totals) if totals else None
    score={'current':total,'display':total}
    if normal is not None:score['normaltime']=normal
    if total is not None and normal is not None and (extra is not None or values.get('running') is not None):
        score['overtime']=total-normal
    penalties=values.get('penalties',values.get('penalty'))
    if penalties is not None:score['penalties']=penalties
    return score
