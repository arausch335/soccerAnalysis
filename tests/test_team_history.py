import csv
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from providers.reep import Index,FORMAT,_digest,install
from providers.identity import teams_match
from providers.teamHistory import CATALOG,history_frame,export_history
from providers.teamProfiles import canonical_frame
from providers.teamScope import in_mens_scope


class TeamHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.output=self.root/'dataset'

    def test_relationship_types_dates_provenance_and_unresolved_ids_are_explicit(self):
        frame=history_frame(self.root)
        self.assertEqual(len(frame),25)
        self.assertEqual(frame.relationship_id.nunique(),len(frame))
        self.assertTrue(frame.gender.eq('male').all())
        self.assertFalse(frame.used_for_identity_matching.any())
        self.assertTrue(frame.status.eq('researched').all())
        self.assertTrue(frame.source_urls.map(lambda v:bool(json.loads(v))).all())
        years=frame[frame.date_precision=='year']
        self.assertTrue(years.effective_date.eq('').all())
        rename=frame[frame.event_id=='padova-name-2015'].iloc[0]
        self.assertEqual(rename.effective_date,'2015-07-06')
        self.assertEqual(rename.from_team_key,rename.to_team_key)
        merger=frame[frame.event_id=='bastia-borgo-merger-2017']
        self.assertEqual(merger.to_team_key.nunique(),1)
        self.assertEqual(merger.from_team_key.nunique(),2)
        self.assertFalse(frame.from_fotmob_id.isin(['6510','557395','1798961']).any())

    def test_export_is_separate_from_identity_tables_and_keeps_upstream_disagreements(self):
        rows=[['team','fotmob','8551','rtNew'],['team','whoscored','773','rtOld']]
        install(self.root,{'format':FORMAT,'version':_digest(rows),'rows':rows})
        path,detail=export_history(self.root,self.output)
        self.assertEqual(path,self.output/'concordance/team_history.csv')
        frame=pd.read_csv(path,dtype=str,keep_default_na=False)
        row=frame[frame.from_team_key=='siena-old'].iloc[0]
        self.assertEqual(row.from_reep_id,'')
        self.assertEqual(json.loads(row.from_reep_ids),['rtNew','rtOld'])
        self.assertEqual(detail['rows'],25)
        self.assertFalse((self.output/'fixtures.csv').exists())
        self.assertFalse((self.output/'concordance/team_ids.csv').exists())

    def test_splits_are_exportable_without_turning_into_identity_aliases(self):
        value=json.loads(CATALOG.read_text(encoding='utf-8'))
        original=value['relationships'][0]
        value['relationships']=[{**original,'id':'split-a','relationship':'split_into'},
            {**original,'id':'split-b','relationship':'split_into','to':'padova-2014'}]
        path=self.root/'history.json';path.write_text(json.dumps(value))
        frame=history_frame(self.root,catalog=path)
        self.assertEqual(frame.from_team_key.nunique(),1)
        self.assertEqual(frame.to_team_key.nunique(),2)
        self.assertFalse(frame.used_for_identity_matching.any())

    def test_gender_rejected_even_when_upstream_joins_women_to_a_mens_club(self):
        rows=[['team','sofascore','2722','rtVicenza'],['team','fotmob','1798961','rtVicenza'],
              ['team','fotmob','145007','rtVicenza'],['player','fotmob','1798961','rpPerson']]
        index=Index({'rows':rows})
        self.assertEqual(index.resolve('team',{'sofascore_id':'2722'})['fotmob_id'],'145007')
        self.assertFalse(index.ids('team','fotmob','1798961'))
        self.assertEqual(index.ids('player','fotmob','1798961'),{'rpPerson'})
        rejected=index.resolve('team',{'sofascore_id':'2722','fotmob_id':'1798961'})
        self.assertEqual((rejected['fotmob_id'],rejected['identity_status']),('','conflict'))
        self.assertFalse(teams_match({'id':2722,'name':'Vicenza'},
            {'id':1798961,'name':'Vicenza'},'fotmob',self.root))
        self.assertFalse(teams_match({'id':1,'name':'Club'},{'id':1,'name':'Club','gender':'F'},'fotmob',self.root,baseline='fotmob'))
        self.assertFalse(in_mens_scope('fotmob',1798961.0))
        self.assertFalse(in_mens_scope('fotmob',99,{'name':'Club (W)'}))
        self.assertEqual(index.resolve('team',{'sofascore_id':'2722','gender':'female'})['identity_status'],'excluded')

    def test_womens_team_causes_schedule_validation_failure(self):
        from fixtures.dataset import _validate
        from tests.test_fixture_dataset import event
        with self.assertRaisesRegex(ValueError,"Women.s team"):
            _validate([event(home=1798961,home_name='Vicenza')],'fotmob')

    def test_export_drops_womens_only_reference_but_keeps_mens_row_with_bad_peer_flagged(self):
        frame=pd.DataFrame([
            {'team_name':'Vicenza','sofascore_id':'2722','fotmob_id':'1798961','whoscored_id':pd.NA},
            {'team_name':'Vicenza (W)','sofascore_id':pd.NA,'fotmob_id':'1798961','whoscored_id':pd.NA}])
        cleaned=canonical_frame(frame,reference=True)
        self.assertEqual(len(cleaned),1)
        self.assertEqual(cleaned.iloc[0].fotmob_id,'')
        from dataset_builder import export_concordance
        paths=export_concordance({'team':frame},self.root,self.output)
        with paths['team concordance'].open() as stream:rows=list(csv.DictReader(stream))
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['fotmob_id'],rows[0]['identity_status']),('','conflict'))
        self.assertIn('team history concordance',paths)

    def test_fixture_build_and_reconciliation_recreate_the_history_export(self):
        from tests.test_fixture_dataset import FixtureDatasetTests
        from fixtures.identityRefresh import reconcile_exports
        case=FixtureDatasetTests();case.setUp();self.addCleanup(case.doCleanups)
        case.build()
        path=case.output.parent/'concordance/team_history.csv'
        self.assertEqual(len(pd.read_csv(path)),25)
        path.unlink()
        reconcile_exports(case.root,case.output.parent)
        self.assertEqual(len(pd.read_csv(path)),25)


