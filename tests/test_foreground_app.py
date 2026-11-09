import json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import foreground_app as f
import impact_app as ui

class Device:
    target='device'
    changed=False
    wrong_uid=False
    reads=0
    def shell(self,*args):
        if args[0]=='hidumper':
            self.reads+=1
            window=4 if self.changed and self.reads>1 else 3
            return 'App 0 500 %d 1 abc\nFocus window: %d\n'%(window,window)
        if args[0]=='cat':return 'Uid: 100 100 100 100'
        if args[0]=='aa':return 'AbilityRecord ID #1\n bundle name [com.example.old]\n state #BACKGROUND\nAbilityRecord ID #2\n bundle name [com.example.current]\n state #FOREGROUND\n'
        if args[0]=='bm':return json.dumps(dict(uid=200 if self.wrong_uid else 100,versionName='1'))

class Tests(unittest.TestCase):
    def test_current_verified_not_background(self):
        self.assertEqual(f.detect(Device())['bundle'],'com.example.current')
    def test_mismatched_uid_rejected(self):
        d=Device();d.wrong_uid=True
        with self.assertRaises(ValueError):f.detect(d)
    def test_changed_focus_rejected(self):
        d=Device();d.changed=True
        with self.assertRaises(ValueError):f.detect(d)
    def test_app_state_alone_is_not_foreground_ability(self):
        self.assertEqual(f.foreground_bundles('AbilityRecord ID #1\n bundle name [com.example]\n state #BACKGROUND\n app state #FOREGROUND'),[])
    def test_callback_updates_one_group_and_invalidates_old_target(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(ui,'BASE',Path(folder)):
            app=ui.App();app.withdraw()
            try:
                app.group['after']['bundle'].set('com.example.other')
                app.group['before']['ability'].set('OldAbility');app.datasets['before']=fixture('before')
                app.events.put(('detect_done',dict(role='before',result=f.detect(Device()),error='',cancelled=False)))
                app.poll()
                self.assertEqual(app.group['before']['bundle'].get(),'com.example.current')
                self.assertEqual(app.group['before']['ability'].get(),'')
                self.assertEqual(app.group['after']['bundle'].get(),'com.example.other')
                self.assertNotIn('before',app.datasets)
                app.events.put(('detect_done',dict(role='before',result=None,error='device disconnected',cancelled=False)))
                with patch.object(ui.messagebox,'showerror'):app.poll()
                self.assertEqual(app.group['before']['bundle'].get(),'com.example.current')
            finally:app.destroy()

if __name__=='__main__':unittest.main(verbosity=2)
