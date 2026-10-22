from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from providers import SofaScoreLoader
from providers.whoScoredClient import WhoScoredClient
from collection.selection import competition_by_name,in_competition
from collection.seasonRunner import SeasonRunner
from collection.ensembleProgress import PortableProgress
from collection.workerIdentity import ensure_identity
from storage.pandasStore import PandasStore,atomic_json
from tests import test_providers as fixtures
from dataset_builder import _fixtures


class EuropeanSofa(fixtures.Sofa):
    def iter_season_events(self,*args):
        for mid in (1,2):
            event=fixtures.event(mid);event['tournament']={'uniqueTournament':{'id':7},'qualificationOrPreliminary':mid==2}
            yield event


class QualifierTests(unittest.TestCase):
    def test_separate_fixture_scopes_and_reuse_legacy_checkpoint_without_scraping_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';ensure_identity(root,'headless')
            parent=root/'champions-league/2024-2025';qualifier=root/'champions-league-qualifiers/2024-2025'
            sofa=EuropeanSofa()
            events=list(sofa.iter_season_events());legacy=events[1]
            loader=SofaScoreLoader(None);season=loader.select_season(7,'Champions League',61627,'2024/2025')
            match=loader._build_match(season,legacy,fixtures.lineup());match.metadata['provider_status']={'sofascore':'complete'}
            PandasStore(parent/'match_checkpoints/2').to_csv(loader.dataset)
            runner=SeasonRunner({'sofascore':sofa},parent,providers=['sofascore'],spatial=(),report=lambda *a,**k:None)
            main=runner.run('Champions League','2024/2025',build_csvs=False)
            self.assertEqual(main.match_id.tolist(),[1]);self.assertEqual(sofa.calls,[1])
            inherited=PandasStore(parent/'match_checkpoints/2').from_csv().matches[2]
            self.assertEqual(inherited.season.competition.name,'Champions League')
            target=SeasonRunner({'sofascore':sofa},qualifier,providers=['sofascore'],spatial=(),report=lambda *a,**k:None)
            result=target.run('Champions League Qualifiers','2024/2025',build_csvs=False)
            self.assertEqual(result.match_id.tolist(),[2]);self.assertEqual(sofa.calls,[1])
            copied=PandasStore(qualifier/'match_checkpoints/2').from_csv().matches[2]
            self.assertEqual(copied.season.competition.name,'Champions League Qualifiers')
            self.assertEqual(_fixtures(qualifier,{'competition':'Champions League Qualifiers','season':'2024/2025'}).match_id.tolist(),[2])
            audit=PortableProgress(None,root);audit.scan(cache=False)
            self.assertEqual({(r['competition'],r['match_id']) for r in audit.local_records.values()},
                             {('Champions League','1'),('Champions League Qualifiers','2')})
    def test_qualifier_transport_uses_api_then_extension_without_uninitialized_http(self):
        from providers.transports.whoscored import WhoScoredTransport
        from providers.httpClient import ProviderRequestError
        parent='regions/250/tournaments/12/europe-champions-league'
        html='<a href="/regions/250/tournaments/757/europe-champions-league-qualification">Qualifying</a>'
        with tempfile.TemporaryDirectory() as tmp:
            api=WhoScoredClient();ext=WhoScoredClient()
            api.get_text=Mock(return_value=html);ext.get_text=Mock(return_value=html)
            client=WhoScoredTransport(tmp,http=api,extension=ext,report=lambda *a,**k:None)
            self.assertIn('/757/',client.qualification_path(parent))
            ext.get_text.assert_not_called()
            api.get_text.side_effect=ProviderRequestError('challenge',status_code=403)
            self.assertIn('/757/',client.qualification_path(parent))
            self.assertEqual(client.active,'extension')
            self.assertEqual(client.get_text(parent),html)
            self.assertEqual(ext.get_text.call_count,2)
            client.close()

    def test_inventory_scope_names_use_utf8_on_windows(self):
        # UTF-8 Ł contains 0x81, which Windows cp1252 cannot decode.
        original=Path.read_text
        def windows_read(path,encoding=None,errors=None):
            return original(path,encoding=encoding or 'cp1252',errors=errors)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';ensure_identity(root,'headless')
            directory=root/'champions-league/2024-2025'
            event=fixtures.event(1);event['homeTeam']['name']='Łódź'
            atomic_json(directory/'fixture-scopes.json',{'events':[event]})
            loader=SofaScoreLoader(None);season=loader.select_season(7,'Champions League',61627,'2024/2025')
            match=loader._build_match(season,event,fixtures.lineup())
            match.metadata['provider_status']={'sofascore':'complete'}
            progress=PortableProgress(None,root)
            with patch.object(Path,'read_text',windows_read):progress.record_match(match,collected_here=True)
            self.assertEqual(next(iter(progress.local_records.values()))['evidence']['label'],'Łódź vs '+event['awayTeam']['name'])

    def test_fotmob_qualifier_feed_is_separate_and_whoscored_ids_come_from_published_links(self):
        self.assertEqual(competition_by_name('Champions League').fotmob_id,42)
        self.assertEqual(competition_by_name('Champions League Qualifiers').fotmob_id,10611)
        self.assertEqual(competition_by_name('Europa League Qualifiers').fotmob_id,10613)
        client=WhoScoredClient();client.get_text=Mock(return_value='<a href="/regions/250/tournaments/757/europe-champions-league-qualification">Qualifying</a>')
        self.assertIn('/757/',client.qualification_path('regions/250/tournaments/12/europe-champions-league'))
        client.get_text=Mock(return_value='<a href="/Regions/250/Tournaments/757/Show/International-UEFA-Champions-League-Qualification">Qualifying</a>')
        self.assertIn('/757/',client.qualification_path('regions/250/tournaments/12/europe-champions-league'))
        client.get_text=Mock(return_value='<a href="/regions/250/tournaments/30/europe-europa-league">Other competition</a>')
        self.assertEqual(client.qualification_path('regions/250/tournaments/12/europe-champions-league'),'regions/250/tournaments/12/europe-champions-league')


if __name__=='__main__':unittest.main()
