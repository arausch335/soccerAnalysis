from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tests.legacy_collectors import WhoScoredEventRunner
from providers.httpClient import ProviderRequestError


class WhoScoredOnly:
    def __init__(self):
        self.calls = []
        self.block = False

    def seasons(self,path):
        self.calls.append(('seasons',path))
        return [{'name':'2024/2025','path':'season/1'}]

    def season_stages(self,path):
        return [11,12]

    def season_events(self,stage,month):
        if month != '202408':
            return []
        return [dict(id=stage,startTimeUtc='2024-08-17T11:30:00Z',
            homeTeamId=50,homeTeamName='Home',awayTeamId=60,awayTeamName='Away',
            homeScore=1,awayScore=0,status=6),
            dict(id=99,status=0)]

    def match_details(self,mid):
        self.calls.append(('match',mid))
        if self.block:
            raise ProviderRequestError('Forbidden',status_code=403)
        return {'match_id':mid,'matchCentreEventTypeJson':{},'matchCentreData':{
            'statusCode':6,'startDate':'2024-08-17',
            'home':{'teamId':50,'name':'Home','players':[{'playerId':70,'name':'One'}]},
            'away':{'teamId':60,'name':'Away','players':[]},
            'events':[{'id':1234567890,'eventId':7,'teamId':50,'playerId':70,
                       'relatedPlayerId':80,'relatedEventId':6,'x':20,'y':30,
                       'qualifiers':[{'type':{'value':140,'displayName':'PassEndX'},'value':'40'}],
                       'future':{'preserved':True}}]}}


def quiet(*args,**kwargs):
    pass
