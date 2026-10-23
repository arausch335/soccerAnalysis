"""WhoScored History evidence remains useful when the current-team field is blank."""
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch

from providers.httpClient import ProviderError
from providers.whoScoredClient import WhoScoredClient
from providers.transports.whoscored import WhoScoredTransport
from providers.playerProfiles import whoscored_profile,ProfileVerifier
from providers.identityReview import ReviewStore,identity_scope
from tests.test_player_profiles import profile


HTML='''<div class="header"><h1 class="header-name">Bohdan Liedniev</h1>
<img class="player-picture" src="https://cdn.example/img/players/347070.jpg">
<div><span>Current Team:</span><a class="team-link" href="/teams/0/show/-"></a></div>
<div><span>Age:</span>28 years old (<i>07-04-1998</i>)</div></div>
<table id="top-player-stats-summary-grid"><tbody id="player-table-statistics-body">
<tr><td>2021/2022</td><td><a href="/teams/350/show/ukraine-dynamo-kyiv">Dynamo Kyiv</a></td><td>UCL</td></tr>
<tr><td>2020/2021</td><td><a href="/teams/350/show/ukraine-dynamo-kyiv">Dynamo Kyiv</a></td><td>UEL</td></tr>
<tr><td>2020/2021</td><td><a href="/teams/350/show/ukraine-dynamo-kyiv">Dynamo Kyiv</a></td><td>UCL</td></tr>
</tbody></table>'''


class WhoScoredProfileTests(unittest.TestCase):
    def test_blank_current_team_keeps_birthday_and_unique_club_seasons(self):
        client=WhoScoredClient();client.get_text=Mock(return_value=HTML)
        p=client.player_profile(347070)
        self.assertEqual(p['birth_date'],'1998-04-07');self.assertIsNone(p['club'])
        self.assertEqual([(r['club'],r['start'],r['end']) for r in p['career']],
                         [('Dynamo Kyiv','2021-07-01','2022-06-30'),('Dynamo Kyiv','2020-07-01','2021-06-30')])
        with self.assertRaises(ProviderError):whoscored_profile(999,HTML)
        client.close()

    def test_partial_http_header_loads_history_through_extension(self):
        with TemporaryDirectory() as root:
            api=Mock();ext=Mock();api.player_profile.return_value=whoscored_profile(347070,HTML.split('<table')[0])
            ext.player_profile.return_value=whoscored_profile(347070,HTML)
            client=WhoScoredTransport(root,http=api,extension=ext,report=lambda *a,**k:None)
            p=client.player_profile(347070)
            self.assertEqual(len(p['career']),2);self.assertEqual(p['birth_date'],'1998-04-07')
            self.assertEqual(client.active,'extension');ext.player_profile.assert_called_once_with(347070)

    def test_extension_failure_preserves_available_http_birthday(self):
        with TemporaryDirectory() as root:
            api=Mock();ext=Mock();api.player_profile.return_value=whoscored_profile(347070,HTML.split('<table')[0])
            ext.player_profile.side_effect=ProviderError('History unavailable')
            client=WhoScoredTransport(root,http=api,extension=ext,report=lambda *a,**k:None)
            self.assertEqual(client.player_profile(347070)['birth_date'],'1998-04-07')

    def test_review_keeps_partial_evidence_without_automatic_identity_confirmation(self):
        with TemporaryDirectory() as root,identity_scope(root):
            who=Mock();sofa=Mock();who.player_profile.return_value=whoscored_profile(347070,HTML)
            sofa.player_profile.return_value=profile('sofascore',862061,birth_date='1998-04-07')
            verifier=ProfileVerifier({'whoscored':who,'sofascore':sofa},root,lambda *a,**k:None)
            p=dict(kind='player',provider='whoscored',source_id='347070',target_id='862061',source_name='Bohdan Liedniev',target_name='Bogdan Lednev')
            result=verifier.investigate([p])[0]['investigation']
            self.assertEqual(result['source']['birth_date'],'1998-04-07')
            self.assertEqual(result['checks']['birthday'],'match');self.assertEqual(result['checks']['club'],'missing')
            self.assertTrue(any('current club' in e for e in result['errors']))
            self.assertEqual(ReviewStore(root).snapshot()['decisions'],[])
            sofa.player_profile.return_value=profile('sofascore',862062,birth_date='1998-04-06')
            result=verifier.investigate([{**p,'target_id':'862062'}])[0]['investigation']
            self.assertEqual(result['checks']['birthday'],'conflict')
            self.assertEqual(result['status'],'conflict')
