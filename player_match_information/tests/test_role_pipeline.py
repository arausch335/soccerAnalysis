import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pandas as pd
from role_pipeline.events import zone_codes
from role_pipeline.profiles import contributions
from role_pipeline.positions import position_usage
from role_pipeline.comparison import transform,compare
from role_pipeline.config import Config

class AggregationTests(unittest.TestCase):
    def test_rates_use_exposure_and_missing_is_not_zero(self):
        df=pd.DataFrame({'minutes_played':[10.,90.,90.],'attempts':[2.,3.,np.nan]})
        spec=[{'feature':'attempts_per90','column':'attempts','kind':'count'}]
        n,d,c,v=contributions(df,spec)
        self.assertAlmostEqual(n.sum().iloc[0]/d.sum().iloc[0],4.5)
        self.assertEqual(c.sum().iloc[0],100)
        self.assertTrue(np.isnan(v.iloc[2,0]))
    def test_ratio_sums_counts_instead_of_averaging_percentages(self):
        df=pd.DataFrame({'minutes_played':[90.,90.,90.],'completed':[1.,0.,0.],'attempted':[1.,9.,0.]})
        spec=[{'feature':'ratio','kind':'ratio','numerator':'completed','denominator':'attempted'}]
        n,d,c,v=contributions(df,spec)
        self.assertAlmostEqual(n.sum().iloc[0]/d.sum().iloc[0],.1)
        self.assertTrue(np.isnan(v.iloc[2,0]))
        self.assertEqual(c.sum().iloc[0],270)
    def test_regions_at_edges_and_missing_coordinates(self):
        codes=zone_codes(pd.Series([0,100,100/3,200/3,-1,np.nan]),pd.Series([0,100,20,80,50,50]))
        self.assertEqual(codes.tolist(),[0,14,5,14,-1,-1])
    def test_imputation_does_not_mutate_observed_data(self):
        x=np.array([[1.,2.],[2.,np.nan],[4.,7.],[5.,9.]])
        original=x.copy();spec=[{'feature':'a','kind':'count','group':'passing'},{'feature':'b','kind':'count','group':'defending'}]
        model,params=transform(x,spec)
        np.testing.assert_equal(x,original)
        self.assertTrue(np.isfinite(model).all())
        self.assertEqual(params[1]['impute_median'],7.)
        self.assertAlmostEqual(np.var(model[:,0]),1.)

class ComparisonTests(unittest.TestCase):
    def test_too_few_peers_retains_profiles_without_forcing_clusters(self):
        df=pd.DataFrame({'profile_id':['a','b'],'competition':['test','test'],'season':['2024','2024'],
          'position_group':['Striker','Striker'],'eligible_comparison':[True,True],'attempts_per90':[2.,3.]})
        cov=pd.DataFrame({'profile_id':['a','b'],'feature':['attempts_per90','attempts_per90'],'minute_coverage':[1.,1.]})
        specs=[{'feature':'attempts_per90','candidate':True,'group':'attacking','kind':'count'}]
        profiles,tables,models=compare(df,cov,specs,{},Config())
        self.assertEqual(len(profiles),2)
        self.assertEqual(models,{})
        self.assertTrue(profiles.cluster_status.eq('insufficient peer sample').all())
        self.assertIn('profile_id',tables['nearest_players'].columns)
        self.assertIn('model_value',tables['role_model_matrix'].columns)

class PositionTests(unittest.TestCase):
    def test_substitute_inherits_recorded_formation_slot_not_profile_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            team={'teamId':10,'players':[{'playerId':1,'position':'DR','isFirstEleven':True},{'playerId':2,'position':'Sub','isFirstEleven':False}],
              'formations':[{'formationName':'test','startMinuteExpanded':0,'endMinuteExpanded':60,'playerIds':[1,2],'formationSlots':[2,0]},
                            {'formationName':'test','startMinuteExpanded':60,'endMinuteExpanded':90,'playerIds':[1,2],'formationSlots':[0,2]}]}
            away={'teamId':11,'players':[],'formations':[]}
            Path(tmp,'sample.json').write_text(json.dumps({'payload':{'match_id':99,'matchCentreData':{'home':team,'away':away}}}))
            identity=pd.DataFrame({'provider_match_id':[99],'match_id':[100]})
            result,stints,dictionary=position_usage(tmp,identity)
            sub=result[result.provider_player_id.eq(2)].iloc[0]
            self.assertEqual(sub.detailed_position,'Full-back / wing-back')
            self.assertEqual(sub.formation_span_minutes,30.)
            self.assertEqual(sub.dominant_position_share,1.)
            self.assertEqual(len(stints),2)
    def test_unknown_formation_slot_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            team={'teamId':10,'players':[{'playerId':2,'position':'Sub','isFirstEleven':False}],
              'formations':[{'formationName':'unknown','startMinuteExpanded':1,'endMinuteExpanded':30,'playerIds':[2],'formationSlots':[4]}]}
            Path(tmp,'sample.json').write_text(json.dumps({'payload':{'match_id':99,'matchCentreData':{'home':team,'away':{'teamId':11,'players':[],'formations':[]}}}}))
            result,_,_=position_usage(tmp,pd.DataFrame({'provider_match_id':[99],'match_id':[100]}))
            self.assertEqual(result.iloc[0].detailed_position,'Unknown')

if __name__=='__main__':unittest.main()
