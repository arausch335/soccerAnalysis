from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import pandas as pd
from api import SofaScoreLoader, FotMobLoader, WhoScoredLoader, ProviderRequestError, FotMobClient
from api.whoScoredClient import WhoScoredClient, embedded_json
from api.identity import (IdentityResolver, IdentityError, canonical_player_name,
                          name_key, team_key)
from api.statistics import synthesize
from collection.selection import (season_key, season_label, competition_by_name,
                                  with_reviewed_schedule_results,
                                  is_qualification_event)
from collection.seasonRunner import SeasonRunner
from storage import PandasStore


def event(mid=1):
    return {'id':mid,'startTimestamp':1723894200+86400*(mid-1),'season':{'id':61627},
        'tournament':{'uniqueTournament':{'id':17}},'status':{'type':'finished'},
        'homeTeam':{'id':44,'name':'Liverpool FC'},'awayTeam':{'id':2,'name':'Other'},
        'homeScore':{'current':0},'awayScore':{'current':0}}


def lineup():
    return {'confirmed':True,**{side:{'players':[{'player':{'id':pid,'name':name,'position':'M'},
        'shirtNumber':7,'substitute':False,'statistics':{'minutesPlayed':90,'totalPass':10,'accuratePass':8}}]}
        for side,pid,name in [('home',7,'Player One'),('away',8,'Player Two')]}}


def fot_payload(mid=101):
    from datetime import datetime,timezone
    date=datetime.fromtimestamp(event(mid-100)['startTimestamp'],timezone.utc).isoformat()
    lineups={};players={}
    for side,tid,pid,name,team in [('home',501,77,'Player One','Liverpool'),('away',502,88,'Player Two','Other')]:
        lineups[side+'Team']={'id':tid,'name':team,'starters':[{'id':pid,'name':name,'shirtNumber':'7'}],'subs':[]}
        players[str(pid)]={'id':pid,'teamId':tid,'stats':[{'stats':{
            'Minutes':{'key':'minutes_played','stat':{'value':90}},
            'Passing':{'key':'accurate_passes','stat':{'value':8,'total':10}},
            'Dribbles':{'key':'dribbles_succeeded','stat':{'value':0,'total':0}}}}]}
    return {'general':{'matchId':str(mid),'finished':True,'matchTimeUTCDate':date},
        'header':{'teams':[{'id':501,'score':0},{'id':502,'score':0}]},
        'content':{'lineup':lineups,'playerStats':players}}


class Sofa:
    def __init__(self):self.count=2;self.calls=[]
    def seasons(self,cid):return {'seasons':[{'id':61627,'year':'24/25','name':'Premier League 24/25'}]}
    def iter_season_events(self,*args):return iter(event(i) for i in range(self.count,0,-1))
    def lineups(self,mid):self.calls.append(mid);return lineup()
    def average_positions(self,mid):return {'home':[],'away':[]}
    def player_heatmap(self,mid,pid):return {'heatmap':[{'x':10,'y':20}]}
    def player_rating_breakdown(self,mid,pid):return {'passes':[]}
    def shotmap(self,mid):return {'shotmap':[]}


class Fot:
    def __init__(self):self.calls=[];self.blocked=False
    def season_events(self,*args):
        from datetime import datetime,timezone
        return [{'id':str(i+100),'home':{'id':'501','name':'Liverpool'},'away':{'id':'502','name':'Other'},
                 'status':{'utcTime':datetime.fromtimestamp(event(i)['startTimestamp'],timezone.utc).isoformat(),
                           'finished':True,'scoreStr':'0 - 0'}} for i in (1,2,3)]
    def match_details(self,mid):
        self.calls.append(mid)
        if self.blocked:raise ProviderRequestError('Forbidden',status_code=403)
        return fot_payload(mid)


