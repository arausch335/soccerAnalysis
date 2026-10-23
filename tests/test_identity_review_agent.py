from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from collection.identityReviewAgent import IdentityReviewAgent
from providers.identityReview import identity_scope,review_root
from providers.transports.bridge import extension_scope,acquire_bridge


class MainIdentityReviewTests(unittest.TestCase):
    def test_disconnected_extension_never_starts_a_profile_request(self):
        with tempfile.TemporaryDirectory() as root:
            agent=IdentityReviewAgent(root,SimpleNamespace(ready=False))
            with patch('providers.playerProfiles.investigate_queue') as investigate:
                self.assertFalse(agent.step());investigate.assert_not_called()
            self.assertEqual(agent.status['state'],'waiting_for_extension')

    def test_old_extension_is_reported_without_issuing_or_caching_failed_lookups(self):
        with tempfile.TemporaryDirectory() as root:
            agent=IdentityReviewAgent(root,SimpleNamespace(ready=True,capabilities=set()))
            with patch('providers.playerProfiles.investigate_queue') as investigate:
                self.assertFalse(agent.step());investigate.assert_not_called()
            self.assertEqual(agent.status['state'],'update_extension')
            self.assertIn('Reload Soccer Collector',agent.status['message'])

    def test_extension_upgrade_retries_only_protocol_failures_and_preserves_decisions(self):
        import json
        from providers.identityReview import ReviewStore,stamp
        from providers.playerProfiles import retry_legacy_extension_failures
        with tempfile.TemporaryDirectory() as root:
            store=ReviewStore(root)
            p=dict(kind='player',provider='whoscored',source_id='1',target_id='2',source_name='One',target_name='Two',
                   investigation={'checked_at':stamp(),'errors':['Unknown collector command.']})
            store.ingest([p]);proposal=store.proposals()[0];store.decide(proposal['id'],'reject')
            with store.connect() as db:
                db.execute('CREATE TABLE profiles(provider TEXT,id TEXT,payload TEXT,expires TEXT,PRIMARY KEY(provider,id))')
                for pid,error in [('1','Unknown collector command.'),('2','HTTP 429')]:
                    db.execute('INSERT INTO profiles VALUES(?,?,?,?)',('whoscored',pid,json.dumps({'error':error}),'2099'))
            self.assertEqual(retry_legacy_extension_failures(root),1)
            self.assertNotIn('investigation',store.proposals()[0])
            self.assertEqual(store.snapshot()['decisions'][0]['action'],'reject')
            with store.connect() as db:self.assertEqual(db.execute('SELECT id FROM profiles').fetchall(),[('2',)])
            self.assertEqual(retry_legacy_extension_failures(root),0)

    def test_background_checks_reuse_main_extension_and_identity_root(self):
        with tempfile.TemporaryDirectory() as root,ExitStack() as stack:
            stack.enter_context(identity_scope(root));stack.enter_context(extension_scope())
            bridge,_=acquire_bridge();bridge.connected.set();bridge.client_id='connected';bridge.capabilities={'player-profiles-v1'}
            changed=Mock();agent=IdentityReviewAgent(root,bridge,on_change=changed)
            def investigate(path,report):
                self.assertEqual(review_root(),Path(root))
                shared,owned=acquire_bridge();self.assertIs(shared,bridge);self.assertFalse(owned)
                agent.stop.set();return True
            with patch('providers.identityReview.seed_existing'),patch('providers.playerProfiles.investigate_queue',side_effect=investigate):
                agent.start();agent.thread.join(timeout=2);self.assertFalse(agent.thread.is_alive());agent.close()
            changed.assert_called_once();self.assertEqual(agent.status['state'],'stopped')

    def test_scraping_only_queues_profile_candidates_without_fetching(self):
        from providers.identityReview import ReviewStore
        from tests.test_providers import ProviderTests,fot_payload
        from providers import FotMobLoader
        with tempfile.TemporaryDirectory() as root,identity_scope(root),patch('providers.playerProfiles.ProfileVerifier') as verifier:
            match=ProviderTests().baseline();payload=fot_payload()
            payload['content']['lineup']['homeTeam']['starters'][0]['name']='N. Uno'
            FotMobLoader().enrich(match,payload,allow_partial=True)
            self.assertTrue(ReviewStore(root).snapshot()['pending']);verifier.assert_not_called()

    def test_legacy_failed_checks_get_one_main_retry_then_back_off(self):
        from providers.playerProfiles import needs_profile_check
        from datetime import datetime,timezone
        self.assertTrue(needs_profile_check({}))
        legacy={'investigation':{'checked_at':datetime.now(timezone.utc).isoformat(),'errors':['Old worker lookup failed']}}
        self.assertTrue(needs_profile_check(legacy))
        legacy['investigation']['workflow']='main-profile-review-v1'
        self.assertFalse(needs_profile_check(legacy))
        legacy['investigation']['checked_at']='2020-01-01T00:00:00+00:00'
        self.assertTrue(needs_profile_check(legacy))
        self.assertFalse(needs_profile_check({'investigation':{'errors':[],'checks':{'club':'conflict'}}}))
