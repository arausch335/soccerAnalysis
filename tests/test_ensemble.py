import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import main
import config
from collection.ensemble import ensemble_session, import_pending
from collection.ensembleProgress import PortableProgress
from collection.seasonRunner import SeasonRunner
from collection.whoScoredEvents import WhoScoredEventRunner
from collection.fotmobOnly import FotMobOnlyRunner
from providers.extensionBridge import acquire_bridge
from storage.pandasStore import PandasStore, atomic_json
from review_data import build_report
from tests.test_providers import Sofa, Fot, lineup
from tests.test_whoscored_events import WhoScoredOnly


def quiet(*args,**kwargs):pass


def populate(root, *, limit=None, extra=False):
    root=Path(root);atomic_json(root/'.soccer-scraper.json',{'format':1})
    directory=root/'premier-league'/'2024-2025'
    sofa=Sofa()
    if extra:
        def detailed(mid):
            result=lineup();result['home']['players'][0]['statistics']['expectedGoals']=.2
            return result
        sofa.lineups=detailed
    SeasonRunner({'sofascore':sofa},directory,providers=['sofascore'],spatial=(),report=quiet).run(
        'EPL','2024/25',limit=limit,build_csvs=False)
    atomic_json(directory/'job.json',{'competition':'Premier League','season':'2024/2025',
        'providers':['sofascore'],'spatial':[],'sofascore_spatial_capture':'none'})
    WhoScoredEventRunner(WhoScoredOnly(),root/'whoscored_events'/'premier-league'/'2024-2025',quiet).run(
        'EPL','2024/25',limit=limit)
    FotMobOnlyRunner(Fot(),root/'fotmob_player_stats'/'premier-league'/'2024-2025',quiet).run(
        'EPL','2024/25',limit=limit)


