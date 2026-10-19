from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.upgrade_worker_reporting import preserve


class ReportingUpgradeTests(unittest.TestCase):
    def git(self,root,*args):
        return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True,text=True).stdout

    def test_preserves_latest_generated_files_and_never_changes_settings_or_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.git(root,'init');self.git(root,'config','user.name','Test');self.git(root,'config','user.email','test@example.invalid')
            generated=root/'ensemble_data/workers/msi.json';generated.parent.mkdir(parents=True);generated.write_text('{"old":true}')
            settings=root/'settings.yaml';settings.write_text('original')
            self.git(root,'add','.');self.git(root,'commit','-m','Baseline')
            generated.write_text('{"new":true}');settings.write_text('keep these local settings')
            identity=root/'data/identity.json';identity.parent.mkdir();identity.write_text('keep original identity')
            paths,backup=preserve(root)
            self.assertEqual(paths,['ensemble_data/workers/msi.json'])
            self.assertEqual((backup/'workers/msi.json').read_text(),'{"new":true}')
            self.assertEqual((root/'ensemble_data/reporting/workers/msi.json').read_text(),'{"new":true}')
            self.assertEqual(generated.read_text(),'{"new":true}')
            self.assertEqual(settings.read_text(),'keep these local settings')
            self.assertEqual(identity.read_text(),'keep original identity')
            self.assertEqual(self.git(root,'diff','--cached','--name-only'),'')
            # A second invocation never replaces a newer direct-report cache.
            cache=root/'ensemble_data/reporting/workers/msi.json';cache.write_text('{"newer":true}')
            preserve(root);self.assertEqual(cache.read_text(),'{"newer":true}')
            self.git(root,'add','settings.yaml')
            with self.assertRaisesRegex(RuntimeError,'Staged changes'):preserve(root)
