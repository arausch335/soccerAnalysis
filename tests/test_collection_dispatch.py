import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
import main
import yaml
from contextlib import nullcontext


class CollectionDispatchTests(unittest.TestCase):
    def setUp(self):
        patcher=patch('main.ensemble_session',side_effect=lambda *a,**k:nullcontext())
        patcher.start();self.addCleanup(patcher.stop)

    def test_config_types_run_every_selected_season_through_main(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('main.COLLECTION_TYPES',['combined','whoscored_events','fotmob_players']), \
                patch('main.COMPETITIONS',['La Liga']),patch('main.SEASONS',['2023/24','2024/25']), \
                patch('main.collect',return_value=0) as normal, \
                patch('main.collect_whoscored',return_value=0) as who, \
                patch('main.collect_fotmob',return_value=0) as fotmob:
            self.assertEqual(main.main(['--data-root',folder,'--limit','3']),0)
            self.assertEqual((normal.call_count,who.call_count,fotmob.call_count),(2,2,2))
            for collect in (who,fotmob):
                self.assertIn('--limit',collect.call_args.args[0])
                self.assertIn('2024/2025',collect.call_args.args[0])

    def test_cli_type_and_selection_override_yaml(self):
        with tempfile.TemporaryDirectory() as folder,patch('main.collect_whoscored',return_value=0) as who, \
                patch('main.collect') as normal:
            main.main(['--collection-types','whoscored_events','--competition','Bundesliga',
                       '--season','2024/25','--data-root',folder,'--offline'])
            normal.assert_not_called()
            who.assert_called_once_with(['--competition','Bundesliga','--season','2024/2025',
                                         '--data-root',folder,'--offline'])

    def test_sofascore_type_forwards_provider_choice(self):
        with tempfile.TemporaryDirectory() as folder,patch('main.collect',return_value=0) as normal:
            main.main(['--collection-types','sofascore','--competition','La Liga',
                       '--season','2024/25','--data-root',folder])
            args=normal.call_args.args[0]
            self.assertEqual(args[args.index('--providers')+1],'sofascore')

    def test_status_reads_local_standalone_counts_without_calling_scraper(self):
        with tempfile.TemporaryDirectory() as folder,patch('main.collect_fotmob') as collect:
            directory=Path(folder)/'fotmob_player_stats'/'la-liga'/'2024-2025'
            directory.mkdir(parents=True)
            (directory/'schedule.json').write_text(json.dumps({'events':[{'id':1}]}))
            with patch('builtins.print') as report:
                main.main(['status','--collection-types','fotmob_players','--competition','La Liga',
                           '--season','2024/25','--data-root',folder])
            collect.assert_not_called()
            self.assertTrue(any('1 scheduled; 1 remaining' in str(c) for c in report.call_args_list))

    def test_export_standalone_forces_offline_and_disables_who_refresh(self):
        with tempfile.TemporaryDirectory() as folder,patch('main.collect_whoscored',return_value=0) as who:
            directory=Path(folder)/'whoscored_events'/'la-liga'/'2024-2025'
            directory.mkdir(parents=True);(directory/'job.json').write_text('{}')
            main.main(['export','--collection-types','whoscored_events','--competition','La Liga',
                       '--season','2024/25','--data-root',folder])
            self.assertIn('--offline',who.call_args.args[0])
            self.assertIn('--no-refresh-schedule',who.call_args.args[0])

    def test_invalid_mixed_options_fail_before_any_scraper_runs(self):
        with patch('main.collect') as normal,patch('main.collect_fotmob') as fotmob:
            for args in (['--collection-types','combined','sofascore'],
                         ['--collection-types','combined','fotmob_players','--limit','0'],
                         ['--collection-types','combined','fotmob_players','--name','test-2024-25']):
                with self.subTest(args=args),self.assertRaises(SystemExit):main.main(args)
            normal.assert_not_called();fotmob.assert_not_called()

    def test_changing_type_revisits_completed_saved_job_and_updates_providers(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('main.COMPETITIONS',['La Liga']),patch('main.SEASONS',['2024/25']), \
                patch('main.collect',return_value=0) as normal:
            directory=main._job_directory(folder,'combined','La Liga','2024/25')
            directory.mkdir(parents=True)
            (directory/'job.json').write_text(json.dumps({'providers':['sofascore'],'sofascore_spatial_capture':'fast'}))
            (directory/'schedule.json').write_text(json.dumps({'events':[{'id':1}]}))
            (directory/'collection.csv').write_text('match_id,status\n1,complete\n')
            self.assertEqual(main.main(['--data-root',folder]),0)
            args=normal.call_args.args[0]
            self.assertEqual(args[0],'resume')
            self.assertEqual(args[args.index('--providers')+1:],['sofascore','fotmob','whoscored'])

    def test_real_runners_share_progress_and_keep_source_outputs_separate(self):
        from types import SimpleNamespace
        from collection.progress import CollectionProgress
        from tests.test_collection_progress import Clock
        from tests.test_providers import Sofa, Fot
        from tests.test_whoscored_events import WhoScoredOnly
        clock=Clock();messages=[]
        progress=CollectionProgress(clock=clock,report=messages.append)
        sofa=Sofa();who=WhoScoredOnly();fot=Fot()
        for client,method,seconds in ((sofa,'lineups',30),(who,'match_details',60),(fot,'match_details',90)):
            client.network_requests=0
            client.request_gate=SimpleNamespace(is_blocked=False)
            original=getattr(client,method)
            def request(mid,original=original,client=client,seconds=seconds):
                clock.advance(seconds);client.network_requests+=1
                return original(mid)
            setattr(client,method,request)
        with tempfile.TemporaryDirectory() as folder, \
                patch('main.CollectionProgress',return_value=progress), \
                patch('collection.cli.create_sofascore_client') as sofa_context, \
                patch('collection.whoScoredEvents.SafeWhoScoredClient') as who_context, \
                patch('collection.fotmobOnly.SafeFotMobClient') as fot_context:
            sofa_context.return_value.__enter__.return_value=sofa
            who_context.return_value.__enter__.return_value=who
            fot_context.return_value.__enter__.return_value=fot
            self.assertEqual(main.main(['--collection-types','sofascore','whoscored_events','fotmob_players',
                '--competition','EPL','--season','2024/25','--data-root',folder,'--no-build-dataset']),0)
            self.assertEqual(len(progress.jobs),3)
            self.assertTrue(all(j.finished and not j.paused for j in progress.jobs.values()))
            self.assertEqual(progress.rate('whoscored_events','http'),(60,2,False))
            self.assertEqual(progress.rate('fotmob_players','http'),(90,3,False))
            self.assertTrue((Path(folder)/'fotmob_player_stats'/'premier-league'/'2024-2025'/'player_match_stats.csv').is_file())
            self.assertTrue((Path(folder)/'premier-league'/'2024-2025'/'collection.csv').is_file())
            self.assertEqual(progress.remaining(),(0,False))
            self.assertTrue(any('collection remaining' in message and '(provisional)' in message for message in messages))
            self.assertIn('7m 30s',messages[-1])


class CollectionConfigTests(unittest.TestCase):
    def load(self,document):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.yaml';path.write_text(yaml.safe_dump(document))
            return config.load_config(path)

    def document(self):return yaml.safe_load(config.CONFIG_FILE.read_text())

    def test_legacy_transport_and_whoscored_preferences_are_preserved(self):
        document=self.document();collection=document['collection']
        document['transport']={'sofascore_mode':collection.pop('sofascore_access'),
                               'http':collection.pop('http'),'browser':collection.pop('browser')}
        document['transport']['sofascore_mode']='browser'
        document['whoscored']=collection.pop('whoscored')
        document['whoscored']['stage_ids']={'La Liga 2023-2024':[123]}
        del collection['types'];del collection['show_timing']
        loaded=self.load(document)
        self.assertEqual(loaded['TRANSPORT']['sofascore_mode'],'browser')
        self.assertEqual(loaded['WHOSCORED_STAGE_IDS'],{'La Liga 2023-2024':[123]})
        self.assertEqual(loaded['COLLECTION_TYPES'],['combined']);self.assertTrue(loaded['SHOW_TIMING'])

    def test_invalid_types_and_timing_are_rejected(self):
        for value in ([],['unknown'],['combined','sofascore']):
            document=self.document();document['collection']['types']=value
            with self.subTest(value=value),self.assertRaises(config.ConfigError):self.load(document)
        document=self.document();document['collection']['show_timing']='true'
        with self.assertRaises(config.ConfigError):self.load(document)

    def test_conflicting_legacy_and_new_settings_are_rejected(self):
        document=self.document()
        document['transport']={'sofascore_mode':'http','http':{},'browser':{}}
        with self.assertRaisesRegex(config.ConfigError,'Both legacy'):self.load(document)


if __name__=='__main__':unittest.main()
