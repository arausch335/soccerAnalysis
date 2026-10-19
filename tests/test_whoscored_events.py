from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from collection.whoScoredEvents import WhoScoredEventRunner
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


class StandaloneEventsTests(unittest.TestCase):
    def test_discovers_multiple_stages_preserves_raw_payload_and_resumes_without_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = WhoScoredOnly()
            runner = WhoScoredEventRunner(client,tmp,quiet)
            self.assertEqual(runner.run('EPL','2024/25'),0)
            saved_paths = list((Path(tmp)/'raw'/'whoscored').glob('*.json'))
            self.assertEqual(len(saved_paths),2)
            for path in saved_paths:
                saved = json.loads(path.read_text())
                self.assertEqual(saved['provider'],'whoscored')
                mid = saved['payload']['match_id']
                self.assertEqual(saved['key'],f'match/{mid}')
                self.assertEqual(saved['payload'],client.match_details(mid))
                self.assertEqual(saved['payload']['matchCentreData']['events'][0]['eventId'],7)
            self.assertEqual(list(Path(tmp).glob('*.csv')),[])
            client.calls.clear()
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertEqual(client.calls,[])

    def test_refusal_stops_requests_and_resume_retries_failed_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = WhoScoredOnly()
            client.block = True
            runner = WhoScoredEventRunner(client,tmp,quiet)
            self.assertEqual(runner.run('EPL','2024/25'),2)
            self.assertEqual([c for c in client.calls if c[0]=='match'],[('match',11)])
            failures = json.loads((Path(tmp)/'whoscored_failures.json').read_text())
            self.assertEqual(set(failures),{'11'})
            self.assertEqual(list((Path(tmp)/'raw').glob('whoscored/*.json')),[])
            client.block = False
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertEqual(json.loads((Path(tmp)/'whoscored_failures.json').read_text()),{})

    def test_rejects_wrong_match_before_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = WhoScoredOnly()
            payload = client.match_details(999)
            client.match_details = lambda mid:deepcopy(payload)
            runner = WhoScoredEventRunner(client,tmp,quiet)
            self.assertEqual(runner.run('EPL','2024/25'),2)
            self.assertEqual(list((Path(tmp)/'raw').glob('whoscored/*.json')),[])

    def test_job_identity_and_explicit_schedule_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = WhoScoredOnly()
            runner = WhoScoredEventRunner(client,tmp,quiet)
            runner.run('EPL','2024/25',limit=1)
            with self.assertRaisesRegex(ValueError,'selection differs'):
                runner.run('Bundesliga','2024/25')
            client.calls.clear()
            self.assertEqual(runner.run('EPL','2024/25',refresh_schedule=True),0)
            self.assertTrue(any(c[0]=='seasons' for c in client.calls))
            self.assertFalse(client.refresh_cache)


if __name__ == '__main__':
    unittest.main()