class ReepManifestTests(unittest.TestCase):
    def test_v4_release_is_pinned_to_the_pointer_and_only_lists_required_files(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import Mock
        from providers.reep import release_sources
        manifest={'stamp':'2026-10-25.1','files':{name+'.csv.gz':{
            'key':'releases/2026-10-25.1/objects/bundle/'+name+'.csv.gz','sha256':'a'*64}
            for name in ('bridges','redirects')}}
        content=json.dumps(manifest).encode()
        pointer={'stamp':manifest['stamp'],'release':{'key':'releases/2026-10-25.1/release.json',
            'sha256':hashlib.sha256(content).hexdigest()}}
        def response(value,raw=b''):
            return SimpleNamespace(json=lambda:value,content=raw,raise_for_status=lambda:None)
        client=Mock();client.get.side_effect=[response(pointer),response(manifest,content)]
        release,base,checksums,urls=release_sources(client)
        self.assertEqual(release,'2026-10-25.1')
        self.assertEqual(set(urls),{'bridges','redirects'})
        self.assertEqual(client.get.call_count,2)
        self.assertEqual(checksums['csv/bridges.csv.gz'],'a'*64)
        client.get.side_effect=[response(pointer),response(manifest,b'changed')]
        with self.assertRaisesRegex(ValueError,'manifest checksum'):release_sources(client)

    def test_legacy_release_pointer_still_uses_legacy_checksums(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from providers.reep import release_sources
        client=Mock();client.get.side_effect=[SimpleNamespace(json=lambda:{'release_tag':'old'},raise_for_status=lambda:None),
            SimpleNamespace(text='a'*64+' csv/bridges.csv.gz',raise_for_status=lambda:None)]
        release,_,_,urls=release_sources(client)
        self.assertEqual(release,'old')
        self.assertTrue(urls['bridges'].endswith('/old/csv/bridges.csv.gz'))


if __name__=='__main__':unittest.main()
