from copy import deepcopy
import unittest
from config.workerPlan import load_plan
from dashboard.coverage import build_grid
from dashboard.model import resource_states

class CoverageGridTests(unittest.TestCase):
    def setUp(self):
        self.plan=load_plan();self.workers={k:{**v,'runtime':None} for k,v in self.plan.workers.items()}
    def row(self,mid,owner,resource='sofascore_stats',state='complete',namespace='sofascore'):
        return {'competition':'Premier League','season':'2021-2022','namespace':namespace,'match_id':str(mid),
                'origins':[owner],'resources':{resource:state}}
    def build(self,rows,total=4):
        return build_grid(rows,self.plan,lambda *args:set(map(str,range(total))),self.workers)
    def cell(self,grid,category='players',row='sofa'):
        comp=next(c for c in grid['competitions'] if c['competition']=='Premier League')
        cat=next(c for c in comp['categories'] if c['id']==category)
        return next(r for r in cat['rows'] if r['id']==row)['cells']['2021-2022']
    def test_mixed_worker_colors_and_planned_remainder_without_duplicate_copies(self):
        first=self.row(1,'main');copy=deepcopy(first)
        rows=[first,copy,self.row(2,'msi')]
        cell=self.cell(self.build(rows))
        self.assertEqual(cell['complete'],{'main':1,'msi':1})
        self.assertEqual(cell['planned'],{'msi':2});self.assertEqual(cell['unplanned'],0)
    def test_unplanned_missing_distinct_from_unavailable_and_unverified(self):
        rows=[self.row(1,'main','heatmaps','unavailable'),self.row(2,'main','heatmaps','unknown')]
        comp=next(c for c in self.build(rows)['competitions'] if c['competition']=='Premier League')
        maprow=next(c for c in comp['categories'] if c['id']=='spatial')['rows'][0]
        heat=next(r for r in maprow['details'] if r['id']=='heatmaps')['cells']['2021-2022']
        self.assertEqual(heat['unavailable'],1);self.assertEqual(heat['verification'],1);self.assertEqual(heat['unplanned'],2)
    def test_fotmob_native_and_combined_data_are_counted_once_when_linked(self):
        combined=self.row(1,'msi','fotmob_enrichment');combined['provider_match_ids']={'fotmob':'99'}
        native=self.row(99,'msi','fotmob_stats',namespace='fotmob_players')
        cell=self.cell(self.build([combined,native]),row='fotmob')
        self.assertEqual(sum(cell['complete'].values()),1)
    def test_resource_credit_preserved_separately_from_match_origins(self):
        row=self.row(1,'main');row['origins']=['main','msi'];row['resource_origins']={'sofascore_stats':['msi']}
        self.assertEqual(self.cell(self.build([row]))['complete'],{'msi':1})
        del row['resource_origins']
        self.assertEqual(self.cell(self.build([row]))['complete'],{'_mixed':1})
    def test_remote_schedule_supplies_target_and_season_columns_are_continuous(self):
        self.workers['msi']['runtime']={'schedules':{'a':{'competition':'Premier League','season':'2021-2022','kind':'combined','match_ids':['1','2','3']}}}
        grid=build_grid([self.row(1,'msi')],self.plan,lambda *args:None,self.workers)
        self.assertEqual(self.cell(grid)['total'],3)
        self.assertEqual(grid['seasons'],['2021-2022','2022-2023'])

    def test_undiscovered_assignment_keeps_planned_allocation_without_inventing_matches(self):
        grid=build_grid([],self.plan,lambda *args:None,self.workers)
        cell=self.cell(grid)
        self.assertEqual(cell['total'],0);self.assertEqual(cell['allocation'],{'msi':1})
        self.assertTrue(cell['unknown_target'])

    def test_unlinked_native_fotmob_does_not_claim_a_second_completed_match(self):
        rows=[self.row(1,'msi','fotmob_enrichment'),self.row(99,'main','fotmob_stats',namespace='fotmob_players')]
        cell=self.cell(self.build(rows,total=1),row='fotmob')
        self.assertEqual(sum(cell['complete'].values()),1)
        self.assertEqual(cell['verification'],1);self.assertTrue(cell['unknown_target'])
