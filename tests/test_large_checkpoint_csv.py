"""Large provider JSON cells are supported; parse failures stay season-scoped."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collection.seasonRunner import SeasonRunner
from dataset_review import load_review
from tests.test_providers import Sofa,Fot
from tests import test_package_modes as fixtures
import dataset_builder


class LargeCheckpointCSVTests(unittest.TestCase):
    def test_storage_initialization_raises_default_limit_with_windows_compatible_value(self):
        program="""import csv,json
csv.field_size_limit(131072)
import storage
value='x'*300000
row=next(csv.reader([value]))
print(json.dumps({'limit':csv.field_size_limit(),'length':len(row[0])}))
"""
        result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['limit'],2**31-1);self.assertEqual(value['length'],300000)

    def test_compact_index_reads_large_match_metadata_and_player_json_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'premier-league/2024-2025'
            runner=SeasonRunner({'sofascore':Sofa(),'fotmob':Fot()},source,
                                providers=['sofascore','fotmob'],spatial=(),report=lambda _:None)
            runner.run('EPL','2024',limit=1,build_csvs=False)
            marker=next((source/'match_checkpoints').glob('*/CURRENT.json'))
            folder=marker.parent/'snapshots'/json.loads(marker.read_text())['generation']
            before={}
            for name,field in [('matches.csv','metadata'),('performances.csv','raw_stats')]:
                path=folder/name
                with path.open(encoding='utf-8',newline='') as stream:
                    reader=csv.DictReader(stream);columns=reader.fieldnames;rows=list(reader)
                value=json.loads(rows[0][field]);value['_large_provider_metadata']='x'*300000
                rows[0][field]=json.dumps(value)
                with path.open('w',encoding='utf-8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(rows)
                before[path]=path.read_bytes()
            program="""import csv,sys,json
csv.field_size_limit(131072)
import dataset_builder
players,teams,matches={},{},{}
dataset_builder._source_id_rows(__import__('pathlib').Path(sys.argv[1]),players,teams,matches)
assert {r['player_name'] for r in players.values()}=={'Player One','Player Two'}
assert all(r['_ids']['fotmob_id'] for r in players.values())
print(json.dumps({'players':len(players),'teams':len(teams),'matches':len(matches)}))
"""
            result=subprocess.run([sys.executable,'-c',program,str(source)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout),{'players':2,'teams':2,'matches':2})
            self.assertEqual(before,{path:path.read_bytes() for path in before})

    def test_csv_error_is_diagnosed_and_other_season_publishes_then_retry_uses_cache(self):
        helper=fixtures.PackageModeTests();helper.setUp();self.addCleanup(helper.doCleanups)
        original=dataset_builder._source_id_rows
        def fail_one(source,*args):
            if source.parent.name=='bundesliga':raise csv.Error('invalid checkpoint CSV')
            return original(source,*args)
        with patch('dataset_builder._source_id_rows',side_effect=fail_one):self.assertEqual(helper.build(),0)
        report=load_review(helper.output)['package_diagnostic']
        self.assertEqual(report['status'],'partial');self.assertEqual(len(report['completed']),1)
        self.assertEqual(report['failed'][0]['phase'],'ID indexing')
        self.assertEqual(report['failed'][0]['error'],'invalid checkpoint CSV')
        with patch('dataset_builder._source_id_rows',wraps=original) as indexed,patch('dataset_builder.package_collection',side_effect=AssertionError('repeated completed export')):
            self.assertEqual(helper.build(),0)
        self.assertEqual([call.args[0].parent.name for call in indexed.call_args_list],['bundesliga'])
        self.assertEqual(load_review(helper.output)['package_diagnostic']['failed'],[])
