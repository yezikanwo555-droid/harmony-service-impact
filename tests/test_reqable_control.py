import copy,json,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import reqable_control as r

class FakeClient:
    def __init__(self):
        self.value=dict(profiles=[dict(id='1',name='User profile',host='*.test',mode='slow')],index=0,isEnabled=False)
        self.status='inactive';self.fail=False
    def config(self):return copy.deepcopy(self.value)
    def live(self):return self.status
    def set_live(self,v):self.status='active' if v else 'inactive'
    def enabled(self,v):self.value['isEnabled']=v
    def call(self,path,payload,post=False):
        if path.endswith('/update'):
            if self.fail:raise ValueError('failed')
            self.value['profiles']=[copy.deepcopy(payload)]
        elif path.endswith('/select'):self.value['index']=0
        elif path.endswith('/create'):
            p=dict(payload,id='new');self.value['profiles'].append(p);return p
        elif path.endswith('/delete'):self.value['profiles']=[];self.value['index']=-1

class Tests(unittest.TestCase):
    def test_all_modes_restore_exact_rule_host_enabled_and_live(self):
        for mode in range(9):
            c=FakeClient();t=r.Throttle(c);before=t.backup();t.apply(before,mode)
            self.assertEqual(c.config()['profiles'][0]['mode'],r.MODES[mode]);self.assertTrue(c.value['isEnabled'])
            self.assertEqual(c.value['profiles'][0]['host'],'*');self.assertEqual(c.live(),'active')
            t.restore(before);self.assertEqual(t.backup(),before)
    def test_empty_profile_create_and_restore(self):
        c=FakeClient();c.value.update(profiles=[],index=-1);t=r.Throttle(c);before=t.backup()
        t.apply(before,5);t.restore(before);self.assertEqual(t.backup(),before)
    def test_update_failure_is_not_success(self):
        c=FakeClient();t=r.Throttle(c);before=t.backup();c.fail=True
        with self.assertRaises(ValueError):t.apply(before,1)
        self.assertEqual(t.backup(),before)
    def test_api_old_hello_and_wrong_schema_rejected(self):
        class Handler(BaseHTTPRequestHandler):
            payload=b'Hello Reqable'
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(self.payload)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            c=r.Client({'network_api_port':server.server_port},token='')
            with self.assertRaisesRegex(ValueError,'3.2'):c.config()
            Handler.payload=b'{}'
            with self.assertRaisesRegex(ValueError,'不兼容'):c.config()
            Handler.payload=json.dumps(FakeClient().value).encode()
            self.assertEqual(c.config(),FakeClient().value)
        finally:server.shutdown();server.server_close();thread.join()
    def test_secret_roundtrip(self):
        secret=b'test-value-only';encoded=r.crypt(secret)
        self.assertNotIn(secret,encoded);self.assertEqual(r.crypt(encoded,True),secret)

if __name__=='__main__':unittest.main()
