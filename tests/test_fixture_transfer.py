from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import dataset_pipeline.build as dataset_builder
from collection.fixtureTransfer import export_bundle,verify,install_bundle,import_staged
from storage.pandasStore import atomic_json
from tests.test_providers import event


class FixtureTransferTests(unittest.TestCase):
    def test_historical_candidate_metadata_preserves_all_competing_ids(self):
        from fixtures.sharedIds import candidate_ids
        first=json.dumps({'fotmob_id':['1'],'sofascore_id':['5']})
        second=json.dumps({'fotmob_id':['2'],'sofascore_id':['5']})
        value=json.dumps([json.dumps([first,second]),first])
        self.assertEqual(candidate_ids(value),{'fotmob_id':['1','2'],'sofascore_id':['5'],'whoscored_id':[]})

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.mode=patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=False,
                                BUILD_TEAM_MATCH_STATS=False,BUILD_FIXTURES=True)
        self.mode.start();self.addCleanup(self.mode.stop)

    def build(self,name,ids):
        root=self.base/name/'data';dataset=self.base/name/'dataset'
        source=root/'premier-league/2024-2025'
        settings={'competition':'Premier League','season':'2024/2025','providers':['sofascore']}
        atomic_json(source/'config.json',settings);atomic_json(source/'job.json',settings)
        atomic_json(source/'schedule.json',{'events':[event(mid) for mid in ids]})
        self.assertEqual(dataset_builder.build_dataset(root,dataset),0)
        return root,dataset

    def test_full_baseline_survives_incremental_packaging_and_csv_to_parquet(self):
        with patch.object(dataset_builder.config,'CONCORDANCE_FORMAT','csv'):
            _,sender=self.build('main',[101,102])
        root,receiver=self.build('msi',[201])
        bundle=self.base/'ensemble/full-history'
        self.assertEqual(export_bundle(sender,bundle),2)
        self.assertTrue(import_staged(bundle.parent,receiver))
        self.assertFalse(import_staged(bundle.parent,receiver))
        self.assertTrue(bundle.exists())
        self.assertEqual(dataset_builder.build_dataset(root,receiver),0)
        from concordance.publication import validate,folder,wide_rows
        self.assertEqual(validate(folder(receiver))['format'],'soccer-concordance-parquet-v2')
        self.assertEqual({r['sofascore_match_id'] for r in wide_rows(receiver)},{'101','102','201'})
        self.assertEqual(dataset_builder.build_dataset(root,receiver),0)
        self.assertEqual(validate(folder(receiver))['rows'],3)

    def test_damaged_or_partial_transfer_never_installs(self):
        _,sender=self.build('main',[101])
        bundle=self.base/'bundle';export_bundle(sender,bundle)
        (bundle/'concordance/match_ids/part-00000.parquet').write_bytes(b'broken')
        output=self.base/'receiver'
        with self.assertRaisesRegex(ValueError,'checksum'):install_bundle(bundle,output)
        self.assertFalse(output.exists())

    def test_extra_files_and_symlinks_are_rejected(self):
        _,sender=self.build('main',[101]);bundle=self.base/'bundle';export_bundle(sender,bundle)
        (bundle/'settings.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'extra files'):verify(bundle)
        (bundle/'settings.json').unlink();p=bundle/'concordance/match_ids/part-00000.parquet';p.unlink()
        p.symlink_to(sender/'concordance/match_ids/part-00000.parquet')
        with self.assertRaisesRegex(ValueError,'symbolic'):verify(bundle)
