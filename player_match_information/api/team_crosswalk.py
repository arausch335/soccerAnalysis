"""Refresh a compact team-only snapshot of Reep's published provider bridges.

Reep's full bridge register is CC0. The snapshot is keyed by a FotMob ID from
our fixtures; missing bridges remain blank and never become guessed IDs.
"""
from collections import defaultdict
import csv
import gzip
import io
import json
from pathlib import Path
import ssl
from urllib.request import Request, urlopen

import certifi

from storage.pandasStore import atomic_json


REEP_RELEASE = '20261005T180536Z'
REEP_BRIDGES_URL = (
    'https://data.reep.football/releases/' + REEP_RELEASE + '/csv/bridges.csv.gz')

# Manually verified against both providers' public team pages. These are
# separate from the Reep snapshot so a refresh cannot erase the review.
VERIFIED_TEAM_ID_OVERRIDES = {
    # Reep linked the FotMob and WhoScored game IDs; WhoScored team IDs below
    # were then read from /matches/<match-id>/live team headers and checked
    # against the fixture sides. Neither name alone nor an unmatched page won.
    '1954': {'whoscored_id':'2121'},  # match 1943941
    '2004': {'sofascore_id':'1836','whoscored_id':'2517'},  # match 1250853; sofascore.com/football/team/ostersunds-fk/1836
    '2169': {'sofascore_id':'4503'},  # sofascore.com/football/team/dukla-banska-bystrica/4503
    '2172': {'sofascore_id':'213357','whoscored_id':'1083'},  # match 1669288; sofascore.com/football/team/ns-mura/213357
    '4087': {'sofascore_id':'21800'},  # sofascore.com/football/team/thonon-evian-grand-geneve-fc/21800
    '4666': {'whoscored_id':'739'},  # match 1937147
    '6510': {'whoscored_id':'1497'},  # match 760914; old Padova profile
    '7852': {'sofascore_id':'4921','whoscored_id':'1260'},  # match 1924678; sofascore.com/football/team/the-new-saints/4921
    '7880': {'whoscored_id':'1356'},  # match 1492787
    '8016': {'whoscored_id':'711'},  # match 1821858
    '8188': {'whoscored_id':'808'},  # match 1995772
    '8551': {'whoscored_id':'773'},  # match 760922
    '8645': {'sofascore_id':'4','whoscored_id':'28'},  # match 958468; sofascore.com/football/team/milton-keynes-dons/4
    '8698': {'sofascore_id':'2321'},  # sofascore.com/football/team/zenit-st-petersburg/2321
    '9880': {'whoscored_id':'265'},  # match 1230999; old Cesena profile
    '9760': {'sofascore_id':'2325'},  # sofascore.com/football/team/cska-moscow/2325
    '10127': {'whoscored_id':'697'},  # match 460197
    '89022': {'whoscored_id':'4701'},  # match 1903710
    '189481': {'whoscored_id':'8183'},  # match 1755431
    '189641': {'whoscored_id':'8287'},  # match 988068
    '195601': {'whoscored_id':'8726'},  # match 1587059
    '210285': {'whoscored_id':'9460'},  # match 1379567
    '210853': {'whoscored_id':'9470'},  # match 1222740
    '357259': {'whoscored_id':'11917'},  # match 1994322
    '583944': {'whoscored_id':'19397'},  # match 2013704; current Calcio Padova profile
    '584022': {'whoscored_id':'19415'},  # match 2013720; current Cesena profile
    '654352': {'whoscored_id':'23552'},  # match 1489967
    '969268': {'sofascore_id':'168476'},  # sofascore.com/football/team/fc-kosice/168476
}


def refresh_team_crosswalk(fixtures_path,output_path,*,url=REEP_BRIDGES_URL):
    """Download one published release and retain only unambiguous fixture teams."""
    wanted=set()
    with Path(fixtures_path).open(newline='',encoding='utf-8') as stream:
        for fixture in csv.DictReader(stream):
            wanted.update((fixture['home_team_id'],fixture['away_team_id']))
    bridges=defaultdict(lambda:defaultdict(set))
    request=Request(url,headers={'User-Agent':'soccerAnalysis/1.0 (team identity audit)'})
    with urlopen(request,timeout=180,
                 context=ssl.create_default_context(cafile=certifi.where())) as response:
        with gzip.GzipFile(fileobj=response) as compressed:
            reader=csv.DictReader(io.TextIOWrapper(compressed,encoding='utf-8'))
            for bridge in reader:
                if bridge['namespace']=='team' and bridge['provider'] in (
                        'fotmob','sofascore','whoscored'):
                    bridges[bridge['reep_id']][bridge['provider']].add(
                        bridge['external_id'])
    fotmob_to_reep=defaultdict(set)
    for reep_id,providers in bridges.items():
        for fotmob_id in providers['fotmob'] & wanted:
            fotmob_to_reep[fotmob_id].add(reep_id)
    teams={}
    for fotmob_id in sorted(wanted):
        reep_ids=fotmob_to_reep[fotmob_id]
        if len(reep_ids)!=1:continue
        providers=bridges[next(iter(reep_ids))]
        teams[fotmob_id]={provider+'_id':next(iter(providers[provider]))
                          for provider in ('sofascore','whoscored')
                          if len(providers[provider])==1}
    atomic_json(Path(output_path),{'source':url,'teams':teams})
    return len(teams)
