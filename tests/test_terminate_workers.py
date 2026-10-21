from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil
from filelock import FileLock

from collection.terminateWorkers import terminate,project_command,_stop
from storage.pandasStore import atomic_json


SCRIPT='''import sys,signal,time,subprocess
from pathlib import Path
from filelock import FileLock
args=sys.argv
root=Path(args[args.index('--data-root')+1]);root.mkdir(parents=True,exist_ok=True)
if '--stubborn' in args:
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 signal.signal(signal.SIGINT,signal.SIG_IGN)
for name in ('worker.lock','collector.lock'):
 (root/'_system/locks').mkdir(parents=True,exist_ok=True)
locks=[FileLock(str(root/'_system/locks'/name)) for name in ('worker.lock','collector.lock')]
for lock in locks:lock.acquire()
if '--child' in args:
 child=subprocess.Popen([sys.executable,args[args.index('--child')+1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 (root/'child.pid').write_text(str(child.pid))
(root/'ready').write_text('ready')
while True:time.sleep(.1)
'''


class TerminateWorkersTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.repo=self.base/'project';self.repo.mkdir()
        self.script=self.repo/'main.py';self.script.write_text(SCRIPT)
        self.children=[];self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:child.kill()
            child.wait(timeout=5)

    def launch(self,worker,*,repository=None,stubborn=False,child=None):
        repo=repository or self.repo;repo.mkdir(exist_ok=True)
        script=repo/'main.py';script.write_text(SCRIPT)
        root=repo/('data-'+worker)
        args=[sys.executable,str(script),'--worker-id',worker,'--data-root',str(root)]
        if stubborn:args.append('--stubborn')
        if child:args.extend(['--child',str(child)])
        process=subprocess.Popen(args,cwd=repo,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.children.append(process)
        deadline=time.monotonic()+10
        while not (root/'ready').exists():
            if process.poll() is not None:self.fail('Test worker exited before acquiring locks')
            if time.monotonic()>deadline:self.fail('Test worker failed to start')
            time.sleep(.01)
        return process,root

    def quiet(self,*args,**kwargs):pass

    def test_forces_stubborn_worker_and_releases_locks_without_deleting_files(self):
        process,root=self.launch('headless',stubborn=True)
        runtime=root/'_system/workers/headless.json'
        atomic_json(runtime,{'format':'soccer-worker-runtime-v1','worker_id':'headless','state':'scraping',
                             'process_id':process.pid,'jobs':{'saved':{'state':'finished'}}})
        self.assertEqual(terminate(self.repo,root,timeout=.1,report=self.quiet),0)
        self.assertIsNotNone(process.poll())
        for name in ('worker.lock','collector.lock'):
            path=root/'_system/locks'/name
            self.assertTrue(path.exists())
            with FileLock(str(path),timeout=0):pass
        import json
        saved=json.loads(runtime.read_text());self.assertEqual(saved['state'],'stopped')
        self.assertEqual(saved['jobs'],{'saved':{'state':'finished'}})

    def test_worker_filter_preserves_msi_and_unrelated_checkout(self):
        headless,root=self.launch('headless')
        msi,_=self.launch('msi')
        unrelated,_=self.launch('headless',repository=self.base/'project-copy')
        self.assertEqual(terminate(self.repo,root,worker='headless',timeout=.2,report=self.quiet),0)
        self.assertIsNotNone(headless.poll())
        self.assertIsNone(msi.poll());self.assertIsNone(unrelated.poll())

    def test_all_project_workers_are_stopped_including_python_child(self):
        external=self.base/'external.py';external.write_text('import time\ntime.sleep(300)\n')
        first,root=self.launch('headless',stubborn=True,child=external)
        second,_=self.launch('msi',stubborn=True)
        child=psutil.Process(int((root/'child.pid').read_text()))
        def cleanup():
            try:child.kill()
            except psutil.NoSuchProcess:pass
        self.addCleanup(cleanup)
        self.assertEqual(terminate(self.repo,root,timeout=.2,report=self.quiet),0)
        self.assertIsNotNone(first.poll());self.assertIsNotNone(second.poll())
        self.assertFalse(child.is_running())

    def test_unknown_lock_owner_is_reported_without_deleting_lock(self):
        root=self.repo/'data';path=root/'_system/locks/worker.lock';path.parent.mkdir(parents=True)
        with FileLock(str(path),timeout=0):
            self.assertEqual(terminate(self.repo,root,timeout=0,report=self.quiet),2)
            self.assertTrue(path.exists())

    def test_python_main_dispatches_termination_before_worker_startup(self):
        import main
        with patch('collection.terminateWorkers.main',return_value=0) as stop,patch('collection.worker.entrypoint') as worker:
            self.assertEqual(main.entrypoint(['--terminate','--worker-id','headless']),0)
            stop.assert_called_once_with(['--terminate','--worker-id','headless']);worker.assert_not_called()

    def test_process_matching_handles_relative_absolute_pycharm_and_modules(self):
        self.assertTrue(project_command(['python','main.py'],str(self.repo),self.repo))
        self.assertTrue(project_command(['python',str(self.script)],None,self.repo))
        self.assertTrue(project_command(['python','/helpers/pydevd.py','--file',str(self.script)],None,self.repo))
        self.assertTrue(project_command(['python','-m','collection.cli'],str(self.repo),self.repo))
        self.assertFalse(project_command(['python','-m','pip','install','psutil'],str(self.repo),self.repo))
        self.assertFalse(project_command(['python','-c',str(self.script)],str(self.repo),self.repo))
        self.assertFalse(project_command(['python',str(self.base/'project-copy/main.py')],str(self.repo),self.repo))

    def test_windows_console_break_failure_falls_back_to_process_termination(self):
        from unittest.mock import Mock
        process=Mock();process.pid=123;process.children.return_value=[]
        process.send_signal.side_effect=ValueError('No console process group')
        with patch('collection.terminateWorkers.os.name','nt'),patch('collection.terminateWorkers.signal.CTRL_BREAK_EVENT',1,create=True), \
             patch('psutil.wait_procs',side_effect=[([process],[]),([],[])]):
            alive,forced=_stop([process],.1,self.quiet)
        process.terminate.assert_called_once();self.assertEqual(alive,[]);self.assertEqual(forced,{123})

    def test_real_termination_cli_runs_even_when_normal_settings_fail(self):
        hook=self.base/'hook';hook.mkdir()
        (hook/'sitecustomize.py').write_text('import sys,types\nclass Broken(types.ModuleType):\n def __getattr__(self,name):raise ValueError("broken normal startup settings")\nsys.modules["config"]=Broken("config")\n')
        script=Path(__file__).resolve().parents[1]/'main.py'
        result=subprocess.run([sys.executable,str(script),'--terminate','--timeout','0'],
            cwd=self.base,env={**os.environ,'PYTHONPATH':str(hook)},capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('no matching project Python processes remain',result.stdout)


if __name__=='__main__':unittest.main()
