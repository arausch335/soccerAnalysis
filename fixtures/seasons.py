"""Resolve published cup editions without interpreting January as a start year."""
from datetime import datetime,timezone
import re

from collection.selection import season_label


def years(competition,start,end):
    last=end if competition.last_season is None else min(end,competition.last_season)
    return range(max(start,competition.first_season),last+1)


def select(competition,start,seasons,*,label=lambda s:s):
    """Prefer an exact paired season; single-year supercups use a date window.

    Both adjacent calendar editions can contain fixtures in a July–June season.
    No single-year convention is guessed for a competition/provider. Callers
    filter their fixtures to the target date window and preserve native IDs.
    """
    paired=[];calendar={}
    for value in seasons:
        name=str(label(value)).strip()
        if re.fullmatch(r'\d{4}',name):
            calendar.setdefault(int(name),[]).append(value)
            continue
        try:key=season_label(name).replace('-','/')
        except ValueError:continue
        if key==f'{start}/{start+1}':paired.append(value)
    if len(paired)>1:raise ValueError(f'Ambiguous {competition.name} season {start}/{start+1}')
    if paired:return paired,False
    if competition.kind=='supercup':
        candidates=[]
        for year in (start,start+1):
            found=calendar.get(year,[])
            if len(found)>1:raise ValueError(f'Ambiguous {competition.name} calendar edition {year}')
            candidates.extend(found)
        return candidates,True
    # Existing league APIs sometimes publish a start year alone.
    return calendar.get(start,[]),False


def in_window(event,start):
    stamp=event['startTimestamp']
    return (datetime(start,7,1,tzinfo=timezone.utc).timestamp()<=stamp
        <datetime(start+1,7,1,tzinfo=timezone.utc).timestamp())
