import copy,json,queue,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import network_env as n
import impact_engine as e
import impact_app as ui

WIFI='WiFi active state: activated\nWiFi connection status: connected\n Connection.ssid: test\n Connection.rssi: -65\n'
CELL='CellularDataEnabled : FALSE\n'
NET='Net connect Info:\n SupplierId: 1003\n IsAvailable: 1\n'

def point():return dict(at='test',**n.parse_device(WIFI,CELL,NET),reqable={'status':'ok','enabled':False,'profile':None,'local_port_open':False})

class Tests(unittest.TestCase):
    def test_missing_is_not_offline(self):
        p=n.parse_device('','','');p['reqable']={}
        self.assertIsNone(p['available_network']);self.assertTrue(n.check(p,{'network_mode':'offline'}))
    def test_wifi_cellular_offline_conditions(self):
        p=point();self.assertFalse(n.check(p,{'network_mode':'wifi'}));self.assertTrue(n.check(p,{'network_mode':'cellular'}))
        p.update(wifi_enabled=False,wifi_connected=False,cellular_enabled=True)
        self.assertFalse(n.check(p,{'network_mode':'cellular'}))
        p.update(cellular_enabled=False,available_network=False)
        self.assertFalse(n.check(p,{'network_mode':'offline'}))
    def test_parse_known_empty_network(self):
        p=n.parse_device('WiFi active state: inactive\nWiFi connection status: not connected',CELL,'Net connect Info:\n defaultNetSupplier_ is nullptr\n SupplierId: \n IsAvailable: \nDns result Info:')
        self.assertIs(p['available_network'],False);self.assertIs(p['wifi_connected'],False)
    def test_reqable_file_and_port_are_not_route_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            (p/'capture_network_condition').write_text(json.dumps(dict(isEnabled=True,index=0,conditions=[dict(mode=2,endpoint='*.example.test')])),encoding='utf-8')
            (p/'capture_config').write_text('{"proxyPort":9090}',encoding='utf-8')
            with patch.object(n.socket,'create_connection',side_effect=OSError):r=n.reqable_snapshot({'network_reqable_dir':folder})
            self.assertEqual(r['profile']['name'],'较慢');self.assertFalse(r['local_port_open'])
            x=point();x['reqable']=r;c=dict(network_mode='reqable',network_reqable_preset=2,network_proxy_confirmed=True)
            self.assertTrue(n.check(x,c));r['local_port_open']=True;self.assertFalse(n.check(x,c))
            c['network_proxy_confirmed']=False;self.assertTrue(n.check(x,c))
    def test_profile_change_rejected_and_evidence_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'logs').mkdir();data={};a=point();b=point();b['ssid']='other'
            g=n.Guard(None,{'network_mode':'wifi'},folder,data,lambda *args:None)
            with patch.object(n,'snapshot',side_effect=[a,b]):
                g.checkpoint('start')
                with self.assertRaises(ValueError):g.checkpoint('end')
            self.assertTrue(data['network']['points'][1]['changed']);self.assertTrue((Path(folder)/'network.json').exists())
    def test_no_shell_syntax_in_ping_target(self):
        for host in ('-c 100','a;reboot','https://example.com','x\nreboot'):
            with self.assertRaises(ValueError):n.validate({'network_ping_host':host})
    def test_ping_not_a_reqable_measurement(self):
        class Fake:
            def shell(self,*args,**kwargs):return '4 packets transmitted, 3 received, 25% packet loss\nrtt min/avg/max = 1.0/2.0/3.0 ms'
        r=n.ping(Fake(),'127.0.0.1',[]);self.assertEqual(r['loss_pct'],25);self.assertEqual(r['rtt_avg_ms'],2)
        self.assertIn('不能验证 Reqable',r['note'])
    def test_report_preserves_network_and_flags_difference(self):
        a,b=fixture('before'),fixture('after')
        for d in (a,b):d['network']=dict(mode='wifi',points=[dict(point(),stage='start'),dict(point(),stage='end')])
        b['network']['points'][0]['ssid']='other'
        self.assertTrue(any('网络条件' in w for w in e.comparison(a,b)['warnings']))
        with tempfile.TemporaryDirectory() as folder:
            text=(e.report(a,b,folder)/'report.html').read_text(encoding='utf-8')
            self.assertIn('网络环境与校验记录',text);self.assertIn('other',text)
    def test_ui_network_config_and_callback(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(ui,'BASE',Path(folder)):
            app=ui.App();app.withdraw()
            try:
                app.network_dialog();app.update_idletasks()
                app.v['network_mode'].set('内置网络模拟（自动代理）')
                self.assertEqual(app.network_config()['network_mode'],'builtin')
                self.assertTrue(app.network_config()['network_auto'])
                self.assertEqual(app.network_config()['network_reqable_preset'],2)
                app.v['network_reqable_preset'].set('极差');self.assertFalse(app.network_proxy_confirmed.get())
                app.events.put(('network_done',dict(result=point(),error='',cancelled=False)));app.poll()
                self.assertIn('Wi-Fi连接：是',app.network_status.get())
            finally:app.destroy()
    def test_builtin_report_discloses_scope_and_missing_traffic(self):
        from network_proxy import Simulator
        a,b=fixture('before'),fixture('after')
        for d in (a,b):
            d['network']=dict(mode='builtin',points=[dict(point(),stage='end',simulation=Simulator(5).snapshot())])
            d['network_restore']={'state':'restored'}
        with tempfile.TemporaryDirectory() as out:
            report=(e.report(a,b,out)/'report.html').read_text(encoding='utf8')
            self.assertIn('未观察到经过内置代理',report);self.assertIn('已恢复原设置',report)
            self.assertIn('新连接延迟',report);self.assertIn('不能按目标包归因',report)
    def test_builtin_allows_expected_connectivity_probe_failure(self):
        a=point();b=dict(a,available_network=False)
        c={'network_mode':'builtin','_network_applied':True}
        self.assertFalse(n.check(b,c));self.assertEqual(n.signature(a,'builtin'),n.signature(b,'builtin'))
        self.assertTrue(n.check(dict(b,wifi_connected=False),c))

if __name__=='__main__':unittest.main(verbosity=2)
