"""Local HTTP/CONNECT simulator. HTTPS stays encrypted; no CA or MITM.

Rates are aggregate application-byte pacing per direction. Delay and failure are
per new upstream connection, not IP packet loss or measured cellular RTT.
"""
import asyncio
import concurrent.futures
import random
import threading
import time
from urllib.parse import urlsplit

# name, upload kbps, download kbps, connection delay ms, connection failure percent
PROFILES=[('断网',0,0,0,100),('极差',128,256,1000,10),('较慢',1000,3000,100,0),
          ('较快',10000,20000,30,0),('2G 模拟',100,200,1200,2.5),('3G 模拟',330,780,400,0),
          ('4G 模拟',5000,20000,50,0),('5G 模拟',15000,50000,10,0),('Wi-Fi 模拟',30000,50000,5,0)]

class Simulator:
    def __init__(self,index,seed=42):
        if type(index)!=int or not 0<=index<len(PROFILES):raise ValueError('内置网络预设无效')
        self.profile=PROFILES[index];self.index=index;self.rng=random.Random(seed);self.seed=seed
        self.loop=None;self.thread=None;self.ready=threading.Event();self.error=None;self.port=None
        self.server=None;self.clients=set();self.next_slot={'up':0.,'down':0.}
        self.stats=dict(connections=0,connected=0,simulated_failures=0,upstream_errors=0,upload_bytes=0,download_bytes=0)
        self.lock=threading.Lock();self.paused=False;self.stopping=False
    def add(self,key,n=1):
        with self.lock:self.stats[key]+=n
    def snapshot(self):
        with self.lock:stats=dict(self.stats)
        return dict(provider='builtin',profile=self.profile[0],upload_kbps=self.profile[1],download_kbps=self.profile[2],
                    connection_delay_ms=self.profile[3],connection_failure_pct=self.profile[4],seed=self.seed,stats=stats,
                    note='HTTP/HTTPS CONNECT 代理；电脑出口经 hdc 转发。带宽按代理字节总量节流；延迟/失败按新连接模拟，非 IP 丢包。HTTPS 不解密。')
    def start(self):
        def run():
            self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop)
            try:
                self.server=self.loop.run_until_complete(asyncio.start_server(self.handle,'127.0.0.1',0,limit=65536))
                self.port=self.server.sockets[0].getsockname()[1];self.ready.set();self.loop.run_forever()
            except BaseException as exc:self.error=exc;self.ready.set()
            finally:
                tasks=asyncio.all_tasks(self.loop)
                for task in tasks:task.cancel()
                self.loop.run_until_complete(asyncio.gather(*tasks,return_exceptions=True));self.loop.close()
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()
        if not self.ready.wait(5) or self.error:raise ValueError('内置网络代理启动失败')
        return self.port
    async def write(self,writer,data,direction):
        rate=self.profile[1 if direction=='up' else 2]*1000/8
        if rate<=0:raise ConnectionError('offline')
        now=self.loop.time();end=max(now,self.next_slot[direction])+len(data)/rate
        self.next_slot[direction]=end
        await asyncio.sleep(max(0,end-now))
        writer.write(data);await writer.drain()
        self.add('upload_bytes' if direction=='up' else 'download_bytes',len(data))
    async def pipe(self,reader,writer,direction):
        while True:
            data=await asyncio.wait_for(reader.read(8192),timeout=60)
            if not data:return
            await self.write(writer,data,direction)
    async def handle(self,reader,writer):
        current=asyncio.current_task();self.clients.add(current);upstream=None;pipes=[]
        try:
            if self.stopping:return
            header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),timeout=15)
            lines=header.decode('iso-8859-1').split('\r\n');method,target,version=lines[0].split(' ',2)
            if self.paused:
                writer.write(b'HTTP/1.1 503 Test paused\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');await writer.drain();return
            self.add('connections')
            if self.rng.random()*100<self.profile[4]:
                self.add('simulated_failures')
                # Close without fabricating an HTTP business response or bypassing TLS.
                return
            await asyncio.sleep(self.profile[3]/1000)
            if method=='CONNECT':
                parsed=urlsplit('//'+target);host=parsed.hostname;port=parsed.port or 443
            else:
                parsed=urlsplit(target)
                if parsed.scheme!='http' or not parsed.hostname:raise ValueError('absolute HTTP URI required')
                host=parsed.hostname;port=parsed.port or 80
            if not host or not 1<=port<=65535 or parsed.username or parsed.password:raise ValueError('invalid destination')
            remote,upstream=await asyncio.wait_for(asyncio.open_connection(host,port),timeout=15)
            self.add('connected')
            if method=='CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
            else:
                route=parsed.path or '/'
                if parsed.query:route+='?'+parsed.query
                # One HTTP request per connection; encrypted CONNECT stays full duplex.
                kept=[l for l in lines[1:] if l and l.split(':',1)[0].lower() not in ('proxy-authorization','proxy-connection','connection')]
                data=(' '.join((method,route,version))+'\r\n'+'\r\n'.join(kept)+'\r\nConnection: close\r\n\r\n').encode('iso-8859-1')
                await self.write(upstream,data,'up')
            pipes=[asyncio.create_task(self.pipe(reader,upstream,'up')),asyncio.create_task(self.pipe(remote,writer,'down'))]
            if method=='CONNECT':
                done,pending=await asyncio.wait(pipes,return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
            else:await pipes[1] # upload EOF must not discard response
        except asyncio.CancelledError:raise
        except (OSError,ValueError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):self.add('upstream_errors')
        finally:
            for task in pipes:task.cancel()
            if pipes:await asyncio.gather(*pipes,return_exceptions=True)
            for stream in (writer,upstream):
                if stream:
                    stream.close()
                    try:await asyncio.wait_for(stream.wait_closed(),1)
                    except (OSError,asyncio.TimeoutError):pass
            self.clients.discard(current)
    async def reset_async(self):
        self.paused=True
        clients=list(self.clients)
        for task in clients:task.cancel()
        if clients:await asyncio.gather(*clients,return_exceptions=True)
        self.rng.seed(self.seed);self.next_slot={'up':0.,'down':0.}
        with self.lock:
            for key in self.stats:self.stats[key]=0
        self.paused=False
    def reset(self):asyncio.run_coroutine_threadsafe(self.reset_async(),self.loop).result(8)
    def stop(self):
        if not self.loop or not self.thread or not self.thread.is_alive():return
        async def shutdown():
            self.stopping=True;self.server.close();self.paused=True
            # Python 3.13+ waits for accepted transports as well as the listener.
            # Close transports whose handler has not entered our task set yet.
            if hasattr(self.server,'close_clients'):self.server.close_clients()
            await asyncio.sleep(0)
            tasks=list(self.clients)
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.wait_for(self.server.wait_closed(),2)
        try:asyncio.run_coroutine_threadsafe(shutdown(),self.loop).result(8)
        finally:self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(5)