class EnsembleTests(unittest.TestCase):
    def test_main_imports_all_providers_merges_checkpoints_and_review_sees_data(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);live=base/'data';ensemble=base/'ensemble_data';source=ensemble/'laptop'/'data'
            populate(live,limit=1);populate(source,extra=True)
            (live/'notes.txt').write_text('local notes')
            (source/'notes.txt').write_text('incoming notes')
            atomic_json(live/'safety'/'sofascore.json',{'status_code':429,'blocked_until':999})
            atomic_json(source/'safety'/'sofascore.json',{'status_code':403})
            store=PandasStore(live/'premier-league'/'2024-2025'/'match_checkpoints'/'1')
            dataset=store.from_csv();dataset.matches[1].metadata['local_provenance']={'kept':True};store.to_csv(dataset)
            incoming=PandasStore(source/'premier-league'/'2024-2025'/'match_checkpoints'/'1')
            data=incoming.from_csv();data.matches[1].metadata['raw_response_files']={'sample':r'raw\sofascore\sample.json'}
            incoming.to_csv(data)
            self.assertEqual(main.main(['--data-root',str(live),'--ensemble-root',str(ensemble),'--import-only']),0)
            self.assertFalse(source.exists())
            match=store.from_csv().matches[1]
            self.assertTrue(match.metadata['local_provenance']['kept'])
            self.assertEqual(match.home_lineup.players[0].stats.raw['expectedGoals'],.2)
            self.assertEqual(match.metadata['raw_response_files']['sample'],'raw/sofascore/sample.json')
            frame=build_report(live,['EPL'],['2024/25'])
            self.assertEqual(dict(zip(frame.provider,frame.complete)),{'sofascore':2,'whoscored':2,'fotmob':3})
            self.assertEqual(set(frame.percent),{100.0})
            self.assertEqual(json.loads((live/'safety'/'sofascore.json').read_text())['status_code'],429)
            self.assertEqual((live/'notes.txt').read_text(),'local notes')
            archive=next((live/'ensemble_imports').iterdir())
            self.assertEqual((archive/'conflicts'/'notes.txt').read_text(),'incoming notes')
            self.assertEqual(json.loads((archive/'import.json').read_text())['state'],'complete')
            portable=json.loads((ensemble/'provenance.json').read_text())
            self.assertEqual({r['namespace'] for r in portable['records'].values()},
                {'sofascore','whoscored_events','fotmob_players'})

    def test_portable_file_skips_remote_matches_without_claiming_local_completeness(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);first=base/'first';populate(first,limit=1)
            transfer=base/'transfer';transfer.mkdir()
            progress=PortableProgress(transfer/'provenance.json',first);progress.scan()
            second=base/'second';staging=base/'ensemble';staging.mkdir()
            shutil.copyfile(transfer/'provenance.json',staging/'provenance.json')
            with ensemble_session(second,staging,quiet):
                report=build_report(second,['EPL'],['2024/25'],staging/'provenance.json')
                self.assertEqual(set(report.complete),{0})
                self.assertTrue(report.percent.isna().all())
                self.assertEqual(set(report.portable_elsewhere),{1})
                sofa=Sofa()
                result=SeasonRunner({'sofascore':sofa},second/'premier-league'/'2024-2025',
                    providers=['sofascore'],spatial=(),report=quiet).run('EPL','2024/25',build_csvs=False)
                self.assertEqual(result.status.tolist(),['elsewhere','complete'])
                self.assertEqual(sofa.calls,[2])
                who=WhoScoredOnly()
                self.assertEqual(WhoScoredEventRunner(who,second/'whoscored_events'/'premier-league'/'2024-2025',quiet).run('EPL','2024/25'),0)
                self.assertEqual([c for c in who.calls if c[0]=='match'],[('match',12)])
                fot=Fot()
                self.assertEqual(FotMobOnlyRunner(fot,second/'fotmob_player_stats'/'premier-league'/'2024-2025',quiet).run('EPL','2024/25'),0)
                self.assertEqual(fot.calls,[102,103])
            report=build_report(second,['EPL'],['2024/25'],staging/'provenance.json')
            self.assertEqual(dict(zip(report.provider,report.complete)),{'sofascore':1,'whoscored':1,'fotmob':2})
            self.assertEqual(set(report.portable_elsewhere),{1})

    def test_failed_copy_retains_source_and_retry_is_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';ensemble=base/'ensemble';source=ensemble/'data'
            atomic_json(source/'.soccer-scraper.json',{'format':1})
            atomic_json(source/'evidence.json',{'provider':'whoscored','kept':'all'})
            with patch('collection.ensemble.atomic_copy',side_effect=OSError('Disk interrupted')):
                with self.assertRaises(OSError):import_pending(root,ensemble,quiet)
            self.assertTrue(source.exists())
            import_pending(root,ensemble,quiet)
            self.assertFalse(source.exists())
            self.assertEqual(json.loads((root/'evidence.json').read_text())['kept'],'all')
            # A repeated transfer must restore missing files even if its journal
            # says an earlier import of identical bytes already completed.
            atomic_json(source/'.soccer-scraper.json',{'format':1})
            atomic_json(source/'evidence.json',{'provider':'whoscored','kept':'all'})
            (root/'evidence.json').unlink()
            import_pending(root,ensemble,quiet)
            self.assertTrue((root/'evidence.json').exists())

    def test_invalid_import_does_not_delete_source_or_overwrite_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';ensemble=base/'ensemble';source=ensemble/'data'
            atomic_json(root/'.soccer-scraper.json',{'format':1});atomic_json(root/'original.json',{'value':'local'})
            atomic_json(source/'.soccer-scraper.json',{'format':1})
            (source/'broken.json').write_text('{broken')
            with self.assertRaises(ValueError):import_pending(root,ensemble,quiet)
            self.assertTrue(source.exists())
            self.assertEqual(json.loads((root/'original.json').read_text())['value'],'local')

    def test_mismatched_provider_selection_is_rejected_before_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';ensemble=base/'ensemble';source=ensemble/'data'
            for directory in (root,source):
                atomic_json(directory/'.soccer-scraper.json',{'format':1})
            atomic_json(root/'job.json',{'provider':'sofascore'})
            atomic_json(source/'job.json',{'provider':'fotmob'})
            with self.assertRaisesRegex(ValueError,'identity differs'):import_pending(root,ensemble,quiet)
            self.assertTrue(source.exists())
            self.assertEqual(json.loads((root/'job.json').read_text())['provider'],'sofascore')

    def test_extension_batch_uses_all_config_combinations_and_one_connection(self):
        self.assertEqual(config.TRANSPORT['sofascore_mode'],'extension')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);connections=[]
            def collect(args):
                connections.append(acquire_bridge(report=quiet)[0]);return 0
            with patch('main.COMPETITIONS',['Premier League','La Liga']),patch('main.SEASONS',['2023/24','2024/25']), \
                    patch('main.collect',side_effect=collect) as scrape:
                self.assertEqual(main.main(['--collection-types','sofascore','--data-root',str(base/'data'),
                    '--ensemble-root',str(base/'ensemble')]),0)
            self.assertEqual(scrape.call_count,4)
            self.assertEqual(len({id(connection) for connection in connections}),1)

    def test_invalid_provider_raw_payload_is_retained_for_review(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);source=base/'ensemble'/'data';populate(source,limit=1)
            raw=next((source/'whoscored_events'/'premier-league'/'2024-2025'/'raw'/'whoscored').glob('*.json'))
            saved=json.loads(raw.read_text());saved['payload']['match_id']=999;atomic_json(raw,saved)
            with self.assertRaisesRegex(ValueError,'Invalid WhoScored'):
                import_pending(base/'data',base/'ensemble',quiet)
            self.assertTrue(source.exists())
            self.assertFalse((base/'data'/'premier-league').exists())

    def test_ignore_provenance_collects_remote_only_work_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);source=base/'first';populate(source,limit=1)
            staging=base/'ensemble';staging.mkdir()
            progress=PortableProgress(staging/'provenance.json',source);progress.scan()
            target=base/'second';sofa=Sofa()
            with ensemble_session(target,staging,quiet,skip_remote=False):
                result=SeasonRunner({'sofascore':sofa},target/'premier-league'/'2024-2025',providers=['sofascore'],
                    spatial=(),report=quiet).run('EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(sofa.calls,[1]);self.assertEqual(result.status.tolist(),['complete'])

    def test_prior_import_archives_are_preserved_as_provenance_not_active_checkpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);source=base/'ensemble'/'data'
            atomic_json(source/'.soccer-scraper.json',{'format':1})
            relative=Path('ensemble_imports/older/source_checkpoints/CURRENT.json')
            atomic_json(source/relative,{'legacy_archive':True})
            import_pending(base/'data',base/'ensemble',quiet)
            self.assertEqual(json.loads((base/'data'/relative).read_text()),{'legacy_archive':True})


if __name__=='__main__':unittest.main()
