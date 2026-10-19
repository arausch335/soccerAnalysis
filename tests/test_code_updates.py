import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from collection.codeUpdates import CodeUpdates


class CodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.remote=base/'remote.git';self.main=base/'main';self.worker=base/'worker'
        self.git(base,'init','--bare',str(self.remote));self.git(base,'clone',str(self.remote),str(self.main))
        self.git(self.main,'checkout','-b','main')
        for root in (self.main,):self.configure(root)
        (self.main/'config').mkdir();(self.main/'config/settings.yaml').write_text('shared: true\n')
        (self.main/'.gitignore').write_text('data/\n')
        self.commit(self.main,'Initial');self.git(self.main,'push','-u','origin','main')
        self.git(base,'clone','-b','main',str(self.remote),str(self.worker));self.configure(self.worker)
        self.sync=CodeUpdates(self.worker,'msi',{'enabled':True,'remote':'origin','branch':'main','poll_seconds':30,'auto_restart':True})

    def git(self,root,*args):
        result=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,check=True)
        return result.stdout.strip()

    def configure(self,root):
        self.git(root,'config','user.name','Test');self.git(root,'config','user.email','test@example.invalid')

    def commit(self,root,message):
        self.git(root,'add','.');self.git(root,'commit','-m',message)

    def remote_change(self):
        (self.main/'main.py').write_text('# Updated code\n');self.commit(self.main,'Code update');self.git(self.main,'push')

    def test_fetch_does_not_change_running_code_and_boundary_fast_forwards_without_push(self):
        self.remote_change();before=self.git(self.worker,'rev-parse','HEAD')
        self.sync.fetch();self.assertEqual(self.git(self.worker,'rev-parse','HEAD'),before)
        commands=[];original=self.sync.git
        def record(*args,**kwargs):commands.append(args[0]);return original(*args,**kwargs)
        with patch.object(self.sync,'git',side_effect=record):changed=self.sync.boundary()
        self.assertIn('main.py',changed)
        self.assertNotIn('push',commands);self.assertNotIn('commit',commands);self.assertNotIn('rebase',commands)
        self.assertEqual(self.git(self.worker,'rev-parse','HEAD'),self.git(self.remote,'rev-parse','main'))

    def test_local_settings_and_staged_changes_pause_without_overwriting(self):
        self.remote_change();path=self.worker/'config/settings.yaml';path.write_text('local: keep me')
        self.sync.fetch();self.assertEqual(self.sync.boundary(),[])
        self.assertEqual(path.read_text(),'local: keep me');self.assertIn('Local edits',self.sync.status['message'])
        self.git(self.worker,'add','config/settings.yaml');self.assertEqual(self.sync.boundary(),[])
        self.assertEqual(self.git(self.worker,'diff','--cached','--name-only'),'config/settings.yaml')

    def test_local_commits_are_never_pushed_or_rebased(self):
        (self.worker/'local.py').write_text('# local')
        self.commit(self.worker,'Keep local code')
        before=self.git(self.remote,'rev-parse','main');self.sync.fetch()
        self.assertEqual(self.sync.boundary(),[]);self.assertIn('Local commits',self.sync.status['message'])
        self.assertEqual(self.git(self.remote,'rev-parse','main'),before)
        self.assertEqual(self.git(self.worker,'log','-1','--format=%s'),'Keep local code')

    def test_ignored_worker_progress_does_not_block_code_updates(self):
        self.remote_change();path=self.worker/'data/worker_runtime/msi.json'
        path.parent.mkdir(parents=True);path.write_text('local progress')
        self.sync.fetch();self.assertIn('main.py',self.sync.boundary())
        self.assertEqual(path.read_text(),'local progress')
