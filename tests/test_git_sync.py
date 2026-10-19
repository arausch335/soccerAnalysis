import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from collection.gitSync import GitSync, SyncPaused


class GitSyncTests(unittest.TestCase):
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
        self.sync=GitSync(self.worker,'msi',{'enabled':True,'remote':'origin','branch':'main','poll_seconds':30,'publish_inventory':True,'auto_restart':True})

    def git(self,root,*args):
        result=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,check=True)
        return result.stdout.strip()

    def configure(self,root):
        self.git(root,'config','user.name','Test');self.git(root,'config','user.email','test@example.invalid')

    def commit(self,root,message):
        self.git(root,'add','.');self.git(root,'commit','-m',message)

    def remote_change(self):
        (self.main/'main.py').write_text('# Updated code\n');self.commit(self.main,'Code update');self.git(self.main,'push')

    def snapshot(self):
        path=self.worker/'ensemble_data/workers/msi.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'worker_id':'msi','records':{}}));return path

    def test_fetch_does_not_change_running_source_boundary_updates_and_publishes_only_own(self):
        self.remote_change();path=self.snapshot();original=path.read_bytes()
        self.sync.fetch();self.assertFalse((self.worker/'main.py').exists())
        self.assertEqual(self.sync.status['state'],'update_pending')
        changed=self.sync.boundary();self.assertIn('main.py',changed)
        self.assertEqual(path.read_bytes(),original)
        files=self.git(self.worker,'show','--format=','--name-only','HEAD')
        self.assertEqual(files,'ensemble_data/workers/msi.json')
        self.assertEqual(self.git(self.worker,'rev-parse','HEAD'),self.git(self.remote,'rev-parse','main'))

    def test_dirty_settings_and_staged_edits_are_preserved(self):
        self.remote_change();path=self.worker/'config/settings.yaml';path.write_text('local: keep me\n')
        self.sync.fetch();self.assertEqual(self.sync.boundary(),[])
        self.assertEqual(path.read_text(),'local: keep me\n');self.assertFalse((self.worker/'main.py').exists())
        self.assertIn('Local edits',self.sync.status['message'])
        self.git(self.worker,'add','config/settings.yaml');self.sync.boundary()
        self.assertEqual(self.git(self.worker,'diff','--cached','--name-only'),'config/settings.yaml')

    def test_untracked_source_and_local_source_commits_are_never_published(self):
        (self.worker/'secret.txt').write_text('keep')
        self.sync.fetch();self.sync.boundary();self.assertIn('Local edits',self.sync.status['message'])
        self.commit(self.worker,'User change');before=self.git(self.remote,'rev-parse','main')
        self.sync.boundary();self.assertIn('Local commits',self.sync.status['message'])
        self.assertEqual(before,self.git(self.remote,'rev-parse','main'))

    def test_failed_snapshot_push_can_rebase_later_without_losing_data(self):
        self.snapshot();self.sync.fetch();original=self.sync.git
        def offline(*args,**kwargs):
            if args[0]=='push':raise SyncPaused('Offline')
            return original(*args,**kwargs)
        with patch.object(self.sync,'git',side_effect=offline):self.sync.boundary()
        self.assertEqual(self.git(self.worker,'log','-1','--format=%s'),'Scraper snapshot: msi')
        self.remote_change();self.sync.fetch();changed=self.sync.boundary()
        self.assertIn('main.py',changed);self.assertEqual(self.sync.status['state'],'restart_pending')
        self.assertTrue((self.worker/'ensemble_data/workers/msi.json').exists())
        self.assertEqual(self.git(self.remote,'rev-parse','main'),self.git(self.worker,'rev-parse','HEAD'))

    def test_duplicate_worker_snapshot_change_pauses_instead_of_overwriting(self):
        remote_path=self.main/'ensemble_data/workers/msi.json';remote_path.parent.mkdir(parents=True)
        remote_path.write_text('another computer');self.commit(self.main,'Other identity');self.git(self.main,'push')
        local=self.snapshot();before=local.read_bytes();self.sync.fetch();self.sync.boundary()
        self.assertIn('unique worker ID',self.sync.status['message']);self.assertEqual(local.read_bytes(),before)


    def test_hourly_publication_keeps_running_code_head_and_index_unchanged(self):
        self.remote_change();path=self.snapshot()
        before_head=self.git(self.worker,'rev-parse','HEAD')
        before_index=self.git(self.worker,'write-tree')
        self.assertTrue(self.sync.publish(),self.sync.status)
        self.assertEqual(self.git(self.worker,'rev-parse','HEAD'),before_head)
        self.assertEqual(self.git(self.worker,'write-tree'),before_index)
        self.assertFalse((self.worker/'main.py').exists())
        published=self.git(self.remote,'rev-parse','main')
        paths=self.git(self.remote,'diff-tree','--no-commit-id','--name-only','-r',published)
        self.assertEqual(paths,'ensemble_data/workers/msi.json')
        # The next saved season boundary can safely consume this process's own publication.
        sync=GitSync(self.worker,'msi',self.sync.options)
        sync.fetch();changed=sync.boundary()
        self.assertIn('main.py',changed)
        self.assertTrue((self.worker/'main.py').exists())
        self.assertEqual(json.loads(path.read_text())['worker_id'],'msi')

    def test_publication_does_not_touch_staged_edits_or_publish_source(self):
        self.snapshot();settings=self.worker/'config/settings.yaml';settings.write_text('keep: local\n')
        self.git(self.worker,'add','config/settings.yaml')
        before=self.git(self.remote,'rev-parse','main')
        self.assertFalse(self.sync.publish())
        self.assertEqual(settings.read_text(),'keep: local\n')
        self.assertEqual(self.git(self.worker,'diff','--cached','--name-only'),'config/settings.yaml')
        self.assertEqual(before,self.git(self.remote,'rev-parse','main'))

    def test_two_workers_publish_without_overwriting_each_other(self):
        self.snapshot();self.assertTrue(self.sync.publish())
        second=GitSync(self.main,'main',self.sync.options)
        target=self.main/'ensemble_data/runtime/main.json';target.parent.mkdir(parents=True)
        target.write_text(json.dumps({'worker_id':'main','state':'monitoring'}))
        self.assertTrue(second.publish(),second.status)
        snapshot=self.worker/'ensemble_data/workers/msi.json'
        snapshot.write_text(json.dumps({'worker_id':'msi','records':{'new':{}}}))
        self.assertTrue(self.sync.publish(),self.sync.status)
        self.assertIn('main',self.git(self.remote,'show','main:ensemble_data/runtime/main.json'))
        self.assertIn('new',self.git(self.remote,'show','main:ensemble_data/workers/msi.json'))
