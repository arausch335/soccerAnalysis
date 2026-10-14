import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from providers.team_crosswalk import refresh_team_crosswalk


class TeamCrosswalkTests(unittest.TestCase):
    def test_refresh_keeps_only_unique_fixture_team_bridges(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixtures=root/'fixtures.csv';bridges=root/'bridges.csv.gz'
            with fixtures.open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=(
                    'home_team_id','away_team_id'))
                writer.writeheader();writer.writerow({'home_team_id':'10',
                                                   'away_team_id':'20'})
            with gzip.open(bridges,'wt',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=(
                    'provider','namespace','external_id','reep_id'))
                writer.writeheader()
                for provider,external_id,reep_id in (
                        ('fotmob','10','team-a'),('sofascore','1','team-a'),
                        ('whoscored','100','team-a'),('fotmob','20','team-b'),
                        ('sofascore','2','team-b')):
                    writer.writerow({'provider':provider,'namespace':'team',
                                     'external_id':external_id,'reep_id':reep_id})
            output=root/'team_id_crosswalk.json'
            self.assertEqual(refresh_team_crosswalk(fixtures,output,
                                                     url=bridges.as_uri()),2)
            result=json.loads(output.read_text())['teams']
            self.assertEqual(result['10'],{'sofascore_id':'1',
                                           'whoscored_id':'100'})
            self.assertEqual(result['20'],{'sofascore_id':'2'})


if __name__=='__main__':
    unittest.main()
