from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from providers.playerProfiles import birthday_rejection_reason
from providers.identityReview import ReviewStore,identity_scope
from tests.test_player_profiles import investigated,profile


def candidate(left,right):
    p=investigated();p['investigation']['source']['birth_date']=left;p['investigation']['baseline']['birth_date']=right
    return p


class BirthdayRuleTests(unittest.TestCase):
    def test_calendar_month_boundaries_in_both_directions(self):
        for first,boundary,after in [('2000-01-31','2000-02-29','2000-03-01'),
                                     ('2001-01-31','2001-02-28','2001-03-01'),
                                     ('2000-02-29','2000-03-29','2000-03-30'),
                                     ('1999-12-31','2000-01-31','2000-02-01')]:
            for left,right in [(first,boundary),(boundary,first)]:
                self.assertIsNone(birthday_rejection_reason(candidate(left,right)))
            for left,right in [(first,after),(after,first)]:
                self.assertIn('one calendar month',birthday_rejection_reason(candidate(left,right)))

    def test_missing_birthdays_and_timezone_sized_differences_stay_for_review(self):
        for left,right in [(None,'1998-04-07'),('1998-04-07',None),('1998-04-06','1998-04-07')]:
            self.assertIsNone(birthday_rejection_reason(candidate(left,right)))
            with TemporaryDirectory() as root,identity_scope(root):
                store=ReviewStore(root);store.ingest([candidate(left,right)])
                self.assertEqual(store.snapshot()['total'],1);self.assertEqual(store.snapshot()['decisions'],[])

    def test_large_birthday_difference_rejects_even_without_club_or_career(self):
        with TemporaryDirectory() as root,identity_scope(root):
            p=candidate('1990-01-01','1995-05-01');p['investigation']['source'].update(club=None,career=[])
            store=ReviewStore(root);store.ingest([p]);snapshot=store.snapshot()
            self.assertEqual(snapshot['total'],0);d=snapshot['decisions'][0]
            self.assertEqual(d['action'],'reject');self.assertEqual(d['review_method'],'birthday_mismatch')
            self.assertIn('1990-01-01',d['review_reason'])
            with self.assertRaises(ValueError):store.decide(d['id'],'confirm',automatic=True)

    def test_human_confirmation_and_rejection_are_preserved(self):
        for action in ('confirm','reject'):
            with TemporaryDirectory() as root,identity_scope(root):
                store=ReviewStore(root);p=candidate('1990-01-01','1990-01-02');store.ingest([p]);pid=store.snapshot()['pending'][0]['id']
                store.decide(pid,action);p=candidate('1990-01-01','1995-05-01');store.ingest([p]);store.auto_confirm()
                self.assertEqual(store.snapshot()['decisions'][0]['action'],action)
                self.assertEqual(store.snapshot()['decisions'][0]['review_method'],'manual')

    def test_rejected_candidate_does_not_block_the_remaining_verified_match(self):
        with TemporaryDirectory() as root,identity_scope(root):
            bad=candidate('2002-07-03','1990-01-01');bad.update(target_id='8',target_name='Other')
            bad['investigation']['baseline']=profile('sofascore',8,birth_date='1990-01-01')
            store=ReviewStore(root);store.ingest([bad,investigated()])
            ds=store.snapshot()['decisions'];self.assertEqual(store.snapshot()['total'],0)
            self.assertEqual({(d['target_id'],d['action']) for d in ds},{('8','reject'),('7','confirm')})