class ProviderTests(unittest.TestCase):
    def baseline(self):
        loader=SofaScoreLoader(None);season=loader.select_season(17,'Premier League',61627,'24/25')
        return loader._build_match(season,event(),lineup())

    def test_named_selection(self):
        for label in ('2024','2024/25','2024-2025','2024/2025'):
            self.assertEqual(season_key(label),'2024/2025')
            self.assertEqual(season_label(label),'2024-2025')
        self.assertEqual(season_label('Bundesliga 24/25'),'2024-2025')
        self.assertEqual(season_label('Premier League 2024/2025'),'2024-2025')
        self.assertEqual(competition_by_name('EPL').sofascore_id,17)
        self.assertEqual(competition_by_name('UCL').fotmob_id,42)
        self.assertEqual(competition_by_name('UCL').fotmob_supplemental_ids,())
        self.assertEqual(competition_by_name('UCL').whoscored_supplemental_paths,())
        self.assertEqual(competition_by_name('UEFA Europa League').sofascore_id,679)
        self.assertEqual(competition_by_name('UEL').fotmob_supplemental_ids,())
        self.assertEqual(competition_by_name('UEL').whoscored_supplemental_paths,())
        self.assertEqual(competition_by_name('League 1').name,'Ligue 1')
        self.assertEqual(team_key('Wolverhampton'),team_key('Wolves'))
        bundesliga_aliases=[
            ('1. FC Heidenheim','FC Heidenheim'),
            ('1. FC Union Berlin','Union Berlin'),
            ('1. FSV Mainz 05','Mainz 05'),
            ('Bayer 04 Leverkusen','Bayer Leverkusen'),
            ("Borussia M'gladbach",'Borussia Mönchengladbach'),
            ('FC Bayern München','Bayern Munich'),
            ('SC Freiburg','Freiburg'),
            ('SV Werder Bremen','Werder Bremen'),
            ('TSG Hoffenheim','Hoffenheim'),
            ('VfL Bochum 1848','Bochum'),
            ('VfL Wolfsburg','Wolfsburg'),
            ('VfB Stuttgart','Stuttgart'),
            ('RB Leipzig','RBL'),
            ('Borussia Dortmund','Dortmund'),
            ('Hamburger SV','Hamburg'),
            ('1. FC Köln','FC Koln'),
        ]
        for baseline,provider in bundesliga_aliases:
            with self.subTest(baseline=baseline,provider=provider):
                self.assertEqual(team_key(baseline),team_key(provider))
        new_team_aliases=[
            ('FC Bayern München','Bayern'),
            ('Bayer 04 Leverkusen','Leverkusen'),
            ('1. FSV Mainz 05','Mainz'),
            ('Atlético Madrid','Atletico'),
            ('UD Las Palmas','Las Palmas'),
            ('Levante UD','Levante'),
            ('AS Roma','Roma'),
            ('SSC Napoli','Napoli'),
            ('Hellas Verona','Verona'),
            ('Parma Calcio 1913','Parma'),
            ('AS Monaco','Monaco'),
            ('RC Lens','Lens'),
            ('RC Strasbourg','Strasbourg'),
            ('Stade de Reims','Reims'),
            ('Stade Brestois','Brest'),
            ('Stade Rennais','Rennes'),
            ('Olympique de Marseille','Marseille'),
            ('Olympique Lyonnais','Lyon'),
            ('Paris Saint-Germain','PSG'),
            ('Basel','FC Basel 1893'),
            ('Beşiktaş JK','Besiktas'),
            ('Bodø/Glimt','Bodoe/Glimt'),
            ('Club Brugge KV','Club Brugge'),
            ('Ferencváros TC','Ferencvaros'),
            ('GNK Dinamo Zagreb','Dinamo Zagreb'),
            ('IF Elfsborg','Elfsborg'),
            ('FC København','FC Copenhagen'),
            ('KRC Genk','Genk'),
            ('Ludogorets','Ludogorets Razgrad'),
            ('Malmö FF','Malmoe FF'),
            ('PAOK','PAOK Thessaloniki FC'),
            ('Red Bull Salzburg','Salzburg'),
            ('Royale Union Saint-Gilloise','Union St.Gilloise'),
            ('RSC Anderlecht','Anderlecht'),
            ('SK Brann','Brann'),
            ('SK Slavia Praha','Slavia Prague'),
            ('ŠK Slovan Bratislava','Slovan Bratislava'),
            ('SK Sturm Graz','Sturm Graz'),
            ('AC Sparta Praha','Sparta Prague'),
            ('Sporting Braga','Braga'),
            ('Sporting CP','Sporting'),
            ('BSC Young Boys','Young Boys'),
            ('Shakhtar Donetsk','Shakhtar'),
            ('Luton Town','Luton'),
            ('Sheffield United','Sheff Utd'),
            ('Darmstadt 98','Darmstadt'),
            ('VfL Bochum','Bochum'),
            ('US Salernitana 1919','Salernitana'),
            ('Sheriff Tiraspol','FC Sheriff'),
            ('Molde FK','Molde'),
            ('BK Häcken','Häcken'),
            ('BK Häcken','Haecken'),
            ('FK TSC Bačka Topola','TSC Backa Topola'),
            ('Darmstadt 98','SV Darmstadt'),
            ('Atlètic Club Escaldes','Atletic Escaldes'),
            ('FK Budućnost Podgorica','Buducnost Podgorica'),
            ('SP Tre Penne','Tre Penne Galazzano'),
            ('Breidablik Kópavogur','Breiðablik'),
            ('Breidablik Kópavogur','Breidablik'),
            ('HŠK Zrinjski Mostar','Zrinjski Mostar'),
            ('FK Žalgiris','Zalgiris Vilnius'),
            ('FC Struga Trim & Lum','FK Struga'),
            ('NK Olimpija Ljubljana','Olimpija Ljubljana'),
            ('Ħamrun Spartans FC','Hamrun Spartans'),
            ('Klaksvíkar Ítróttarfelag','Klaksvik'),
            ('KF Ballkani','Ballkani'),
            ('FK Partizani','Partizani'),
            ('The New Saints','TNS'),
            ('Zoria Luhansk','Zorya'),
            ('FK Čukarički','Cukaricki'),
        ]
        for baseline,provider in new_team_aliases:
            with self.subTest(baseline=baseline,provider=provider):
                self.assertEqual(team_key(baseline),team_key(provider))
        with self.assertRaises(ValueError):season_key('2024-2026')
        with self.assertRaises(ValueError):competition_by_name('Unknown competition')

    def test_whoscored_multi_stage_seasons(self):
        client=WhoScoredClient()
        client.get_text=Mock(return_value='''
            <select id="stages">
              <option value="/regions/250/tournaments/12/seasons/1/stages/100/show/name">League</option>
              <option value="/regions/250/tournaments/12/seasons/1/stages/200/show/name">Knockout</option>
            </select>
            <a href="/regions/250/tournaments/12/seasons/1/stages/100/fixtures">League again</a>
        ''')
        self.assertEqual(client.season_stages('/season'),[100,200])

    def test_european_fixture_discovery_excludes_qualification_competitions(self):
        from datetime import datetime,timezone

        def fotmob_row(mid):
            date=datetime.fromtimestamp(event()['startTimestamp'],timezone.utc).isoformat()
            return {'id':str(mid),'home':{'id':'1','name':'Home'},
                'away':{'id':'2','name':'Away'},
                'status':{'utcTime':date,'finished':True,'scoreStr':'1 - 0'}}

        class QualificationFot:
            def __init__(self):self.calls=[]
            def season_events(self,competition_id,season):
                self.calls.append((competition_id,season))
                return [fotmob_row(competition_id)]

        class QualificationWho:
            def __init__(self):self.season_calls=[];self.stage_calls=[]
            def seasons(self,path):
                self.season_calls.append(path)
                return [{'name':'2024/2025','path':path+'/seasons/1'}]
            def season_stages(self,path):
                stage=200 if '/tournaments/757/' in path else 100
                return [stage]
            def season_events(self,stage,month):
                self.stage_calls.append((stage,month))
                return [{'id':stage,'status':6,'startTimeUtc':'2024-08-17T11:30:00Z',
                    'homeTeamId':1,'homeTeamName':'Home','awayTeamId':2,
                    'awayTeamName':'Away','homeScore':1,'awayScore':0}]

        with tempfile.TemporaryDirectory() as folder:
            fot=QualificationFot();who=QualificationWho()
            clients={'sofascore':Sofa(),'fotmob':fot,'whoscored':who}
            runner=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            fixtures,errors=runner._fixtures(
                competition_by_name('UCL'),'2024/2025',[event()])
            self.assertEqual(errors,{})
            self.assertEqual({row['id'] for row in fixtures['fotmob']},{42})
            self.assertEqual({row['id'] for row in fixtures['whoscored']},{100})
            self.assertEqual(fot.calls,[(42,'2024/2025')])
            self.assertFalse(any('/tournaments/757/' in path for path in who.season_calls))

    def test_reviewed_player_name_aliases(self):
        aliases=[
            ('Timur Tuterov','Tymur Tutierov'),
            ('Linus Guther','Lins Guether'),
            ('Pacha','Alfonso Espino'),
            ('Iván Cuéllar','Pichu'),
            ('Rubén Iranzo','Rubo'),
            ('Alemão','Alexandre Zurawski'),
            ('Gilbert Jordana','Gibi'),
            ('Víctor Valdepeñas','Valde'),
            ('Rafael Bauzà','Rafel Bauza'),
            ('Javier Hernández','Javi Hernandez'),
            ('Rubi','Ruben Munoz'),
            ('Juan Brandariz','Chumi'),
            ('Mathias Jørgensen','Zanka'),
            ('Marezi','Marko Milovanović'),
            ('Pote','Pedro Gonçalves'),
            ('Samuel Omorodion','Samu Aghehowa'),
            ('Vanailson','Vaná'),
            ('Vladyslav Kochergin','Wladyslaw Kocherhin'),
            ('Shareef Keouf','Sharif Kaiuf'),
            ('Ilai Hagag','Ilay Hajaj'),
            ("Diaa Sabi'a",'Dia Saba'),
            ('Vitor Oliveira','Vitinha'),
            ('Tiago Ferreira','Mamede'),
            ('Shahrudin Mahammadaliyev','Shakhrudin Magomedaliyev'),
            ('Sergey Volkov','Siarhei Volkau'),
            ('Sergiy Gorbunov','Serhiy Horbunov'),
            ('Jacob Barrett Laursen','Jacob Laursen'),
            ('Pedrinho','Pedro Naressi'),
            ('Musa Al-Taamari','Mousa Tamari'),
            ('Souleymane Touré','Isaak Touré'),
            ('Elie Kroupi','Eli Junior Kroupi'),
            ('Igor Carioca','Igor Silva'),
            ('Bəhlul Mustafazadə','Bahlul Mustafazada'),
            ('Eric Bille','Yves Erick Bile'),
            ('Neophytos Michael','Neofytos Michail'),
            ('Nəriman Akhundzada','Nariman Akhundzade'),
            ('Kouadio Koné','Manu Koné'),
            ('Koka','Ahmed Hassan'),
            ('Vitalii Buialskyi','Vitaliy Buyalskiy'),
            ('Charalambos Lykogiannis','Charalampos Lykogiannis'),
            ('Pablo Cuñat','Pablo Cunat Campos'),
            ('Joshua King','Josh King'),
            ('Luka Đurić','Luka Duric'),
            ('Luka Đurić','Luka Djuric'),
        ]
        for baseline,provider in aliases:
            with self.subTest(baseline=baseline,provider=provider):
                self.assertEqual(name_key(baseline),name_key(provider))
        self.assertEqual(canonical_player_name('Michal Skoras'),'Michał Skóraś')
        self.assertEqual(canonical_player_name('Pablo Cunat Campos'),'Pablo Cuñat')
        self.assertEqual(canonical_player_name('Josh King'),'Joshua King')
        self.assertEqual(canonical_player_name('Luka Duric'),'Luka Đurić')

    def test_reviewed_domestic_team_aliases(self):
        aliases=[
            ('SV 07 Elversberg','Elversberg'),('SC Paderborn 07','Paderborn'),
            ('Viking FK','Viking'),('Rodez AF','Rodez'),
            ('USL Dunkerque','Dunkerque'),('Red Star FC','Red Star'),
            ('Coventry City','Coventry'),('Hull City','Hull'),
            ('Deportivo de A Coruña','Deportivo La Coruna'),
            ('Real Racing Club','Racing Santander'),
        ]
        for baseline,provider in aliases:
            with self.subTest(baseline=baseline,provider=provider):
                self.assertEqual(team_key(baseline),team_key(provider))

    def test_reviewed_nantes_toulouse_result_uses_real_event_id(self):
        competition=competition_by_name('Ligue 1')
        events=with_reviewed_schedule_results(
            competition,'2025/2026',77356,[])
        self.assertEqual(len(events),1)
        reviewed=events[0]
        self.assertEqual(reviewed['id'],14061912)
        self.assertEqual(reviewed['homeTeam']['name'],'Nantes')
        self.assertEqual(reviewed['awayTeam']['name'],'Toulouse')
        self.assertEqual(reviewed['homeScore']['current'],0)
        self.assertEqual(reviewed['awayScore']['current'],0)
        self.assertTrue(reviewed['isAwarded'])
        self.assertEqual(reviewed['status']['type'],'finished')

    def test_only_preliminary_competition_matches_are_qualifiers(self):
        qualifier={'tournament':{'qualificationOrPreliminary':True}}
        league_phase={'tournament':{'name':'UEFA Champions League'}}
        self.assertTrue(is_qualification_event(qualifier))
        self.assertFalse(is_qualification_event(league_phase))

    def test_pair_integrity_missing_and_unused_subs(self):
        available={'fotmob':{'metrics':{'passes_attempted':12,'passes_completed':9}}}
        result=synthesize({'totalPass':10},available,True)
        self.assertEqual(result['actions']['passes'].attempts,12)
        self.assertEqual(result['actions']['passes'].completions,9)
        result=synthesize({}, {'fotmob':{'metrics':{'dribbles_attempted':0,'dribbles_completed':0}}},True)
        self.assertIsNone(result['actions']['dribbles'].rate)
        self.assertTrue(all(v is None for v in synthesize({},available,False)['metrics'].values()))

    def test_sofascore_rating_progression_and_goalkeeper_fields(self):
        raw={'rating':8.1,'keeperSaveValue':1.25,'totalKeeperSweeper':4,'accurateKeeperSweeper':3,
             'totalProgression':112.5,'bestBallCarryProgression':19.2,
             'penaltyFaced':2,'crossNotClaimed':1}
        result=synthesize(raw,{},True)
        self.assertEqual(result['metrics']['sofascore_rating'],8.1)
        self.assertEqual(result['metrics']['sofascore_keeper_save_value'],1.25)
        self.assertEqual(result['metrics']['sofascore_total_progression'],112.5)
        self.assertEqual(result['metrics']['sofascore_best_ball_carry_progression'],19.2)
        self.assertEqual(result['metrics']['penalties_faced'],2)
        self.assertEqual(result['metrics']['crosses_not_claimed'],1)
        self.assertEqual(result['metrics']['keeper_sweeper_actions_attempted'],4)
        self.assertEqual(result['metrics']['keeper_sweeper_actions_completed'],3)


    def test_enrichment_is_atomic_and_preserves_sofa_baseline(self):
        match=self.baseline();bad=fot_payload()
        bad['content']['lineup']['awayTeam']['starters'][0]['name']='Someone Else'
        before=deepcopy(match.home_lineup.players[0].stats.raw)
        with self.assertRaises(IdentityError):FotMobLoader().enrich(match,bad)
        self.assertEqual(match.home_lineup.players[0].stats.raw,before)
        FotMobLoader().enrich(match,fot_payload())
        self.assertEqual(match.home_lineup.players[0].stats.raw['accuratePass'],8)
        self.assertEqual(match.home_lineup.players[0].stats.raw['_combined_sources']['passes_completed'],'sofascore')

    def test_explicit_id_override_ignores_shirt_conflict(self):
        match=self.baseline();resolver=IdentityResolver({'fotmob':{'77':7}})
        entry={'id':77,'name':'Nickname','shirt_number':99}
        player,method=resolver.player('fotmob',entry,match.home_lineup.players)
        self.assertIs(player,match.home_lineup.players[0])
        self.assertEqual(method,'explicit_id_mapping_with_shirt_conflict_ignored')

    def test_reviewed_provider_id_pair_can_cross_a_known_shirt_conflict(self):
        match=self.baseline();appearance=match.home_lineup.players[0]
        appearance.player.id=1466124;appearance.player.name='Adam Aznou';appearance.shirt_number=49
        entry={'id':1573563,'name':'Adam Aznou','shirt_number':48}
        player,method=IdentityResolver().player('fotmob',entry,match.home_lineup.players)
        self.assertIs(player,appearance)
        self.assertEqual(method,'name_or_reviewed_alias_with_shirt_conflict_ignored')

    def test_reviewed_match_correction_is_not_a_global_player_alias(self):
        match=self.baseline();appearance=match.home_lineup.players[0]
        appearance.player.id=134778;appearance.player.name='Alexander Schwolow';appearance.shirt_number=37
        entry={'id':186557,'name':'Frederik Rønnow','shirt_number':1}
        player,method=IdentityResolver().player(
            'fotmob',entry,match.home_lineup.players,provider_match_id=4534671)
        self.assertIs(player,appearance)
        self.assertEqual(method,'reviewed_match_id_mapping_with_shirt_conflict_ignored')
        with self.assertRaises(IdentityError):
            IdentityResolver().player('fotmob',entry,match.home_lineup.players)

    def test_reviewed_provider_id_disambiguates_pepe_and_pepe(self):
        match=self.baseline();appearance=match.home_lineup.players[0]
        appearance.player.id=5884;appearance.player.name='Pepe';appearance.shirt_number=3
        entry={'id':25921,'name':'Pepe','shirt_number':3}
        player,method=IdentityResolver().player('fotmob',entry,match.home_lineup.players)
        self.assertIs(player,appearance)
        self.assertEqual(method,'reviewed_provider_id_mapping')
        entry['shirt_number']=4
        player,method=IdentityResolver().player('fotmob',entry,match.home_lineup.players)
        self.assertIs(player,appearance)
        self.assertEqual(method,'reviewed_provider_id_mapping_with_shirt_conflict_ignored')

    def test_reviewed_duplicate_provider_profile_maps_to_shavy_babicka(self):
        match=self.baseline();appearance=match.home_lineup.players[0]
        appearance.player.id=1136280;appearance.player.name='Shavy Babicka'
        entry={'id':1290353,'name':'Warren Shavy','shirt_number':80}
        player,method=IdentityResolver().player('fotmob',entry,match.home_lineup.players)
        self.assertIs(player,appearance)
        self.assertEqual(method,'reviewed_provider_id_mapping_with_shirt_conflict_ignored')

    def test_rejected_shared_token_links_do_not_match_different_players(self):
        match=self.baseline();appearance=match.home_lineup.players[0]
        cases=[
            (1191206,'César Fernández',37,1197006,'Álvaro Fernández'),
            (1133106,'Daniel Toula',53,1152741,'Adam Toula'),
        ]
        for baseline_id,baseline_name,shirt,provider_id,provider_name in cases:
            with self.subTest(baseline_name=baseline_name):
                appearance.player.id=baseline_id;appearance.player.name=baseline_name
                appearance.shirt_number=shirt
                entry={'id':provider_id,'name':provider_name,'shirt_number':shirt}
                with self.assertRaises(IdentityError):
                    IdentityResolver().player('fotmob',entry,match.home_lineup.players)

    def test_unused_provider_bench_difference_is_non_blocking(self):
        match=self.baseline();payload=fot_payload()
        payload['content']['lineup']['homeTeam']['subs']=[
            {'id':999,'name':'Unused Difference','shirtNumber':'99'}]
        FotMobLoader().enrich(match,payload,allow_partial=True)
        self.assertEqual(match.metadata['identity_mismatches']['fotmob'],[])
        differences=match.metadata['lineup_differences']['fotmob']
        self.assertEqual(len(differences),1)
        self.assertIn('unused provider substitute',differences[0]['issue'])

    def test_savinho_alias_matches_savio(self):
        match=self.baseline()
        match.home_lineup.players[0].player.name='Sávio'
        entry={'id':77,'name':'Savinho','shirt_number':7}
        player,method=IdentityResolver().player('fotmob',entry,match.home_lineup.players)
        self.assertEqual(player.player.name,'Sávio')
        self.assertEqual(method,'name_or_reviewed_alias')

    def test_enrichment_ignores_shirt_conflict_for_unique_player(self):
        match=self.baseline();payload=fot_payload()
        payload['content']['lineup']['awayTeam']['starters'][0]['shirtNumber']='99'
        FotMobLoader().enrich(match,payload,allow_partial=True)
        self.assertIn('fotmob',match.home_lineup.players[0].stats.raw['_providers'])
        provider=match.away_lineup.players[0].stats.raw['_providers']['fotmob']
        self.assertEqual(provider['identity_method'],
                         'name_or_reviewed_alias_with_shirt_conflict_ignored')
        self.assertEqual(match.metadata['identity_mismatches']['fotmob'],[])

    def test_whoscored_event_flags_aliases_and_missing_categories(self):
        types={'passThroughBallAccurate':129,'passThroughBallInaccurate':130,'passThroughBallInacurate':131,'subOn':213}
        p={'match_id':11,'matchCentreEventTypeJson':types,'matchCentreData':{
            'statusCode':6,'startDate':'2024-08-17','events':[
                {'playerId':77,'teamId':501,'satisfiedEventsTypes':[129]},
                {'playerId':77,'teamId':501,'satisfiedEventsTypes':[130,131]}],
            **{side:{'teamId':tid,'name':team,'scores':{'fulltime':0},'players':[
                {'playerId':pid,'name':name,'shirtNo':7,'isFirstEleven':True}], 'formations':[]}
               for side,tid,pid,name,team in [('home',501,77,'Player One','Liverpool'),('away',502,88,'Player Two','Other')]}}}
        decoded=WhoScoredLoader().decode(p,event())
        stats=decoded['home']['players'][0]['metrics']
        self.assertEqual(stats['through_balls_attempted'],2)
        self.assertEqual(stats['through_balls_completed'],1)
        self.assertIsNone(stats['shots'])
        loader=WhoScoredLoader();season=loader.select_season(2,'Premier League',10316,'2024/2025')
        loaded=loader.load_match(p,season,event())
        self.assertTrue(loaded.home_lineup.players[0].did_play)
        self.assertIsNone(loaded.home_lineup.players[0].minutes_played)
        del types['passThroughBallInaccurate'];del types['passThroughBallInacurate']
        self.assertIsNone(WhoScoredLoader().decode(p,event())['home']['players'][0]['metrics']['through_balls_attempted'])

    def test_page_json_is_decoded_without_executing_code(self):
        self.assertEqual(embedded_json('matchCentreData: {"x": "}; evil()"}, after: 1','matchCentreData'),{'x':'}; evil()'})
        with self.assertRaises(Exception):embedded_json('matchCentreData: dangerousFunction()','matchCentreData')

    def test_whoscored_match_teams_reads_current_header_without_lineups(self):
        client=WhoScoredClient(session=Mock(),min_delay=0,max_delay=0)
        client.get_text=Mock(return_value="""
            require.config.params['matchheader'] = {
                input: [724,2121,'Omonia Nicosia','Wolfsberger AC','28/08/2025'],
                matchId: 1943941
            };
        """)
        teams=client.match_teams(1943941)
        self.assertEqual(teams['home'],{'id':724,'name':'Omonia Nicosia'})
        self.assertEqual(teams['away'],{'id':2121,'name':'Wolfsberger AC'})
        with self.assertRaisesRegex(Exception,'different or incomplete'):
            client.match_teams(1943942)

    def test_fotmob_client_rejects_wrong_season_and_match(self):
        client=FotMobClient(session=Mock(),min_delay=0,max_delay=0)
        client.get=Mock(return_value={'allAvailableSeasons':['2024/2025'],'overview':{'selectedSeason':'2025/2026'}})
        with self.assertRaises(Exception):client.season_events(47,'2024/2025')
        client.get.return_value={'general':{'matchId':'999'}}
        with self.assertRaises(Exception):client.match_details(1)

    def test_standalone_fotmob_loader(self):
        loader=FotMobLoader();season=loader.select_season(47,'Premier League',2024,'2024/2025')
        match=loader.load_match(fot_payload(),season)
        self.assertEqual(match.id,101)
        self.assertIs(loader.dataset.players[77],match.home_lineup.players[0].player)
        self.assertEqual(match.home_lineup.players[0].stats.actions['passes'].rate,.8)

    def test_newer_fotmob_stats_are_retained_when_present(self):
        payload=fot_payload();stats=payload['content']['playerStats']['77']['stats'][0]['stats']
        stats.update({
            'Line breaking passes':{'key':'line_breaking_passes','stat':{'value':9}},
            'Top speed':{'key':'physical_metrics_topspeed','stat':{'value':32.4}},
            'Distance':{'key':'physical_metrics_distance_covered','stat':{'value':9876}},
        })
        loader=FotMobLoader();season=loader.select_season(47,'Premier League',2024,'2024/2025')
        player=loader.load_match(payload,season).home_lineup.players[0]
        self.assertEqual(player.stats.raw['_providers']['fotmob']['raw']['line_breaking_passes']['value'],9)
        self.assertEqual(player.stats.raw['_combined']['line_breaking_passes'],9)
        self.assertEqual(player.stats.raw['_combined']['top_speed_kmh'],32.4)
        self.assertEqual(player.stats.raw['_combined']['distance_covered_m'],9876)

    def test_resume_uses_cached_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();fot=Fot();clients={'sofascore':sofa,'fotmob':fot}
            runner=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            self.assertTrue(runner.run('Premier League','2024').status.eq('complete').all())
            self.assertEqual(sofa.calls,[1,2]);self.assertEqual(fot.calls,[101,102])
            sofa.count=3
            resumed=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            self.assertTrue(resumed.run('EPL','2024/25').status.eq('complete').all())
            self.assertEqual(sofa.calls,[1,2]);self.assertEqual(fot.calls,[101,102])
            self.assertIs(resumed.dataset.matches[1].home_team,resumed.dataset.matches[2].home_team)

    def test_collection_can_skip_combined_csv_build(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();sofa.count=1
            runner=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],
                                spatial=(),report=lambda _:None)
            runner._export=Mock()
            result=runner.run('EPL','2024',build_csvs=False)
            self.assertTrue(result.status.eq('complete').all())
            runner._export.assert_not_called()
            self.assertFalse(json.loads(Path(folder,'run_summary.json').read_text())['csvs_built'])
            self.assertTrue(Path(folder,'match_checkpoints','1').exists())

    def test_awarded_match_stays_in_snapshot_without_invented_player_statistics(self):
        class AwardedSofa(Sofa):
            def iter_season_events(self,*args):
                normal=event(1);normal['roundInfo']={'round':13}
                awarded=event(2);awarded.update(isAwarded=True,roundInfo={'round':14})
                awarded['homeTeam']={'id':55,'name':'Union Berlin'}
                awarded['awayTeam']={'id':66,'name':'Bochum'}
                awarded['homeScore']={'current':1,'display':0,'normaltime':0}
                awarded['awayScore']={'current':1,'display':2,'normaltime':2}
                return iter((normal,awarded))
            def lineups(self,mid):
                if mid==2:raise ProviderRequestError('HTTP 404',status_code=404)
                return super().lineups(mid)

        with tempfile.TemporaryDirectory() as folder:
            result=SeasonRunner({'sofascore':AwardedSofa()},folder,providers=['sofascore'],
                spatial=(),report=lambda _:None).run('EPL','2024')
            self.assertEqual(result.status.tolist(),['complete','failed'])
            matches=PandasStore(Path(folder,'dataset')).from_csv().matches
            self.assertEqual((matches[2].home_score,matches[2].away_score),(0,2))
            self.assertEqual(matches[2].round_number,14)
            self.assertIsNone(matches[2].home_lineup)
            players=pd.read_csv(Path(folder,'player_match_stats.csv'))
            self.assertNotIn(2,set(players.match_id))
            self.assertFalse(Path(folder,'team_match_results.csv').exists())
            self.assertFalse(Path(folder,'fotmob_team_statistics.csv').exists())

    def test_fotmob_only_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            fot=Fot()
            with self.assertRaisesRegex(ValueError,'FotMob-only backup'):
                SeasonRunner({'fotmob':fot},folder,providers=['fotmob'],baseline_provider='fotmob')
            self.assertEqual(fot.calls,[])

    def test_provider_block_preserves_baseline_and_retries_only_missing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();fot=Fot();fot.blocked=True;clients={'sofascore':sofa,'fotmob':fot}
            runner=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            self.assertEqual(runner.run('EPL','2024').status.tolist(),['partial','partial'])
            self.assertEqual(fot.calls,[101]);self.assertEqual(sofa.calls,[1,2])
            fot.blocked=False
            resumed=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            self.assertTrue(resumed.run('EPL','2024').status.eq('complete').all())
            self.assertEqual(sofa.calls,[1,2]);self.assertEqual(fot.calls,[101,101,102])

    def test_sofascore_spatial_403_saves_checkpoint_and_stops_season(self):
        class BlockedSpatial(Sofa):
            def average_positions(self,mid):
                raise ProviderRequestError('HTTP 403 (challenge)',status_code=403)

        with tempfile.TemporaryDirectory() as folder:
            sofa=BlockedSpatial()
            reports=[]
            result=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],
                                report=reports.append).run('EPL','2024')
            self.assertEqual(result.status.tolist(),['partial','pending'])
            self.assertTrue(result.attrs['sofascore_blocked'])
            self.assertTrue(Path(folder,'match_checkpoints','1').exists())
            self.assertFalse(Path(folder,'match_checkpoints','2').exists())
            self.assertTrue(any('saved the current checkpoint' in line for line in reports))

    def test_bad_output_scope_cannot_mix_seasons(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'config.json').write_text('{"other":"season"}')
            with self.assertRaisesRegex(ValueError,'another season'):
                SeasonRunner({'sofascore':Sofa()},folder,providers=['sofascore']).run('EPL','2024')

    def test_spatial_responses_are_cached_and_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();sofa.count=1
            reports=[]
            runner=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],report=reports.append)
            self.assertTrue(runner.run('EPL','2024').status.eq('complete').all())
            self.assertTrue(any(line.startswith('[export 1/5]') for line in reports))
            self.assertTrue(any(line.startswith('[export complete]') for line in reports))
            raw=list(Path(folder,'raw','sofascore').glob('*.json'))
            keys={json.loads(path.read_text())['key'] for path in raw}
            self.assertIn('average_positions/1',keys)
            self.assertIn('shotmap/1',keys)
            self.assertEqual(sum(key.startswith('heatmap/1/') for key in keys),2)
            self.assertEqual(sum(key.startswith('rating_breakdown/1/') for key in keys),2)
            for name in ('player_match_stats.csv','identities.csv','identity_mismatches.csv','lineup_differences.csv'):
                self.assertTrue(Path(folder,name).exists())
            self.assertFalse(Path(folder,'events.csv').exists())
            self.assertFalse(Path(folder,'whoscored_events.csv').exists())
            master=pd.read_csv(Path(folder,'player_match_stats.csv'))
            for column in ('sofascore_rating','sofascore_keeper_save_value','sofascore_total_progression',
                           'sofascore_best_ball_carry_progression','penalties_faced','crosses_not_claimed',
                           'keeper_sweeper_actions_attempted','keeper_sweeper_actions_completed',
                           'touch_count','heatmap_xy'):
                self.assertIn(column,master.columns)
            for column in ('average_signed_width','spatial_entropy','dominant_zone_number',
                           'average_pass_length_m','dribble_location_variability','fotmob_shot_xg_sum'):
                self.assertNotIn(column,master.columns)
            for name in ('spatial_features.csv','pass_features.csv','dribble_features.csv',
                         'pass_zone_transitions.csv','shot_features.csv','player_match_features.csv',
                         'player_stats.csv','spatial_maps.csv','passes.csv','dribbles.csv',
                         'average_positions.csv','fotmob_player_stats.csv','fotmob_team_statistics.csv'):
                self.assertFalse(Path(folder,name).exists())
            self.assertEqual(master.touch_count.tolist(),[1,1])
            self.assertEqual(json.loads(master.heatmap_xy.iloc[0]),[{'x':10,'y':20}])
            self.assertFalse(Path(folder,'touches.csv').exists())
            self.assertEqual(len(master),2)
            self.assertEqual(len(master.columns),len(set(master.columns)))
            for csv_path in Path(folder).glob('*.csv'):
                exported=pd.read_csv(csv_path)
                self.assertIn('competition',exported.columns,csv_path.name)
                if not exported.empty:
                    self.assertEqual(set(exported.competition),{'Premier League'},csv_path.name)
            before=len(raw)
            SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],report=lambda _:None).run('EPL','2024')
            self.assertEqual(len(list(Path(folder,'raw','sofascore').glob('*.json'))),before)

    def test_switching_fast_to_all_backfills_only_missing_spatial_data(self):
        class TrackingSofa(Sofa):
            def __init__(self):
                super().__init__();self.count=1;self.spatial_calls=[]
            def average_positions(self,mid):
                self.spatial_calls.append(('average_positions',mid));return super().average_positions(mid)
            def shotmap(self,mid):
                self.spatial_calls.append(('shotmap',mid));return super().shotmap(mid)
            def player_heatmap(self,mid,pid):
                self.spatial_calls.append(('heatmap',mid,pid));return super().player_heatmap(mid,pid)
            def player_rating_breakdown(self,mid,pid):
                self.spatial_calls.append(('rating_breakdown',mid,pid));return super().player_rating_breakdown(mid,pid)

        with tempfile.TemporaryDirectory() as folder:
            sofa=TrackingSofa();clients={'sofascore':sofa}
            fast=SeasonRunner(clients,folder,providers=clients,spatial=('average_positions',),report=lambda _:None)
            self.assertTrue(fast.run('EPL','2024').status.eq('complete').all())
            self.assertEqual(sofa.spatial_calls,[('average_positions',1)])
            all_spatial=SeasonRunner(clients,folder,providers=clients,report=lambda _:None)
            self.assertTrue(all_spatial.run('EPL','2024').status.eq('complete').all())
            self.assertEqual(sofa.spatial_calls.count(('average_positions',1)),1)
            self.assertIn(('shotmap',1),sofa.spatial_calls)
            self.assertEqual(sum(call[0]=='heatmap' for call in sofa.spatial_calls),2)
            self.assertEqual(sum(call[0]=='rating_breakdown' for call in sofa.spatial_calls),2)

    def test_missing_rating_breakdown_is_probed_once_per_team(self):
        class MissingRatingBreakdown(Sofa):
            def __init__(self):
                super().__init__();self.rating_calls=[]
            def player_rating_breakdown(self,mid,pid):
                self.rating_calls.append(pid)
                raise ProviderRequestError('missing',status_code=404)

        with tempfile.TemporaryDirectory() as folder:
            sofa=MissingRatingBreakdown();match=self.baseline()
            # Add extra played appearances to prove they are skipped after the
            # two representative starters establish match-level unavailability.
            for lineup_ in match.lineups:
                original=lineup_.players[0]
                for offset in (100,200):
                    clone=deepcopy(original);clone.player.id+=offset
                    lineup_.players.append(clone)
            runner=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],
                                spatial=('rating_breakdown',),report=lambda _:None)
            self.assertEqual(runner._collect_spatial(match,event(),sofa),[])
            self.assertEqual(len(sofa.rating_calls),2)
            self.assertEqual(set(sofa.rating_calls),{7,8})
            statuses=match.metadata['sofascore_spatial_status']
            self.assertEqual(statuses['_capabilities']['rating_breakdown'],'unavailable')
            self.assertTrue(all(statuses[str(p.player.id)]['rating_breakdown']=='unavailable'
                                for lineup_ in match.lineups for p in lineup_.players))





if __name__=='__main__':unittest.main()
