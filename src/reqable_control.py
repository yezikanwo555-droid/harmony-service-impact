"""Reqable official local API. No GUI clicks, config-file writes or secret logging."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import urllib.request
import urllib.error

MODES=('offline','bad','slow','fast','m2G','m3G','m4G','m5G','wifi')
ROUTE='/capture/network-throttling'

def token_path():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ServiceImpact/reqable_token.bin'

def crypt(data,decrypt=False):
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buf=ctypes.create_string_buffer(data);src=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dst=Blob()
    api=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dst)):
        raise ValueError('无法使用 Windows 当前用户加密保存/读取 Reqable 令牌')
    try:return ctypes.string_at(dst.data,dst.size)
    finally:ctypes.windll.kernel32.LocalFree(dst.data)

def save_token(value):
    if not value.strip():raise ValueError('访问令牌不能为空')
    path=token_path();path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(crypt(value.strip().encode()))

def read_token():
    try:return crypt(token_path().read_bytes(),True).decode()
    except FileNotFoundError:return ''

class Client:
    def __init__(self,config,token=None):
        self.port=int(config.get('network_api_port') or 9090)
        if not 1<=self.port<=65535:raise ValueError('Reqable API 端口必须为 1～65535')
        self.token=read_token() if token is None else token
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(self,path,payload=None,post=False):
        headers={'Accept':'application/json','User-Agent':'ServiceImpact/1.0'}
        if self.token:headers['Authorization']='Bearer '+self.token
        data=json.dumps(payload).encode() if payload is not None else b'' if post else None
        if data is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request('http://127.0.0.1:%d%s'%(self.port,path),data=data,headers=headers)
        try:
            with self.opener.open(request,timeout=8) as r:raw=r.read(2*1024*1024)
        except urllib.error.HTTPError as exc:
            body=exc.read(2048).decode('utf8',errors='replace')
            if exc.code==403 and 'premium' in body.lower():
                raise ValueError('Reqable 高级版限制：免费版允许读取，但当前自动修改接口需要高级版；未开始测试') from None
            if exc.code in (401,403):raise ValueError('Reqable 拒绝访问：请开启 API 服务，并在工具中保存正确访问令牌') from None
            raise ValueError('Reqable API 返回 HTTP %d（%s）'%(exc.code,path)) from None
        except OSError:raise ValueError('无法连接 Reqable API：请启动 Reqable 3.2+、开启 API 服务并核对端口') from None
        # Old versions return HTTP 200 "Hello Reqable" for every unknown route.
        if raw.strip()==b'Hello Reqable':raise ValueError('当前 Reqable 没有可用 API；请升级到 3.2+ 并开启 API 服务')
        if post and not raw.strip():return None
        try:result=json.loads(raw)
        except (ValueError,UnicodeError):raise ValueError('Reqable API 未返回有效 JSON；不能确认操作成功') from None
        if isinstance(result,dict) and (result.get('error') or result.get('success') is False):
            raise ValueError('Reqable API 操作失败：'+path)
        return result
    def config(self):
        r=self.call(ROUTE)
        if not isinstance(r,dict) or type(r.get('isEnabled'))!=bool or type(r.get('index'))!=int or not isinstance(r.get('profiles'),list):
            raise ValueError('Reqable 网络限制接口结构不兼容')
        for p in r['profiles']:
            if not isinstance(p,dict) or not all(isinstance(p.get(k),str) and p[k] for k in ('id','name','host','mode')) or p['mode'] not in MODES:
                raise ValueError('Reqable 网络预设结构不兼容')
        if not -1<=r['index']<len(r['profiles']):raise ValueError('Reqable 当前预设索引无效')
        return r
    def enabled(self,value):self.call(ROUTE+('/on' if value else '/off'),post=True)
    def live(self):
        r=self.call('/capture/live/status')
        if not isinstance(r,dict) or r.get('status') not in ('active','inactive'):raise ValueError('Reqable 抓包状态无法确认')
        return r['status']
    def set_live(self,value):self.call('/capture/live/'+('on' if value else 'off'),post=True)

class Throttle:
    """Change the selected profile in place (works with the free one-profile quota).

    Caller persists the undo state BEFORE apply; failed or cancelled runs restore it.
    """
    def __init__(self,client):self.client=client
    def backup(self):
        config=self.client.config()
        if config['profiles'] and config['index']<0:
            raise ValueError('Reqable 有预设但未选中；请首次选择一个预设后重试，以便精确恢复')
        return dict(config=config,live=self.client.live())
    def apply(self,state,mode):
        c=self.client;cfg=state['config']
        if cfg['profiles']:
            profile=dict(cfg['profiles'][cfg['index']],mode=MODES[mode],host='*')
            c.call(ROUTE+'/update',profile,post=True)
        else:
            profile=c.call(ROUTE+'/create',dict(name='ServiceImpact Auto',host='*',mode=MODES[mode]),post=True)
            if not isinstance(profile,dict) or not profile.get('id'):raise ValueError('创建 Reqable 测试预设失败')
        c.call(ROUTE+'/select',{'id':profile['id']},post=True)
        c.enabled(True);c.set_live(True)
        actual=c.config();idx=actual['index']
        if not actual['isEnabled'] or idx<0 or actual['profiles'][idx]!=profile or c.live()!='active':
            raise ValueError('Reqable 预设切换后回读不一致，已停止测试')
        return profile
    def restore(self,state):
        c=self.client;cfg=state['config'];now=c.config()
        if cfg['profiles']:
            original=cfg['profiles'][cfg['index']]
            c.call(ROUTE+'/update',original,post=True)
            c.call(ROUTE+'/select',{'id':original['id']},post=True)
        else:
            owned=[p['id'] for p in now['profiles'] if p['name']=='ServiceImpact Auto']
            if owned:c.call(ROUTE+'/delete',{'ids':owned},post=True)
        c.enabled(cfg['isEnabled']);c.set_live(state['live']=='active')
        restored=c.config()
        # Preserve other profiles; reject any restoration discrepancy.
        if restored!=cfg or c.live()!=state['live']:raise ValueError('Reqable 恢复后回读不一致，请检查恢复记录')
