import json
import unittest

from api import SofaScoreLoader
from collection.whoScoredExports import EVENT_COLUMNS, event_rows
from test_providers import event as match_event, lineup


class WhoScoredExportTests(unittest.TestCase):
    def test_event_rows_preserve_timing_geometry_classifications_and_future_fields(self):
        loader=SofaScoreLoader(None)
        season=loader.select_season(17,'Premier League',61627,'24/25')
        match=loader._build_match(season,match_event(),lineup())
        match.metadata['provider_match_ids']={'whoscored':99}
        event={'id':123.0,'eventId':7,'minute':45,'second':12,'expandedMinute':48,
            'teamId':501,'playerId':77,'relatedPlayerId':88,'relatedEventId':6,
            'period':{'value':1,'displayName':'FirstHalf'},
            'type':{'value':1,'displayName':'Pass'},
            'outcomeType':{'value':1,'displayName':'Successful'},
            'x':40,'y':20,'endX':80,'endY':30,'isTouch':True,'futureField':{'x':1},
            'qualifiers':[
                {'type':{'value':140,'displayName':'PassEndX'},'value':'80'},
                {'type':{'value':141,'displayName':'PassEndY'},'value':'30'},
                {'type':{'value':212,'displayName':'Length'},'value':'42.5'},
                {'type':{'value':56,'displayName':'Zone'},'value':'Left'},
                {'type':{'value':20,'displayName':'RightFoot'}},
            ],'satisfiedEventsTypes':[91,30]}
        saved={'provider':'whoscored','key':'match/99','fetched_at':'now','payload':{
            'matchCentreEventTypeJson':{'touches':91,'passKey':30},
            'matchCentreData':{'events':[event],
                'home':{'teamId':501,'name':'Liverpool','players':[{'playerId':77,'name':'One'}]},
                'away':{'teamId':502,'name':'Other','players':[{'playerId':88,'name':'Two'}]}}}}
        rows=event_rows(match,saved)
        self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual(set(row),set(EVENT_COLUMNS))
        self.assertEqual(row['expanded_minute'],48)
        self.assertEqual((row['x'],row['y'],row['end_x'],row['end_y']),(40,20,80,30))
        self.assertEqual(row['player_name'],'One');self.assertEqual(row['related_player_name'],'Two')
        self.assertTrue(row['is_key_pass']);self.assertEqual(row['body_part'],'RightFoot')
        self.assertEqual(json.loads(row['satisfied_event_types']),['touches','passKey'])
        self.assertEqual(json.loads(row['extra_event_json']),{'futureField':{'x':1}})


if __name__=='__main__':unittest.main()
