from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from providers.playerProfiles import (compare_profiles,sofa_profile,fotmob_profile,whoscored_profile,
                                     checked_profile,ProfileVerifier)
from providers.identityReview import ReviewStore,identity_scope,player_link,revision
from providers.identity import IdentityResolver,IdentityError
from providers.httpClient import ProviderError,ProviderRequestError
from providers.transports.sofascore import SofaScoreTransport
from providers.transports.sofascore_extension import ExtensionSofaScoreClient
from providers.transports.fotmob import FotMobTransport
from providers.transports.whoscored import WhoScoredTransport
from tests.test_extension_payloads import extension_settings
from tests import test_reporting


def profile(provider,pid,**changes):
    url=(f'https://www.sofascore.com/api/v1/player/{pid}' if provider=='sofascore' else
         f'https://www.fotmob.com/players/{pid}' if provider=='fotmob' else f'https://www.whoscored.com/players/{pid}/history')
    return {'provider':provider,'id':str(pid),'name':'A Player','birth_date':'2002-07-03','club':'AEK Athens',
            'career':[{'club':'Primeiro de Agosto','start':'2020-07-01','end':'2023-01-01'},
                      {'club':'AEK Athens','start':'2023-01-01','end':None}], 'sources':[url],**changes}


def proposal(**changes):
    return {'kind':'player','provider':'fotmob','source_id':'77','source_name':'A. Plyr',
            'target_id':'7','target_name':'A Player','score':.75,'contexts':[],**changes}


def investigated(**changes):
    return proposal(investigation={'checked_at':'2026-10-20T00:00:00+00:00','source':profile('fotmob',77),
                                   'baseline':profile('sofascore',7),**changes})


