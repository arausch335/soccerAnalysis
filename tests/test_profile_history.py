from copy import deepcopy
from datetime import date,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from providers.profileHistory import compare_careers,club_matcher
from providers.playerProfiles import assess_profiles,sofa_profile,fotmob_profile,checked_profile
from providers.identityReview import ReviewStore,identity_scope,player_link
from providers.reep import install as install_reep,_digest,FORMAT
from concordance.registry import Registry
from tests.test_player_profiles import profile,investigated,proposal


class ProfileHistoryTests(unittest.TestCase):
    def test_existing_project_club_link_overrides_different_names(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            registry=Registry(Path(tmp)/'_system/identity/alex_ids.json')
            registry.assign('team',{'sofascore_id':'12','fotmob_id':'34'})
            registry.save()
            p=investigated();a=p['investigation']['source'];b=p['investigation']['baseline']
            a.update(club='One official name',club_id='34',career=[{'club':'One official name','club_id':'34','start':'2020-01-01'}])
            b.update(club='A different display name',club_id='12',career=[{'club':'A different display name','club_id':'12','start':'2020-01-01'}])
            checks=assess_profiles(p,tmp)
            self.assertEqual(set(checks['checks'].values()),{'match'})
            self.assertEqual(checks['career_similarity']['similarity_percent'],100)
            store=ReviewStore(tmp);store.ingest([p]);self.assertEqual(player_link('fotmob',77,tmp),'7')

    def test_reep_links_and_reviewed_historical_profiles_are_used(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            rows=[['team','fotmob','34','rtshared'],['team','sofascore','12','rtshared']]
            install_reep(tmp,{'format':FORMAT,'version':_digest(rows),'rows':rows})
            same=club_matcher(tmp)
            self.assertTrue(same({'club':'Different','club_id':'34'},{'club':'Name','club_id':'12'},'fotmob','sofascore'))
            self.assertTrue(same({'club':'Viitorul','club_id':'210132'},{'club':'Farul','club_id':'3294'},'fotmob','sofascore'))

    def test_known_different_club_ids_are_not_merged_by_identical_names(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            rows=[['team','fotmob','34','rtone'],['team','sofascore','12','rttwo']]
            install_reep(tmp,{'format':FORMAT,'version':_digest(rows),'rows':rows})
            p=investigated();p['investigation']['source']['club_id']='34';p['investigation']['baseline']['club_id']='12'
            self.assertEqual(assess_profiles(p,tmp)['checks']['club'],'conflict')
            store=ReviewStore(tmp);store.ingest([p]);self.assertIsNone(player_link('fotmob',77,tmp))

    def test_old_evidence_uses_explicit_reviewed_display_aliases(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated()
            for key,name in (('source','FC Viitorul Constanța'),('baseline','FCV Farul Constanța')):
                p['investigation'][key].update(club=name,career=[{'club':name,'start':'2020-01-01'}])
            self.assertEqual(set(assess_profiles(p,tmp)['checks'].values()),{'match'})

    def test_single_club_with_different_first_appearance_dates_is_enough(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated()
            for key,start in (('source','2026-02-08'),('baseline','2026-03-05')):
                p['investigation'][key]['career']=[{'club':'AEK Athens','start':start}]
            store=ReviewStore(tmp);store.ingest([p])
            decision=store.snapshot()['decisions'][0]
            self.assertEqual(decision['investigation']['career_similarity']['similarity_percent'],100)

    def test_small_transfer_date_differences_and_duplicates_do_not_reduce_score(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();p['investigation']['source']['career'][0]['end']='2023-01-18'
            p['investigation']['source']['career'][1]['start']='2023-01-18'
            p['investigation']['source']['career']*=2
            self.assertEqual(assess_profiles(p,tmp)['career_similarity']['similarity_percent'],100)

    def test_threshold_uses_dated_history_instead_of_same_set_of_clubs(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            start=date(2020,1,1);end=start+timedelta(days=1000)
            left=profile('fotmob',77,career=[{'club':'AEK Athens','start':str(start),'end':str(end)}])
            # Symmetric comparison counts the 30-day tolerance on one side only.
            right=profile('sofascore',7,career=[
                {'club':'AEK Athens','start':str(start),'end':str(start+timedelta(days=584))},
                {'club':'Other','start':str(start+timedelta(days=584)),'end':str(end)}])
            score=compare_careers(left,right,root=tmp,as_of=end)
            self.assertEqual(score['similarity_percent'],59.9)
            self.assertEqual(score['state'],'conflict')
            right['career'][0]['end']=right['career'][1]['start']=str(start+timedelta(days=585))
            score=compare_careers(left,right,root=tmp,as_of=end)
            self.assertEqual(score['similarity_percent'],60)
            self.assertEqual(score['state'],'match')
            p=investigated(source=left,baseline=right)
            store=ReviewStore(tmp);store.ingest([p])
            self.assertEqual(player_link('fotmob',77,tmp),'7')
            p=investigated();p['investigation']['source']['career'].reverse()
            for row in p['investigation']['source']['career']:
                row['club']='Other' if row['club']=='AEK Athens' else 'AEK Athens'
            self.assertEqual(assess_profiles(p,tmp)['checks']['career'],'conflict')

    def test_future_rows_and_short_shared_windows_do_not_create_false_agreement(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            a=profile('fotmob',77,career=[{'club':'AEK Athens','start':'2030-01-01'}])
            b=profile('sofascore',7,career=[{'club':'AEK Athens','start':'2030-01-01'}])
            self.assertIsNone(compare_careers(a,b,root=tmp,as_of=date(2026,11,2))['similarity_percent'])
            for v in (a,b):v['career']=[{'club':'AEK Athens','start':'2026-10-20'}]
            self.assertEqual(compare_careers(a,b,root=tmp,as_of=date(2026,11,2))['state'],'missing')

    def test_high_history_similarity_never_overrides_birthday_conflict_or_manual_rejection(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            p=investigated();p['investigation']['source']['birth_date']='2002-07-18'
            store=ReviewStore(tmp);store.ingest([p])
            self.assertEqual(assess_profiles(p,tmp)['career_similarity']['similarity_percent'],100)
            self.assertIsNone(player_link('fotmob',77,tmp))
            p=investigated();store.decide(store.snapshot()['pending'][0]['id'],'reject')
            store.ingest([p]);store.auto_confirm();self.assertIsNone(player_link('fotmob',77,tmp))

    def test_existing_pending_evidence_is_rescored_without_refetching_profiles(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            store=ReviewStore(tmp);p=investigated()
            for side in ('source','baseline'):
                p['investigation'][side]['career']=[{'club':'AEK Athens','start':'2024-01-01'}]
            p['investigation']['checks']={'birthday':'match','club':'match','career':'missing'}
            from providers.identityReview import validate
            p=validate(p)
            with store.connect() as db:db.execute('INSERT INTO proposals VALUES(?,?)',(p['id'],json.dumps(p)))
            self.assertEqual(store.snapshot()['total'],1)
            self.assertGreater(store.auto_confirm(),0)
            self.assertEqual(store.snapshot()['total'],0)
            self.assertEqual(store.snapshot()['decisions'][0]['investigation']['career_similarity']['similarity_percent'],100)

    def test_no_shared_dates_does_not_match_an_identical_club(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            a=profile('fotmob',77,career=[{'club':'AEK Athens','start':'2020-01-01','end':'2021-01-01'}])
            b=profile('sofascore',7,career=[{'club':'AEK Athens','start':'2024-01-01','end':'2025-01-01'}])
            result=compare_careers(a,b,root=tmp)
            self.assertEqual(result['state'],'missing');self.assertIsNone(result['similarity_percent'])

    def test_provider_normalizers_preserve_current_and_historical_native_club_ids(self):
        sofa=sofa_profile(7,{'player':{'id':7,'team':{'name':'Alias','id':12}}},
            {'transferHistory':[{'player':{'id':7},'transferTo':{'name':'Alias','id':12},'transferDateTimestamp':1577836800}]})
        fot=fotmob_profile(77,{'id':77,'primaryTeam':{'teamName':'Other name','teamId':34},
            'careerHistory':{'careerItems':{'senior':{'teamEntries':[{'team':'Other name','teamId':34,'startDate':'2020-01-01'}]}}}})
        self.assertEqual(sofa['club_id'],'12');self.assertEqual(sofa['career'][0]['club_id'],'12')
        self.assertEqual(fot['club_id'],'34');self.assertEqual(fot['career'][0]['club_id'],'34')
        bad=deepcopy(fot);bad['career'][0]['club_id']='../../other'
        with self.assertRaises(ValueError):checked_profile(bad,'fotmob',77)


if __name__=='__main__':unittest.main()
