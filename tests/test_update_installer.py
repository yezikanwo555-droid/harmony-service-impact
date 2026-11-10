import hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch,Mock
import update_installer as u
import impact_app as ui

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.target=self.root/'工具 空格.exe';self.target.write_bytes(b'MZold program')
        self.settings=self.root/'impact_settings.json';self.settings.write_text('keep me')
    def tearDown(self):self.temp.cleanup()
    def package(self,entries=None):
        path=self.root/'update.zip'
        with zipfile.ZipFile(path,'w') as z:
            for name,value in entries or [('ServiceImpact.exe',b'MZnew program'),('README.md',b'docs')]:z.writestr(name,value)
        return path,dict(version='9.0.0',asset=dict(size=path.stat().st_size,sha256=u.sha(path)))
    def plan(self):
        package,release=self.package();return u.prepare(package,release,self.target,self.root/'updates',pid=123)
    def test_replace_restart_and_preserve_settings_backup(self):
        plan=self.plan();restart=Mock();wait=Mock();result=u.apply(plan,wait=wait,restart=restart)
        self.assertEqual(self.target.read_bytes(),b'MZnew program');self.assertEqual(self.settings.read_text(),'keep me')
        state=json.loads(result.read_text(encoding='utf8'));self.assertEqual(state['state'],'restarted')
        self.assertEqual(Path(state['backup']).read_bytes(),b'MZold program');restart.assert_called_once_with(self.target);wait.assert_called_once_with(123)
    def test_restart_failure_rolls_back(self):
        plan=self.plan()
        with self.assertRaises(OSError):u.apply(plan,wait=lambda p:None,restart=Mock(side_effect=OSError('launch failed')))
        self.assertEqual(self.target.read_bytes(),b'MZold program')
        self.assertEqual(json.loads((plan.parent/'result.json').read_text(encoding='utf8'))['recovery'],'rolled_back')
    def test_wait_timeout_preserves_original(self):
        plan=self.plan()
        with self.assertRaises(TimeoutError):u.apply(plan,wait=Mock(side_effect=TimeoutError('busy')),restart=Mock())
        self.assertEqual(self.target.read_bytes(),b'MZold program')
    def test_staged_tampering_and_changed_target_rejected(self):
        for target in ('ServiceImpact.exe','old'):
            self.target.write_bytes(b'MZold program');plan=self.plan()
            modified=plan.parent/'ServiceImpact.exe' if target!='old' else self.target;modified.write_bytes(b'changed')
            with self.assertRaises(ValueError):u.apply(plan,wait=Mock(),restart=Mock())
            self.assertEqual(self.target.read_bytes(),b'MZold program' if target!='old' else b'changed')
    def test_bad_zip_paths_duplicate_and_missing_program(self):
        cases=[[('../escape.exe',b'MZbad')],[('C:/escape.exe',b'bad')],[('ServiceImpact.exe',b'MZ1'),('ServiceImpact.exe',b'MZ2')],[('readme',b'no exe')],[('ServiceImpact.exe',b'not windows')]]
        for entries in cases:
            with self.subTest(entries=entries):
                package,release=self.package(entries)
                with self.assertRaises(ValueError):u.prepare(package,release,self.target,self.root/'updates')
                self.assertEqual(self.target.read_bytes(),b'MZold program')
    def test_bad_download_digest_and_existing_temp_are_not_overwritten(self):
        package,release=self.package();release['asset']['sha256']='0'*64
        with self.assertRaises(ValueError):u.prepare(package,release,self.target,self.root/'updates')
        plan=self.plan();data=json.loads(plan.read_text(encoding='utf8'));incoming=self.target.with_name(self.target.name+'.new-'+data['id']);incoming.write_text('keep existing')
        with self.assertRaises(ValueError):u.apply(plan,wait=lambda p:None,restart=Mock())
        self.assertEqual(incoming.read_text(),'keep existing');self.assertEqual(self.target.read_bytes(),b'MZold program')

class UITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=patch.object(ui,'BASE',Path(self.temp.name));self.base.start()
        self.app=ui.App();self.app.withdraw()
    def tearDown(self):
        self.app.destroy();self.base.stop();self.temp.cleanup()
    def test_cancel_after_prepare_does_not_launch(self):
        self.app.update_cancel.set()
        with patch('update_installer.launch_helper') as launch:
            self.app.update_complete(dict(action='install',result='plan.json',error=''));launch.assert_not_called()
        self.assertFalse(self.app.installing)
    def test_update_busy_blocks_new_test(self):
        self.app.update_busy=True
        with patch.object(self.app,'get_config') as config:self.app.start('before');self.app.start_batch();config.assert_not_called()
    def test_helper_failure_keeps_ui_open_and_retry_available(self):
        with patch('update_installer.launch_helper',side_effect=OSError('permission denied')):
            self.app.update_complete(dict(action='install',result='plan.json',error=''))
        self.assertFalse(self.app.installing);self.assertEqual(str(self.app.update_download_button['state']),'normal')
