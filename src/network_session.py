"""Transactional network setup with a persistent undo journal and crash recovery."""
import json
import os
from pathlib import Path
import threading
import time
import uuid
import msvcrt
import harmony_startup as core
from startup_app import DesktopHdc
from phone_network import PhoneNetwork
from network_proxy import Simulator

ACTIVE={}

def journal_path():return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ServiceImpact/network_recovery.json'

def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8');tmp.replace(path)

class Session:
    def __init__(self,hdc,config,out,cancel,emit=lambda *a:None):
        self.hdc,self.config,self.out,self.cancel,self.emit=hdc,config,Path(out),cancel,emit
        self.phone=PhoneNetwork(hdc,cancel);self.sim=None;self.state=None;self.lock=None;self.id=uuid.uuid4().hex
    def acquire(self):
        p=journal_path().with_suffix('.lock');p.parent.mkdir(parents=True,exist_ok=True)
        self.lock=p.open('a+b');self.lock.seek(0)
        if p.stat().st_size==0:self.lock.write(b'0');self.lock.flush()
        self.lock.seek(0)
        try:msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:self.lock.close();self.lock=None;raise ValueError('另一窗口正在修改测试网络，请先结束该任务')
    def persist(self):
        write(journal_path(),self.state);write(self.out/'network_session.json',self.state)
    def point(self):
        import network_env
        return network_env.snapshot(self.hdc,dict(self.config,network_auto=False))
    def wait_wifi(self,enabled):
        self.hdc.shell('svc','wifi','enable' if enabled else 'disable')
        for _ in range(20):
            if self.cancel.wait(.5):raise KeyboardInterrupt
            p=self.point()
            if p['wifi_enabled']==enabled and (not enabled or p['wifi_connected'] is True):return p
        raise ValueError('Wi-Fi 未达到预期连接状态，请先连接已保存的测试 Wi-Fi')
    def set_cell(self,enabled):
        if self.point()['cellular_enabled'] is enabled:return
        self.phone.set_cellular(enabled)
        for _ in range(10):
            if self.cancel.wait(.5):raise KeyboardInterrupt
            if self.point()['cellular_enabled'] is enabled:return
        raise ValueError('移动数据开关回读不一致，未开始测试')
    def begin(self):
        import network_env
        self.acquire()
        if journal_path().exists():
            old=json.loads(journal_path().read_text(encoding='utf8'))
            if old.get('state')!='restored':raise ValueError('上次网络设置未恢复，请先点击“恢复上次环境”')
        before=self.point()
        if any(before.get(k) is None for k in ('wifi_enabled','cellular_enabled')):raise ValueError('无法备份手机网络开关，未修改环境')
        self.state=dict(state='preparing',device=self.hdc.target,hdc=self.hdc.executable,mode=self.config['network_mode'],
                        before=before,proxy=None,forward=None,started_at=core.timestamp(),error='')
        self.persist();mode=self.config['network_mode'];self.emit('phase','自动准备网络环境')
        if mode in ('wifi','builtin','reqable'):
            p=before if before['wifi_connected'] else self.wait_wifi(True)
            self.set_cell(False)
            ssid=p.get('ssid')
            if not ssid:raise ValueError('无法确定当前 Wi-Fi 名称')
            self.emit('phase','读取并备份手机 Wi-Fi 代理')
            self.state['proxy']=self.phone.read_proxy(ssid);self.persist()
            if mode=='wifi':expected=dict(ssid=ssid,mode='none')
            elif mode=='builtin':
                self.sim=Simulator(self.config['network_reqable_preset']);port=self.sim.start()
                # Fail rather than replace a pre-existing hdc forward rule.
                existing=self.hdc.run(['fport','ls'])
                if 'tcp:%d'%port in existing:raise ValueError('代理转发端口已被占用，请重试')
                self.state['forward']=['tcp:%d'%port,'tcp:%d'%port];self.persist()
                result=self.hdc.run(['rport',*self.state['forward']])
                if 'Forwardport result:OK' not in result:raise ValueError('hdc 代理反向转发失败')
                self.verify_forward()
                expected=dict(ssid=ssid,mode='manual',host='127.0.0.1',port=str(port),exclude='')
            else:raise ValueError('本版使用免费内置网络模拟，请选择“内置网络模拟（自动代理）”')
            self.emit('phase','自动设置手机代理并重新打开页面校验')
            self.state['applied_proxy']=self.phone.set_proxy(expected);self.persist();self.phone.home()
        elif mode in ('cellular','offline'):
            self.set_cell(mode=='cellular');self.wait_wifi(False);self.phone.home()
        else:raise ValueError('无需自动修改当前网络')
        self.config['_network_applied']=True;self.config['_network_session_id']=self.id;ACTIVE[self.id]=self
        p=self.point();err=network_env.check(p,self.config)
        if err:raise ValueError(err)
        self.state['state']='applied';self.state['simulation']=self.sim.snapshot() if self.sim else None;self.persist()
        self.emit('log','网络环境已自动配置，测试结束后恢复原设置。')
    def verify_forward(self):
        lines=self.hdc.run(['fport','ls']).splitlines();pair=' '.join(self.state['forward'])
        if not any(self.hdc.target in line and pair in line and '[Reverse]' in line for line in lines):
            raise ValueError('手机代理 USB 转发未回读到，停止测试')
    def new_group(self):
        if self.sim:self.sim.reset()
    def evidence(self):
        if self.sim:
            self.verify_forward();return self.sim.snapshot()
        return None
    def finish(self):
        errors=[]
        try:
            if self.state:
                self.emit('phase','正在恢复手机代理与网络，请保持解锁亮屏')
                if self.sim:self.state['final_simulation']=self.sim.snapshot()
                # Cleanup uses a fresh cancellation event; Stop must not abort restoration.
                h=DesktopHdc(self.hdc.executable,self.hdc.target,30,threading.Event())
                cleanup=Session(h,self.config,self.out,threading.Event(),self.emit)
                cleanup.state=self.state
                errors=cleanup.undo()
                self.state.update(state='restore_failed' if errors else 'restored',error='；'.join(errors),finished_at=core.timestamp())
                self.persist()
        finally:
            try:
                if self.sim:self.sim.stop()
            except Exception as exc:
                errors.append('本机代理关闭：'+str(exc))
                if self.state:
                    self.state.update(state='restore_failed',error='；'.join(errors));self.persist()
            finally:
                ACTIVE.pop(self.id,None)
                if self.lock:
                    self.lock.seek(0);msvcrt.locking(self.lock.fileno(),msvcrt.LK_UNLCK,1);self.lock.close();self.lock=None
        if errors:raise ValueError('网络恢复未完成：'+'；'.join(errors)+'。解锁手机后点击“恢复上次环境”。')
        if self.state:self.emit('log','手机网络与代理已恢复原设置。')
        return self.state
    def undo(self):
        errors=[];old=self.state['before']
        # Attempt independent restore operations even if one fails.
        if self.state.get('proxy'):
            try:
                if self.point()['wifi_connected'] is not True:self.wait_wifi(True)
                self.phone.set_proxy(self.state['proxy']);self.phone.home()
            except Exception as exc:errors.append('手机代理：'+str(exc))
        try:self.set_cell(old['cellular_enabled'])
        except Exception as exc:errors.append('移动数据：'+str(exc))
        try:
            if self.point()['wifi_enabled']!=old['wifi_enabled']:self.wait_wifi(old['wifi_enabled'])
        except Exception as exc:errors.append('Wi-Fi：'+str(exc))
        pair=self.state.get('forward')
        if pair:
            try:
                text=self.hdc.run(['fport','ls'])
                if any(self.hdc.target in line and ' '.join(pair) in line and '[Reverse]' in line for line in text.splitlines()):
                    self.hdc.run(['fport','rm',*pair])
                text=self.hdc.run(['fport','ls'])
                if any(self.hdc.target in line and ' '.join(pair) in line for line in text.splitlines()):raise ValueError('转发规则仍存在')
            except Exception as exc:errors.append('USB 代理转发：'+str(exc))
        return errors

def recover(config,events):
    path=journal_path()
    if not path.exists():return '没有待恢复的网络记录'
    state=json.loads(path.read_text(encoding='utf8'))
    if state.get('state')=='restored':return '上次环境已经恢复'
    h=DesktopHdc(core.find_hdc(config.get('hdc') or state['hdc']),state['device'],30,threading.Event());h.select_device()
    s=Session(h,config,config.get('out') or path.parent,threading.Event(),lambda *a:events.put(a));s.acquire()
    s.state=state;s.finish();return '上次手机代理与网络开关已恢复'
