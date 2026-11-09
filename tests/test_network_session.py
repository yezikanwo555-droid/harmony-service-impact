import copy,json,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import network_session as ns

class Hdc:
    executable='fake';target='device';rules=[]
    def __init__(self,*a,**kw):pass
    def run(self,args):
        if args[:2]==['fport','ls']:return '\n'.join('device    '+' '.join(p)+'    [Reverse]' for p in self.rules)
        if args[0]=='rport':self.rules.append(args[1:]);return 'Forwardport result:OK'
        if args[:2]==['fport','rm']:self.rules.remove(args[2:]);return 'OK'
    def shell(self,*a):return 'OK'

class Phone:
    value={'ssid':'test','mode':'none'};fail=False;restore_fail=False
    def __init__(self,hdc,cancel):self.cancel=cancel
    def read_proxy(self,ssid):return dict(self.value)
    def set_proxy(self,v):
        if v['mode']=='none' and Phone.restore_fail:raise ValueError('locked')
        Phone.value=dict(v)
        if v['mode']=='manual' and Phone.fail:self.cancel.set();raise KeyboardInterrupt
        return dict(v)
    def home(self):pass

def point(*a):return dict(wifi_enabled=True,wifi_connected=True,ssid='test',cellular_enabled=False,available_network=True,reqable={})

class Tests(unittest.TestCase):
    def run_case(self,cancelled=False,restore_fail=False):
        import network_env
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);Phone.value={'ssid':'test','mode':'none'};Phone.fail=cancelled;Phone.restore_fail=False;Hdc.rules=[]
            with patch.object(ns,'journal_path',return_value=root/'recover.json'),patch.object(ns,'PhoneNetwork',Phone),patch.object(ns,'DesktopHdc',Hdc),patch.object(network_env,'snapshot',side_effect=point):
                config=dict(network_auto=True,network_mode='builtin',network_reqable_preset=3)
                s=ns.Session(Hdc(),config,root/'out',threading.Event())
                if cancelled:
                    with self.assertRaises(KeyboardInterrupt):s.begin()
                else:s.begin()
                self.assertTrue((root/'recover.json').exists());self.assertTrue(Hdc.rules)
                Phone.restore_fail=restore_fail
                if restore_fail:
                    with self.assertRaisesRegex(ValueError,'恢复未完成'):s.finish()
                    self.assertEqual(json.loads((root/'recover.json').read_text(encoding='utf8'))['state'],'restore_failed')
                    self.assertFalse(Hdc.rules)
                    # The next task must not overwrite a pending recovery.
                    second=ns.Session(Hdc(),config,root/'other',threading.Event())
                    try:
                        with self.assertRaisesRegex(ValueError,'上次网络'):second.begin()
                    finally:second.finish()
                    Phone.restore_fail=False
                else:
                    s.finish();self.assertEqual(Phone.value,{'ssid':'test','mode':'none'})
                    self.assertFalse(Hdc.rules);self.assertEqual(json.loads((root/'recover.json').read_text(encoding='utf8'))['state'],'restored')
                self.assertFalse(s.sim.thread.is_alive())
    def test_normal_restores_phone_and_usb_forward(self):self.run_case()
    def test_stop_event_does_not_cancel_restoration(self):self.run_case(cancelled=True)
    def test_failed_restore_retains_journal_and_blocks_next_task(self):self.run_case(restore_fail=True)
    def test_missing_package_does_not_change_network(self):
        import impact_engine as e,queue
        from test_batch_monkey import cfg,FakeHdc
        with tempfile.TemporaryDirectory() as folder:
            config=dict(cfg('before',folder),network_auto=True,network_mode='builtin',network_reqable_preset=5)
            with patch.object(e.core,'find_hdc',return_value='fake'),patch.object(e,'DesktopHdc',FakeHdc),patch.object(e.core,'bundle_json',side_effect=ValueError('missing package')),patch.object(ns,'Session') as session:
                result=e.collect(config,queue.Queue(),threading.Event())
                self.assertEqual(result['result']['state'],'error');session.assert_not_called()

if __name__=='__main__':unittest.main()
