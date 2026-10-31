from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from providers.reep import install,FORMAT,_digest
from tools.review_imports import retry_identity_matches


class IdentityRetryTests(unittest.TestCase):
    def test_only_complete_proven_same_fixture_pairs_are_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows=[['team','sofascore','1','rt1'],['team','whoscored','10','rt1'],
                                ['team','sofascore','2','rt2'],['team','whoscored','20','rt2']]
            install(root,{'format':FORMAT,'version':_digest(rows),'rows':rows})
            comparison={'date':'2022-09-08','teams':{'home':{'id':10},'away':{'id':20}},
                'checkpoints':[{'provider':'sofascore','date':'2022-09-08','teams':{'home':{'id':'1'},'away':{'id':'2'}}}]}
            valid={'id':'accepted','error':'home team differs from the checkpoint','identity_comparison':comparison}
            wrong=deepcopy(valid);wrong['id']='wrong-team';wrong['identity_comparison']['teams']['away']['id']=10
            date=deepcopy(valid);date['id']='wrong-date';date['identity_comparison']['date']='2022-09-09'
            duplicate=deepcopy(valid);duplicate['id']='two-checkpoints';duplicate['identity_comparison']['checkpoints']*=2
            other=deepcopy(valid);other.update(id='other-error',error='events are missing')
            archive=Mock();archive.diagnostics.return_value={'quarantined_uploads':[valid,wrong,date,duplicate,other]}
            result=retry_identity_matches(root,archive)
            self.assertEqual(result['retried'],['accepted']);self.assertEqual(len(result['still_held']),4)
            archive.retry.assert_called_once_with('accepted')
