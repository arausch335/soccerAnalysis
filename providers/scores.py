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
    score={'current':extra if extra is not None else normal}
    if normal is not None:score['normaltime']=normal
    if extra is not None and normal is not None:score['overtime']=extra-normal
    penalties=values.get('penalties',values.get('penalty'))
    if penalties is not None:score['penalties']=penalties
    return score
