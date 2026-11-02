"""Resolve published cup editions without interpreting January as a start year."""
from datetime import datetime,timezone
import re

from collection.selection import season_label

SUPERCUP_MAPPING = 2


def calendar_year(name):
    # Providers also publish labels such as "Supercoppa 2025". Do not
    # interpret a paired or otherwise multi-year label as a calendar edition.
    found=re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',name)
    if len(found)==1 and not re.search(r'\d\s*[-/–—]\s*\d',name):return int(found[0])
    return None


def years(competition,start,end):
    last=end if competition.last_season is None else min(end,competition.last_season)
    return range(max(start,competition.first_season),last+1)


def select(competition,start,seasons,*,label=lambda s:s):
    """Regular cups prefer exact seasons; supercups use a playing-date window.

    Both adjacent calendar editions can contain fixtures in a July–June season.
    No single-year convention is guessed for a competition/provider. Callers
    filter their fixtures to the target date window and preserve native IDs.
    """
    paired=[];calendar={};nearby=[]
    for value in seasons:
        name=str(label(value)).strip()
        year=calendar_year(name) if competition.kind=='supercup' else int(name) if re.fullmatch(r'\d{4}',name) else None
        if year is not None:
            calendar.setdefault(year,[]).append(value)
            continue
        try:key=season_label(name).replace('-','/')
        except ValueError:continue
        if key==f'{start}/{start+1}':paired.append(value)
        if start-1<=int(key[:4])<=start+1:nearby.append(value)
    if competition.kind=='supercup':
        # A title-season label can name the preceding league campaign, while
        # another provider uses the year the final was played. Read adjacent
        # published editions and assign by kickoff, never by label alone.
        candidates=[*nearby,*calendar.get(start,[]),*calendar.get(start+1,[])]
        unique=[]
        for value in candidates:
            if value not in unique:unique.append(value)
        return unique,True
    if len(paired)>1:raise ValueError(f'Ambiguous {competition.name} season {start}/{start+1}')
    if paired:return paired,False
    # Existing league APIs sometimes publish a start year alone.
    return calendar.get(start,[]),False


def in_window(event,start):
    stamp=event['startTimestamp']
    return (datetime(start,7,1,tzinfo=timezone.utc).timestamp()<=stamp
        <datetime(start+1,7,1,tzinfo=timezone.utc).timestamp())
