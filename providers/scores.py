"""Goals through the end of play, including extra time and excluding shootouts."""


def final_score(event,side):
    values=event.get(side+'Score') or {}
    awarded=event.get('isAwarded') or event.get('status',{}).get('awarded')
    keys=('display','normaltime','current') if awarded else ('overtime','current','display','normaltime')
    # Some feeds fold shootout kicks into current/display. A recorded period
    # boundary is authoritative; never add penalty kicks to match goals.
    if not awarded and values.get('penalties') is not None:
        keys=('overtime','normaltime','current','display')
    for key in keys:
        if values.get(key) is not None:return values[key]
    return None


def whoscored_scores(values):
    normal=values.get('fulltime')
    extra=values.get('extratime',values.get('extraTime'))
    score={'current':extra if extra is not None else normal}
    if normal is not None:score['normaltime']=normal
    if extra is not None:score['overtime']=extra
    return score
