import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import impact_app as ui

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=patch.object(ui,'BASE',Path(self.temp.name));self.base.start()
        self.app=ui.App();self.app.withdraw()
    def tearDown(self):
        self.app.destroy();self.base.stop();self.temp.cleanup()
    def test_old_repository_response_is_ignored(self):
        self.app.update_repo.set('example/new')
        self.app.update_complete(dict(action='check',repo='example/old',error='',result={}))
        self.assertIsNone(self.app.update_release)
        self.assertIn('重新检查',self.app.update_status.get())
    def test_download_does_not_compete_with_measurement(self):
        self.app.update_release={'newer':True}
        self.app.worker=Mock();self.app.worker.is_alive.return_value=True
        self.app.download_update()
        self.assertIsNone(self.app.update_thread)
        self.assertIn('采集',self.app.update_status.get())
    def test_first_run_settings_directory_and_token_exclusion(self):
        self.app.settings_path=Path(self.temp.name)/'new-user'/'impact_settings.json'
        self.app.update_token.set('fake-test-secret')
        self.app.save_update_preferences()
        text=self.app.settings_path.read_text(encoding='utf8')
        self.assertNotIn('fake-test-secret',text)
        self.assertIn('update_auto',text)
    def test_failed_check_allows_retry(self):
        self.app.update_check_button.configure(state='disabled')
        self.app.update_complete(dict(action='check',repo='example/old',error='offline',result=None))
        self.assertEqual(str(self.app.update_check_button['state']),'normal')
        self.assertEqual(self.app.update_status.get(),'offline')
