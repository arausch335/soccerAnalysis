from copy import deepcopy
from datetime import date
from tempfile import TemporaryDirectory
import unittest

from providers.profileHistory import compare_careers
from providers.profileLoans import loan_status
from providers.playerProfiles import sofa_profile,fotmob_profile,checked_profile
from providers.identityReview import identity_scope
from tests.test_player_profiles import profile


class LoanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.scope=identity_scope(self.temp.name);self.scope.__enter__();self.addCleanup(lambda:self.scope.__exit__(None,None,None))
        self.source=profile('fotmob',77,career=[
            {'club':'Parent','start':'2020-01-01','end':'2021-01-01'},
            {'club':'Borrower','start':'2021-01-01','end':'2022-01-01','is_loan':True,'loan_parent_club':'Parent'},
            {'club':'Parent','start':'2022-01-01','end':'2024-01-01'}])
        self.baseline=profile('sofascore',7,career=[{'club':'Parent','start':'2020-01-01','end':'2024-01-01'}])

    def score(self,a=None,b=None):return compare_careers(a or self.source,b or self.baseline,root=self.temp.name,as_of=date(2024,1,1))

    def test_omitted_loan_is_missing_coverage_not_a_conflict_or_manufactured_match(self):
        result=self.score()
        self.assertEqual(result['state'],'match');self.assertEqual(result['similarity_percent'],100)
        self.assertLess(result['unadjusted_similarity_percent'],80)
        self.assertEqual(result['loan_excluded_days'],365*2)
        self.assertEqual(result['compared_days'],(366+365*2)*2)
        self.assertEqual(result['temporary_spells'][0]['evidence'],'explicit_loan')
        reverse=self.score(self.baseline,self.source)
        self.assertEqual(reverse['similarity_percent'],result['similarity_percent'])
        self.assertEqual(reverse['loan_excluded_days'],result['loan_excluded_days'])

    def test_continuing_parent_plus_borrower_does_not_double_penalize_registration(self):
        self.source['career'].append({'club':'Parent','start':'2020-01-01','end':'2024-01-01'})
        result=self.score();self.assertEqual(result['similarity_percent'],100)
        self.assertEqual(result['loan_excluded_days'],365)
        self.baseline['career'].append(deepcopy(self.source['career'][1]))
        result=self.score();self.assertEqual(result['similarity_percent'],100)

    def test_loan_reported_on_both_sides_remains_positive_comparable_evidence(self):
        self.baseline['career']=deepcopy(self.source['career'])
        result=self.score();self.assertEqual(result['similarity_percent'],100)
        self.assertEqual(result['loan_excluded_days'],0)

    def test_unknown_bounded_return_is_labelled_temporary_not_a_confirmed_loan(self):
        self.source['career'][1].pop('is_loan');self.source['career'][1].pop('loan_parent_club')
        result=self.score();self.assertEqual(result['similarity_percent'],100)
        self.assertEqual(result['temporary_spells'][0]['evidence'],'return_to_same_club')

    def test_explicit_permanent_or_unbounded_unknown_move_is_not_exempt(self):
        self.source['career'][1]['is_loan']=False
        result=self.score();self.assertEqual(result['loan_excluded_days'],0)
        self.assertEqual(result['state'],'conflict')
        self.source['career']=self.source['career'][:2]
        self.source['career'][1].pop('is_loan');self.source['career'][1].pop('loan_parent_club');self.source['career'][1]['end']='2024-01-01'
        result=self.score();self.assertEqual(result['loan_excluded_days'],0)

    def test_different_explicit_borrowers_remain_conflicting_despite_shared_parent(self):
        self.baseline['career']=deepcopy(self.source['career'])
        self.baseline['career'][1]['club']='Different Borrower'
        result=self.score();self.assertEqual(result['loan_excluded_days'],0)
        self.assertEqual(result['state'],'conflict');self.assertTrue(result['loan_conflicts'])

    def test_an_unrelated_parallel_registration_is_not_erased_by_a_loan_exemption(self):
        self.source['career'].append({'club':'Unrelated Club','start':'2021-01-01','end':'2022-01-01'})
        result=self.score();self.assertLess(result['similarity_percent'],100)
        self.assertGreater(result['compared_days'],(366+365*2)*2)

    def test_loan_only_evidence_does_not_create_an_identity_match(self):
        self.source['career']=self.source['career'][1:2]
        result=self.score();self.assertEqual(result['state'],'missing')
        self.assertIsNone(result['similarity_percent']);self.assertEqual(result['compared_days'],0)

    def test_current_explicit_loan_can_use_a_verified_preceding_parent_stint(self):
        self.source['career']=self.source['career'][:2]
        self.source['career'][1].pop('loan_parent_club');self.source['career'][1]['end']=None
        result=self.score();self.assertEqual(result['similarity_percent'],100)
        self.assertTrue(result['loan_excluded_days'])

    def test_numeric_transfer_codes_are_preserved_without_guessing_their_meaning(self):
        self.assertIsNone(loan_status({'type':1}));self.assertIsNone(loan_status({'transferType':2}))
        self.assertTrue(loan_status({'transferType':'Loan'}));self.assertFalse(loan_status({'transferType':'Permanent'}))

    def test_normalizers_retain_loan_flags_origins_and_uninterpreted_transfer_types(self):
        sofa=sofa_profile(7,{'player':{'id':7}}, {'transferHistory':[
            {'player':{'id':7},'transferFrom':{'id':1,'name':'Parent'},'transferTo':{'id':2,'name':'Borrower'},
             'transferDateTimestamp':1609459200,'isLoan':True,'type':1}]})
        row=sofa['career'][0];self.assertTrue(row['is_loan']);self.assertEqual(row['from_club_id'],'1');self.assertEqual(row['transfer_type'],1)
        fot=fotmob_profile(77,{'id':77,'careerHistory':{'careerItems':{'senior':{'teamEntries':[
            {'team':'Borrower','teamId':2,'transferType':'Loan','startDate':'2021-01-01','endDate':'2022-01-01'}]}}}})
        self.assertTrue(fot['career'][0]['is_loan']);self.assertEqual(fot['career'][0]['transfer_type'],'Loan')
        bad=deepcopy(fot);bad['career'][0]['is_loan']='probably'
        with self.assertRaises(ValueError):checked_profile(bad,'fotmob',77)


if __name__=='__main__':unittest.main()
