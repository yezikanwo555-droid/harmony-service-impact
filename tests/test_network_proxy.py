import socket,socketserver,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from network_proxy import Simulator,PROFILES

BODY=b'hello'*20000
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Length',str(len(BODY)));self.end_headers();self.wfile.write(BODY)
    def log_message(self,*a):pass

def request(port,target):
    with socket.create_connection(('127.0.0.1',port),3) as sock:
        sock.settimeout(15);sock.sendall(('GET '+target+' HTTP/1.1\r\nHost: localhost\r\n\r\n').encode())
        parts=[]
        while True:
            data=sock.recv(16384)
            if not data:break
            parts.append(data)
        return b''.join(parts)

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.origin.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:%d/test'%cls.origin.server_port
    @classmethod
    def tearDownClass(cls):cls.origin.shutdown();cls.origin.server_close();cls.thread.join()
    def test_3g_rate_delay_and_payload(self):
        sim=Simulator(5);sim.start()
        try:
            start=time.monotonic();raw=request(sim.port,self.url);elapsed=time.monotonic()-start
            self.assertEqual(raw.split(b'\r\n\r\n',1)[1],BODY)
            self.assertGreater(elapsed,1.35);self.assertLess(elapsed,6)
            stats=sim.snapshot()['stats'];self.assertEqual(stats['connected'],1);self.assertGreaterEqual(stats['download_bytes'],len(BODY))
            sim.reset();self.assertEqual(sim.snapshot()['stats']['connections'],0)
        finally:sim.stop()
        self.assertFalse(sim.thread.is_alive())
    def test_offline_no_upstream(self):
        sim=Simulator(0);sim.start()
        try:
            self.assertEqual(request(sim.port,self.url),b'');self.assertEqual(sim.snapshot()['stats']['simulated_failures'],1)
            self.assertEqual(sim.snapshot()['stats']['connected'],0)
        finally:sim.stop()
    def test_connect_is_byte_preserving_and_no_certificate(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                data=self.request.recv(8192);self.request.sendall(data)
        echo=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo);t=threading.Thread(target=echo.serve_forever,daemon=True);t.start()
        sim=Simulator(3);sim.start()
        try:
            with socket.create_connection(('127.0.0.1',sim.port),3) as sock:
                sock.settimeout(3);sock.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n'%echo.server_address[1]).encode())
                self.assertIn(b'200 Connection Established',sock.recv(4096))
                payload=bytes(range(256))*3;sock.sendall(payload);self.assertEqual(sock.recv(8192),payload)
        finally:sim.stop();echo.shutdown();echo.server_close();t.join()
    def test_stop_cancels_partial_headers(self):
        sim=Simulator(3);sim.start();sock=socket.create_connection(('127.0.0.1',sim.port),3)
        try:
            sock.sendall(b'CONNECT ');start=time.monotonic();sim.stop()
            self.assertLess(time.monotonic()-start,4);self.assertFalse(sim.thread.is_alive())
        finally:sock.close()
    def test_every_profile_has_valid_parameters(self):
        self.assertEqual(len(PROFILES),9)
        for i,p in enumerate(PROFILES):
            s=Simulator(i);self.assertEqual(s.snapshot()['profile'],p[0]);self.assertGreaterEqual(p[4],0);self.assertLessEqual(p[4],100)

if __name__=='__main__':unittest.main()
