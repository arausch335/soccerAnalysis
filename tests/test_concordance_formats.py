"""Format changes republish typed concordances while retaining incremental work."""
import json
from pathlib import Path
import tempfile
import yaml
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import config
import dataset_builder
from concordance import publication
from tests import test_package_modes, test_parquet_concordances, test_msi_incremental_builds


class ConcordanceFormatTests(unittest.TestCase):
    def fixture(self, cls):
        helper=cls();helper.setUp();self.addCleanup(helper.doCleanups)
        return helper

    def test_both_formats_preserve_types_ids_and_private_inputs(self):
        helper=self.fixture(test_parquet_concordances.ParquetTests)
        private=helper.dataset/'concordance/metadata/compiled/fixtures.csv'
        private.parent.mkdir(parents=True);private.write_text('private linker input\n')
        frames=[]
        for encoding in ('parquet','csv','parquet'):
            with self.subTest(encoding=encoding):
                manifest=helper.write(data_format=encoding)
                base=helper.dataset/'concordance';publication.validate(base)
                self.assertEqual(manifest['data_format'],encoding)
                self.assertEqual({p.suffix for p in base.glob('*.*')},{'.'+encoding})
                self.assertFalse(any((base/publication.table_path(name,other)).exists()
                    for other in publication.FORMATS if other!=encoding for name in publication.TABLES))
                self.assertEqual(private.read_text(),'private linker input\n')
                frame=publication.read_table(base,'fixtures/fixtures');frames.append(frame)
                self.assertIsInstance(frame.match_entity_id.iloc[0],str)
                self.assertTrue(pd.isna(frame.home_score.iloc[2]))
                self.assertEqual(str(frame.kickoff_utc.dt.tz),'UTC')
                if encoding=='parquet':
                    schema=pq.read_schema(base/'fixtures.parquet')
                    self.assertEqual(schema.field('date').type,pa.date32())
                    self.assertEqual(schema.field('awarded').type,pa.bool_())
                    self.assertTrue(pa.types.is_integer(schema.field('home_score').type))
        for frame in frames[1:]:pd.testing.assert_frame_equal(frames[0],frame)

    def test_late_parquet_failure_keeps_previous_hardlinked_metadata_intact(self):
        helper=self.fixture(test_parquet_concordances.ParquetTests)
        helper.write();base=helper.dataset/'concordance'
        before={p:publication.digest(p) for p in publication.publication_files(base)}
        original=pq.write_table;count=0
        def fail_later(*args,**kwargs):
            nonlocal count
            count+=1
            if count==5:raise OSError('interrupted after metadata writes')
            return original(*args,**kwargs)
        with patch('concordance.publication.pq.write_table',side_effect=fail_later):
            with self.assertRaises(OSError):helper.write()
        self.assertEqual({p:publication.digest(p) for p in before},before)
        publication.validate(base)

    def test_incremental_format_switch_skips_raw_exports_and_id_scans(self):
        helper=self.fixture(test_package_modes.PackageModeTests)
        self.assertEqual(dataset_builder.package_dataset(helper.root,helper.output,concordance_format='csv'),0)
        raw={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (helper.output/'raw_data').rglob('*.csv')}
        private=helper.output/'concordance/metadata/compiled/fixtures.csv'
        before=(private.read_bytes(),private.stat().st_mtime_ns)
        with patch('dataset_builder.package_collection',side_effect=AssertionError('raw export')), \
             patch('dataset_builder._build_reference_csvs',side_effect=AssertionError('ID scan')), \
             patch('dataset_builder._seed_fixture_catalog',side_effect=AssertionError('fixture rebuild')):
            for encoding in ('parquet','csv','parquet'):
                self.assertEqual(dataset_builder.package_dataset(helper.root,helper.output,concordance_format=encoding),0)
                self.assertEqual(json.loads(helper.state.read_text())['concordance_format'],encoding)
                self.assertEqual(publication.validate(helper.output/'concordance')['format'],publication.FORMATS[encoding])
                with patch('concordance.publication.refresh',side_effect=AssertionError('unchanged publication')):
                    self.assertEqual(dataset_builder.package_dataset(helper.root,helper.output,concordance_format=encoding),0)
        self.assertEqual({p:(p.read_bytes(),p.stat().st_mtime_ns) for p in raw},raw)
        self.assertEqual((private.read_bytes(),private.stat().st_mtime_ns),before)

    def test_missing_supporting_table_can_be_repaired_in_selected_format(self):
        helper=self.fixture(test_package_modes.PackageModeTests)
        self.assertEqual(dataset_builder.package_dataset(helper.root,helper.output,concordance_format='csv'),0)
        (helper.output/'concordance/metadata/fixture_extra.csv').unlink()
        with patch('dataset_builder.package_collection',side_effect=AssertionError('raw export')), \
             patch('dataset_builder._build_reference_csvs',side_effect=AssertionError('ID scan')):
            self.assertEqual(dataset_builder.package_dataset(helper.root,helper.output,concordance_format='csv'),0)
        self.assertEqual(publication.validate(helper.output/'concordance')['format'],publication.FORMATS['csv'])

    def test_background_format_switch_updates_transfer_without_rebuilding_raw(self):
        helper=self.fixture(test_msi_incremental_builds.MSIIncrementalBuildTests)
        helper.add_season('premier-league-2024-25','Premier League',101)
        with helper.fixture_mode():
            with patch.object(config,'CONCORDANCE_FORMAT','csv'):
                helper.archive.schedule_build('premier-league/2024-2025')
                self.assertTrue(helper.agent.maintenance_once())
            with patch.object(config,'CONCORDANCE_FORMAT','parquet'), \
                 patch('dataset_builder.package_collection',side_effect=AssertionError('raw export')), \
                 patch('dataset_builder._build_reference_csvs',side_effect=AssertionError('ID scan')):
                helper.archive.schedule_build('premier-league/2024-2025')
                self.assertTrue(helper.agent.maintenance_once())
                paths={item['path'] for item in helper.archive.publication()['files']}
                self.assertIn('concordance/fixtures.parquet',paths)
                self.assertNotIn('concordance/fixtures.csv',paths)
                self.assertIn('raw_data/premier-league/2024-2025/fixtures.csv',paths)
                self.assertFalse(any('compiled/' in path for path in paths))
                publication.validate(helper.output/'concordance')
                with patch.object(helper.archive,'publish',side_effect=AssertionError('unchanged publication')):
                    helper.archive.schedule_build('premier-league/2024-2025')
                    self.assertTrue(helper.agent.maintenance_once())

    def test_config_defaults_and_format_changes_leave_raw_recipe_unchanged(self):
        from concordance.package_state import recipe
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'config').mkdir()
            (root/'dataset_builder.py').write_text('# recipe fixture\n')
            path=root/'config/settings.yaml'
            document=yaml.safe_load(config.CONFIG_FILE.read_text())
            document['dataset'].pop('concordance_format',None)
            path.write_text(yaml.safe_dump(document))
            self.assertEqual(config.load_config(path,project_root=root)['CONCORDANCE_FORMAT'],'parquet')
            with patch.object(config,'PROJECT_ROOT',root):
                before=recipe()
                document['dataset']['concordance_format']='csv'
                path.write_text(yaml.safe_dump(document))
                self.assertEqual(config.load_config(path,project_root=root)['CONCORDANCE_FORMAT'],'csv')
                self.assertEqual(recipe(),before)
                document['dataset']['tables']['fixtures']=not document['dataset']['tables']['fixtures']
                path.write_text(yaml.safe_dump(document))
                self.assertNotEqual(recipe(),before)
            document['dataset']['concordance_format']='json'
            path.write_text(yaml.safe_dump(document))
            with self.assertRaisesRegex(config.ConfigError,'concordance_format'):
                config.load_config(path,project_root=root)

    def test_cli_forwards_format_and_rejects_both_flags(self):
        with patch('dataset_builder.build_dataset',return_value=0) as build:
            for flags,expected in (([],None),(['--csv'],'csv'),(['--parquet'],'parquet')):
                self.assertEqual(dataset_builder.main(flags),0)
                self.assertEqual(build.call_args.kwargs['concordance_format'],expected)
        with self.assertRaises(SystemExit):dataset_builder.main(['--csv','--parquet'])


if __name__=='__main__':unittest.main()
