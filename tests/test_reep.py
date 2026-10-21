import csv
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from providers import reep
from providers.identity import IdentityResolver, IdentityError, match_fixture


def snapshot(rows):
    return {'format':reep.FORMAT,'version':reep._digest(rows),'release':'test','rows':rows}


class ReepTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.rows=[['team','sofascore','1','rt1'],['team','fotmob','10','rt1'],
                   ['team','sofascore','2','rt2'],['team','fotmob','20','rt2'],
                   ['player','sofascore','1','rp1'],['player','fotmob','30','rp1'],
                   ['match','sofascore','100','rm1'],['match','fotmob','200','rm1']]
        reep.install(self.root,snapshot(self.rows))

    def test_types_are_separate_and_conflicting_existing_ids_are_preserved(self):
        index=reep.index(self.root)
        linked=index.resolve('team',{'sofascore_id':'1','fotmob_id':'','whoscored_id':''})
        self.assertEqual((linked['fotmob_id'],linked['reep_id']),('10','rt1'))
        self.assertEqual(index.resolve('player',{'sofascore_id':'1'})['reep_id'],'rp1')
        conflict=index.resolve('team',{'sofascore_id':'1','fotmob_id':'20'})
        self.assertEqual((conflict['fotmob_id'],conflict['reep_id'],conflict['identity_status']),('20','','conflict'))

    def test_ambiguous_peer_is_never_guessed_and_index_is_invalidated(self):
        value=snapshot(self.rows+[['team','fotmob','11','rt1']])
        reep.install(self.root,value)
        result=reep.index(self.root).resolve('team',{'sofascore_id':'1'})
        self.assertNotIn('fotmob_id',result)
        broken={**value,'rows':[]}
        with self.assertRaises(ValueError):reep.install(self.root,broken)
        self.assertEqual(reep.version(self.root),value['version'])

    def test_fixture_ids_resolve_different_names_but_reject_wrong_sides_or_day(self):
        event={'id':100,'startTimestamp':1725148800,'homeTeam':{'id':1,'name':'APO Levadiakos'},'awayTeam':{'id':2,'name':'Other'}}
        fixture={'id':200,'startTimestamp':1725148800,'homeTeam':{'id':10,'name':'Levadeia'},'awayTeam':{'id':20,'name':'Different'}}
        self.assertEqual(match_fixture(event,[fixture],provider='fotmob',review_root=self.root),fixture)
        for wrong in ({**fixture,'homeTeam':fixture['awayTeam'],'awayTeam':fixture['homeTeam']},
                      {**fixture,'startTimestamp':1725148800+86400}):
            with self.assertRaises(IdentityError):match_fixture(event,[wrong],provider='fotmob',review_root=self.root)

    def test_player_ids_precede_names_but_rejected_pairs_remain_rejected(self):
        candidate=SimpleNamespace(player=SimpleNamespace(id=1,name='Completely different'),shirt_number=8)
        entry={'id':30,'name':'Nickname','shirt_number':8}
        player,method=IdentityResolver(review_root=self.root).player('fotmob',entry,[candidate])
        self.assertIs(player,candidate);self.assertEqual(method,'reep_provider_id')
        from unittest.mock import patch
        with patch('providers.identity.REJECTED_PLAYER_ID_LINKS',{('fotmob','30','1')}):
            with self.assertRaises(IdentityError):IdentityResolver(review_root=self.root).player('fotmob',entry,[candidate])

    def test_bulk_checksums_redirect_chains_and_peer_selection(self):
        paths={};checksums={}
        for name,header,rows in (
            ('bridges',['reep_id','provider','namespace','external_id','entity_type'],[
                ['old','sofascore','player','1','player'],['new','fotmob','person','30','player'],
                ['other','whoscored','player','99','player'],['club','fotmob','team','10','team'],
                ['game','sofascore','event','100','match'],['game','fotmob','match','200','match'],
                ['new','fotmob','coach','999','player']]),
            ('redirects',['from_id','to_id'],[['old','middle'],['middle','new']])):
            path=self.root/(name+'.csv.gz');paths[name]=path
            with gzip.open(path,'wt',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(header);writer.writerows(rows)
            checksums['csv/'+name+'.csv.gz']=hashlib.sha256(path.read_bytes()).hexdigest()
        value=reep.import_release(paths['bridges'],paths['redirects'],{('player','sofascore','1'),('match','sofascore','100')},release='test',source='test',checksums=checksums)
        self.assertEqual(len(value['rows']),5)
        self.assertIn(('player','sofascore','1','new'),value['rows'])
        self.assertIn(('match','fotmob','200','game'),value['rows'])
        self.assertFalse(any(r[2]=='999' for r in value['rows']))
        self.assertFalse(any(r[2]=='99' for r in value['rows']))
        checksums['csv/bridges.csv.gz']='bad'
        with self.assertRaises(ValueError):reep.import_release(paths['bridges'],paths['redirects'],set(),release='test',source='test',checksums=checksums)

    def test_four_source_exports_keep_unlinked_clubs_and_report_conflicts(self):
        source=self.root/'league'/'2024-2025';source.mkdir(parents=True)
        (source/'config.json').write_text(json.dumps({'competition':'League','season':'2024/2025'}))
        event={'id':100,'startTimestamp':1725148800,'homeTeam':{'id':1,'name':'Club'},'awayTeam':{'id':999,'name':'Unlinked'}}
        (source/'schedule.json').write_text(json.dumps({'events':[event]}))
        import dataset_builder
        paths=dataset_builder.build_id_tables(self.root,self.root/'exports',build_player_ids=False)
        with paths['team concordance'].open() as f:teams=list(csv.DictReader(f))
        self.assertEqual(len(teams),2)
        linked=next(t for t in teams if t['sofascore_id']=='1')
        self.assertEqual((linked['fotmob_id'],linked['reep_id']),('10','rt1'))
        with paths['match concordance'].open() as f:match=next(csv.DictReader(f))
        self.assertEqual((match['fotmob_id'],match['reep_id']),('200','rm1'))


class ReepReportingTests(unittest.TestCase):
    def test_only_changed_index_is_sent_and_client_installs_it(self):
        from tests.test_reporting import ReportingTests
        case=ReportingTests();case.setUp();self.addCleanup(case.doCleanups)
        case.receiver();value=snapshot([['team','fotmob','10','rt1']])
        reep.install(case.main,value)
        reporter=case.reporter();reporter.pull()
        self.assertEqual(reep.version(case.root),value['version'])
        from collection.reporting import ReportingStore
        store=ReportingStore(case.main,case.main_ensemble)
        self.assertIsNone(store.inventories({},reep_version=value['version'])['reep_index'])


if __name__=='__main__':unittest.main()