class PlayerProfileTests(unittest.TestCase):
    def test_three_independent_checks_verify_but_any_conflict_or_missing_data_blocks(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();self.assertEqual(set(compare_profiles(p).values()),{'match'})
            for field,value in [('birth_date','2001-07-03'),('birth_date',None),('club','Other'),('career',[])]:
                changed=deepcopy(p);changed['investigation']['source'][field]=value
                self.assertFalse(all(v=='match' for v in compare_profiles(changed).values()),(field,value))

    def test_club_set_alone_does_not_override_reversed_career_dates(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();p['investigation']['source']['career']=[
                {'club':'AEK Athens','start':'2020-07-01','end':'2023-01-01'},
                {'club':'Primeiro de Agosto','start':'2023-01-01','end':None}]
            self.assertEqual(compare_profiles(p)['career'],'conflict')
            p=investigated();p['investigation']['source']['career'].insert(1,{'club':'Unrelated Club','start':'2021-01-01','end':'2022-01-01'})
            self.assertEqual(compare_profiles(p)['career'],'conflict')

    def test_single_club_and_partial_history_do_not_claim_full_career_agreement(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();p['investigation']['source']['career']=p['investigation']['source']['career'][1:]
            self.assertEqual(compare_profiles(p)['career'],'missing')

    def test_verified_mapping_is_automatic_shared_and_never_overwrites_manual_rejection(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            store=ReviewStore(tmp);store.ingest([investigated()]);snapshot=store.snapshot()
            self.assertEqual(snapshot['pending'],[]);self.assertEqual(snapshot['decisions'][0]['review_method'],'profile_evidence')
            self.assertEqual(player_link('fotmob',77),'7')
            before=revision(tmp);p=investigated();p['investigation']['checked_at']='2026-10-21T00:00:00+00:00'
            store.ingest([p]);self.assertEqual(revision(tmp),before)
            self.assertEqual(store.snapshot()['decisions'][0]['investigation']['checked_at'],'2026-10-21T00:00:00+00:00')
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            store=ReviewStore(tmp);store.ingest([proposal()]);pid=store.snapshot()['pending'][0]['id']
            with self.assertRaises(ValueError):store.decide(pid,'confirm')
            store.decide(pid,'reject');store.ingest([investigated()])
            self.assertEqual(store.snapshot()['decisions'][0]['action'],'reject');self.assertIsNone(player_link('fotmob',77))

    def test_ambiguous_verified_candidates_and_forged_profile_identity_stay_unmerged(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();other=deepcopy(p);other.update(target_id='8',target_name='Other profile')
            other['investigation']['baseline']=profile('sofascore',8)
            store=ReviewStore(tmp);store.ingest([p,other]);self.assertEqual(len(store.snapshot()['pending']),2)
            bad=deepcopy(p);bad['investigation']['source']['id']='88'
            with self.assertRaises(ValueError):store.ingest([bad])
            bad=deepcopy(p);bad['investigation']['source']['sources']=['https://evil.test/players/77']
            with self.assertRaises(ValueError):store.ingest([bad])

    def test_provider_normalizers_retain_ids_dates_and_dated_club_stints(self):
        timestamp=lambda s:int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
        sofa=sofa_profile(7,{'player':{'id':7,'name':'A','team':{'name':'AEK Athens'},'dateOfBirthTimestamp':timestamp('2002-07-03')}},
            {'transferHistory':[{'player':{'id':7},'transferTo':{'name':'AEK Athens'},'transferDateTimestamp':timestamp('2023-01-01')},
                                {'player':{'id':7},'transferTo':{'name':'Primeiro de Agosto'},'transferDateTimestamp':timestamp('2020-07-01')}]})
        fot=fotmob_profile(77,{'id':'77','name':'A','birthDate':{'utcTime':'2002-07-03T00:00:00Z'},'primaryTeam':{'teamName':'AEK Athens'},
            'careerHistory':{'careerItems':{'senior':{'teamEntries':[{'participantId':77,'team':'AEK Athens','startDate':'2023-01-01T00:00:00','endDate':None}]}}}})
        self.assertEqual(sofa['birth_date'],fot['birth_date']);self.assertEqual(sofa['career'][0]['end'],'2023-01-01')
        with self.assertRaises(ProviderError):fotmob_profile(88,{'id':77})

    def test_whoscored_rendered_history_parses_header_and_excludes_national_team(self):
        html='''<div class="header"><h1 class="header-name">A Player</h1><img class="player-picture" src="https://cdn.test/img/players/9.jpg"><div><span>Age:</span>24 years old (<i>03-07-2002</i>)</div><a class="team-link" href="/teams/2/show/aek-athens">AEK Athens</a></div><table id="top-player-stats-summary-grid"><tbody><tr><td>2023/2024</td><td><a href="/teams/2/show/aek-athens">AEK Athens</a></td></tr><tr><td>2023</td><td><a href="/teams/3/show/angola">Angola</a></td><td><a href="/regions/1/tournaments/1/international-cup">Cup</a></td></tr></tbody></table>'''
        p=whoscored_profile(9,html);self.assertEqual(p['birth_date'],'2002-07-03');self.assertEqual(p['club'],'AEK Athens')
        self.assertEqual(p['career'],[{'club':'AEK Athens','start':'2023-07-01','end':'2024-06-30','precision':'season'}])
        with self.assertRaises(ProviderError):whoscored_profile(10,html)

    def test_fotmob_null_current_team_and_nullable_career_fields_stay_missing(self):
        histories=[None,{'careerItems':None},{'careerItems':{'senior':None}},
                   {'careerItems':{'senior':{'teamEntries':None}}}]
        for history in histories:
            with self.subTest(history=history):
                value=fotmob_profile(77,{'id':77,'primaryTeam':None,'careerHistory':history})
                checked_profile(value,'fotmob','77')
                self.assertIsNone(value['club']);self.assertEqual(value['career'],[])
        with self.assertRaises(ProviderError):fotmob_profile(77,{'id':77,'primaryTeam':'bad shape'})

    def test_sofascore_null_team_and_transfer_destination_do_not_crash(self):
        value=sofa_profile(7,{'player':{'id':7,'team':None}},
            {'transferHistory':[{'player':{'id':7},'transferTo':None,'toTeamName':'Former Club','transferDateTimestamp':1609459200}]})
        self.assertIsNone(value['club']);self.assertEqual(value['career'][0]['club'],'Former Club')
        checked_profile(value,'sofascore','7')

    def test_optional_profile_decode_failures_leave_review_pending_and_are_cached(self):
        for error in (AttributeError('Unexpected nullable provider field'),IndexError('Missing provider row')):
            with TemporaryDirectory() as tmp,identity_scope(tmp),self.subTest(error=type(error).__name__):
                fot=Mock();fot.player_profile.side_effect=error
                sofa=Mock();sofa.player_profile.return_value=profile('sofascore',7)
                verifier=ProfileVerifier({'fotmob':fot,'sofascore':sofa},tmp,lambda *a:None)
                rows=verifier.investigate([proposal()]);verifier.investigate([proposal()])
                self.assertEqual(rows[0]['investigation']['status'],'incomplete')
                self.assertTrue(rows[0]['investigation']['errors'])
                self.assertIsNone(player_link('fotmob',77));self.assertEqual(fot.player_profile.call_count,1)

    def test_missing_current_club_never_verifies_an_identity_automatically(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            fot=Mock();fot.player_profile.return_value=profile('fotmob',77,club=None)
            sofa=Mock();sofa.player_profile.return_value=profile('sofascore',7)
            rows=ProfileVerifier({'fotmob':fot,'sofascore':sofa},tmp,lambda *a:None).investigate([proposal()])
            self.assertEqual(rows[0]['investigation']['status'],'incomplete')
            self.assertEqual(rows[0]['investigation']['checks']['club'],'missing')
            self.assertIsNone(player_link('fotmob',77))

    def test_lookup_cache_reuses_profiles_and_source_errors_leave_manual_evidence(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            fot=Mock();fot.player_profile.return_value=profile('fotmob',77)
            sofa=Mock();sofa.player_profile.return_value=profile('sofascore',7)
            verifier=ProfileVerifier({'sofascore':sofa,'fotmob':fot},tmp,lambda *a:None)
            verifier.investigate([proposal()]);verifier.investigate([proposal()])
            self.assertEqual(fot.player_profile.call_count,1);self.assertEqual(sofa.player_profile.call_count,1)
            self.assertEqual(player_link('fotmob',77),'7')
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            fot.player_profile.side_effect=ProviderRequestError('HTTP 502',status_code=502)
            verifier=ProfileVerifier({'sofascore':sofa,'fotmob':fot},tmp,lambda *a:None)
            rows=verifier.investigate([proposal()]);self.assertEqual(rows[0]['investigation']['status'],'incomplete')
            self.assertEqual(rows[0]['investigation']['checks']['birthday'],'missing')

    def test_profile_requests_follow_api_then_extension(self):
        with TemporaryDirectory() as tmp:
            api=Mock();extension=Mock();api.player_profile.side_effect=ProviderError('Blocked')
            extension.player_profile.return_value=profile('fotmob',77)
            fot=FotMobTransport(tmp,http=api,extension=extension,report=lambda *a,**k:None)
            self.assertEqual(fot.player_profile(77)['id'],'77');self.assertEqual(fot.active,'extension')
            api=Mock();extension=Mock();api.player_profile.side_effect=ProviderError('History missing')
            extension.player_profile.return_value=profile('whoscored',77)
            who=WhoScoredTransport(tmp,http=api,extension=extension,report=lambda *a,**k:None)
            self.assertEqual(who.player_profile(77)['id'],'77');self.assertEqual(who.active,'extension')

    def test_profile_denial_does_not_pause_match_collection_for_reload_recovery(self):
        with TemporaryDirectory() as tmp:
            bridge=Mock();bridge.request.return_value={'ok':True,'status':403,'source_url':'https://www.sofascore.com/','payload':None}
            client=ExtensionSofaScoreClient(tmp,extension_settings(),bridge=bridge,report=lambda *a,**k:None)
            with self.assertRaises(ProviderRequestError):client.player(7)
            self.assertEqual([c.args[0] for c in bridge.request.call_args_list],['page'])


class ProfileSharingTests(unittest.TestCase):
    setUp=test_reporting.ReportingTests.setUp
    reporter=test_reporting.ReportingTests.reporter
    receiver=test_reporting.ReportingTests.receiver

    def test_mains_unchecked_queue_is_investigated_by_worker_and_auto_link_returns_to_main(self):
        server=self.receiver();server.store.identities.ingest([proposal()],'main')
        client=self.reporter();client.pull()
        local=ReviewStore(self.root);self.assertEqual(len(local.snapshot()['pending']),1)
        fot=Mock();fot.player_profile.return_value=profile('fotmob',77)
        sofa=Mock();sofa.player_profile.return_value=profile('sofascore',7)
        verifier=ProfileVerifier({'sofascore':sofa,'fotmob':fot},self.root,lambda *a:None)
        verifier.pending();client.enqueue();client.flush()
        result=server.store.identities.snapshot()
        self.assertEqual(result['pending'],[])
        self.assertEqual(result['decisions'][0]['review_method'],'profile_evidence')
        client.pull();self.assertEqual(ReviewStore(self.root).snapshot()['decisions'][0]['target_id'],'7')

    def test_idle_queue_lookup_uses_and_closes_only_the_needed_provider_clients(self):
        from providers.playerProfiles import investigate_queue
        store=ReviewStore(self.root);store.ingest([proposal()])
        fot=Mock();fot.player_profile.return_value=profile('fotmob',77)
        sofa=Mock();sofa.player_profile.return_value=profile('sofascore',7)
        with patch('providers.transports.sofascore.create_sofascore_client',return_value=sofa),patch('providers.transports.fotmob.create_fotmob_client',return_value=fot),patch('providers.transports.whoscored.create_whoscored_client') as who:
            self.assertTrue(investigate_queue(self.root,lambda *a:None));who.assert_not_called()
        sofa.close.assert_called_once();fot.close.assert_called_once()
