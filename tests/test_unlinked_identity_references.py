import subprocess
import shutil
import unittest
from unittest.mock import patch

import pandas as pd

from concordance.publication import identities,read_table,validate
from dataset_review import create_review,print_review,load_review
from dataset_handoff import create,verify
from tests import test_parquet_concordances as fixtures


class UnlinkedReferenceTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ParquetTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.registry=self.case.registry

    def orphan(self,kind='team'):
        row={'fotmob_id':'99999',kind+'_name':'Old unlinked reference'}
        row.update(self.registry.assign(kind,row))
        self.registry.withhold(kind,'fotmob','99999','reviewed_rejection')
        return row

    def test_accepted_registry_aliases_recover_all_three_identity_kinds(self):
        for kind in ('match','team','player'):
            with self.subTest(kind=kind):
                identity=self.registry.assign(kind,{'sofascore_id':'98765','fotmob_id':'87654'})
                row={**identity,'sofascore_id':'wrong-owner','sofascore_name':'Name'}
                ids,details=identities([row],kind,registry=self.registry)
                self.assertEqual(ids[0]['sofascore_id'],'98765')
                self.assertEqual(ids[0]['fotmob_id'],'87654')
                self.assertEqual(ids[0]['entity_id'],identity['entity_id'])

    def test_unused_orphans_publish_with_full_audit_in_both_formats(self):
        for encoding in ('parquet','csv'):
            with self.subTest(encoding=encoding):
                orphan=self.orphan()
                manifest=self.case.write(teams=[orphan],data_format=encoding)
                validate(self.case.dataset/'concordance')
                self.assertNotIn(orphan['entity_id'],set(read_table(self.case.dataset/'concordance','team_ids').entity_id))
                audit=manifest['unlinked_identity_references']
                self.assertEqual(audit['quarantined_count'],1)
                self.assertEqual(audit['quarantined'][0]['references'],[orphan])
                self.assertNotIn('team|fotmob|99999',self.registry.value['aliases'])

    def test_orphan_match_and_player_references_are_also_audited(self):
        match=self.orphan('match');player=self.orphan('player')
        manifest=self.case.write(matches=[match],players=[player])
        self.assertEqual(manifest['unlinked_identity_references']['quarantined_count'],2)
        self.assertEqual({r['kind'] for r in manifest['unlinked_identity_references']['quarantined']},{'match','player'})

    def test_reassigned_native_id_never_redirects_the_old_entity(self):
        old={'fotmob_id':'99999'};old.update(self.registry.assign('team',old))
        new=self.registry.assign('team',{'sofascore_id':'98765'})
        self.registry.value['aliases']['team|fotmob|99999']=new['alex_id']
        manifest=self.case.write(teams=[old,{'entity_id':new['entity_id'],'alex_id':new['alex_id']}])
        self.assertEqual(manifest['unlinked_identity_references']['quarantined_count'],1)
        ids=read_table(self.case.dataset/'concordance','team_ids').fillna('').set_index('entity_id')
        self.assertNotIn(old['entity_id'],ids.index)
        self.assertEqual(ids.loc[new['entity_id'],'fotmob_id'],'99999')
        self.assertEqual(self.registry.value['redirects'],{})

    def test_fixture_team_without_accepted_alias_stops_with_specific_id(self):
        self.case.write();marker=self.case.dataset/'concordance/metadata/manifest.json';before=marker.read_bytes()
        entity=self.case.rows[0]['home_entity_id']
        for provider,native in [('sofascore','1'),('fotmob','11'),('whoscored','111')]:
            self.registry.withhold('team',provider,native,'reviewed_rejection')
        with self.assertRaisesRegex(ValueError,'team_ids.*'+entity+'.*fixtures'):
            self.case.write()
        self.assertEqual(marker.read_bytes(),before);validate(self.case.dataset/'concordance')

    def test_exported_canonical_references_prevent_quarantine_for_every_kind(self):
        self.case.write();marker=self.case.dataset/'concordance/metadata/manifest.json';before=marker.read_bytes()
        target=self.case.dataset/'raw_data/test/2024-2025';target.mkdir(parents=True)
        path=target/'player_match_stats.csv'
        for kind,column,argument in [('team','opponent_entity_id','teams'),('player','entity_player_id','players'),('match','match_entity_id','matches')]:
            with self.subTest(kind=kind):
                orphan=self.orphan(kind)
                pd.DataFrame([{'match_id':'100',column:orphan['entity_id']}]).to_csv(path,index=False)
                with self.assertRaisesRegex(ValueError,kind+'_ids.*'+orphan['entity_id']+'.*player_match_stats.csv'):
                    self.case.write(**{argument:[orphan]})
                self.assertEqual(marker.read_bytes(),before)

    def test_relationship_reference_prevents_quarantine(self):
        orphan=self.orphan('player')
        with self.assertRaisesRegex(ValueError,'player_ids.*'+orphan['entity_id']+'.*entities'):
            self.case.write(players=[orphan],entities=[{'from_entity_id':orphan['entity_id']}])

    def test_normal_publication_does_not_scan_exported_rows_for_orphans(self):
        with patch('concordance.identityReferences.pd.read_csv',side_effect=AssertionError('unnecessary CSV scan')):
            self.case.write()

    def test_audit_and_copied_dataset_load_in_r(self):
        orphan=self.orphan();self.case.write(teams=[orphan])
        review=create_review(self.case.dataset);messages=[]
        print_review(review,report=lambda message,**kw:messages.append(message))
        self.assertEqual(review['quality']['unlinked_identity_references']['quarantined_count'],1)
        self.assertTrue(any('unused identity references' in message for message in messages))
        create(self.case.dataset);verify(self.case.dataset)
        if not shutil.which('Rscript'):return
        script="root=commandArgs(TRUE)[1];source(file.path(root,'read_dataset.R'));ds=suppressWarnings(read_dataset(root));stopifnot(nrow(ds$concordance$team_ids)==2)"
        result=subprocess.run(['Rscript','--vanilla','-e',script,str(self.case.dataset)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_reference_only_repair_preserves_audit_and_allows_approved_restoration(self):
        from collection.datasetPackaging import package_saved
        row={'fotmob_id':'99999','team_name':'Old reference'}
        row.update(self.registry.assign('team',row))
        self.case.write(teams=[row])
        self.registry.withhold('team','fotmob','99999','reviewed_rejection');self.registry.save()
        with patch('dataset_builder.package_collection',side_effect=AssertionError('season export during reference repair')):
            for index in range(2):
                # Force a real refresh, rather than the unchanged-package shortcut.
                self.registry.value.setdefault('withheld',{})[f'team|whoscored|{80000+index}']='unrelated_review'
                self.registry.save()
                self.assertEqual(package_saved(self.case.root,self.case.dataset,references_only=True,report=lambda *a,**k:None),0)
                review=load_review(self.case.dataset)
                self.assertEqual(review['quality']['unlinked_identity_references']['quarantined_count'],1)
                verify(self.case.dataset)
            self.registry.value['withheld'].pop('team|fotmob|99999')
            self.registry.value['aliases']['team|fotmob|99999']=row['alex_id'];self.registry.save()
            self.assertEqual(package_saved(self.case.root,self.case.dataset,references_only=True,report=lambda *a,**k:None),0)
        self.assertIn(row['entity_id'],set(read_table(self.case.dataset/'concordance','team_ids').entity_id))
        self.assertEqual(load_review(self.case.dataset)['quality']['unlinked_identity_references']['quarantined_count'],0)
        verify(self.case.dataset)
