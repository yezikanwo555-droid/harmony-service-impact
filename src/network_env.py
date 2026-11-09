"""Network condition evidence and Reqable 3.0.x preset integration.

Never claims that a proxy's local configuration proves application routing.
Only the explicit prepare action changes Wi-Fi; collection is read-only.
"""
import json
import os
from pathlib import Path
import re
import socket
import time
import harmony_startup as core
from startup_app import DesktopHdc
import reqable_control
import network_proxy

MODES={'内置网络模拟（自动代理）':'builtin','当前网络（只记录）':'current','Wi-Fi':'wifi','蜂窝（关闭 Wi-Fi）':'cellular',
       '手机断网（关闭 Wi-Fi 和数据）':'offline','Reqable 弱网（Wi-Fi 代理）':'reqable'}
# Reqable documented presets; these are configured ranges, not measurements.
PRESETS=[('断网','0 bps','0 bps',0,100),('极差','0～1 Mbps','0～1 Mbps',1000,10),
         ('较慢','1～5 Mbps','1～5 Mbps',100,0),('较快','5～20 Mbps','5～20 Mbps',30,0),
         ('2G 模拟','0～240 kbps','0～200 kbps',1200,2.5),('3G 模拟','0～780 kbps','0～330 kbps',400,0),
         ('4G 模拟','30～50 Mbps','5～10 Mbps',50,0),('5G 模拟','40～70 Mbps','7～15 Mbps',0,0),
         ('Wi-Fi 模拟','25～40 Mbps','15～30 Mbps',0,0)]
DOC='https://reqable.com/en-US/docs/capture/network-condition'

def default_config_dir():return str(Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))/'Reqable/config')

def validate(config):
    if config.get('network_mode','current') not in MODES.values():raise ValueError('请选择有效网络环境')
    host=config.get('network_ping_host','').strip()
    if host and not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?',host):
        raise ValueError('Ping 目标仅支持 IPv4 或域名，不要填写 URL、端口或命令')
    config['network_ping_host']=host
    mode=int(config.get('network_reqable_preset',2))
    if not 0<=mode<len(PRESETS):raise ValueError('Reqable 预设无效')
    config['network_reqable_preset']=mode
    if config.get('network_auto') and config.get('network_mode')=='reqable':
        port=int(config.get('network_api_port') or 9090)
        if not 1<=port<=65535:raise ValueError('Reqable API 端口必须为 1～65535')
        config['network_api_port']=port
    if config.get('network_mode')=='reqable' and not config.get('network_auto') and config.get('network_proxy_confirmed') is not True:
        raise ValueError('请先在 Reqable 确认两组目标业务请求经过代理并命中规则，再勾选代理路径确认')
    return config

def parse_device(wifi,cell,net):
    def match(pattern,text):
        found=re.search(pattern,text,re.M|re.I)
        return found[1].strip() if found else None
    active=match(r'^WiFi active state:\s*(\S+)',wifi)
    connection=match(r'^WiFi connection status:[ \t]*([^\r\n]+)',wifi)
    enabled=match(r'^CellularDataEnabled\s*:\s*(TRUE|FALSE)\s*$',cell)
    available=re.findall(r'^\s*IsAvailable:\s*([01])\s*$',net,re.M)
    known_empty='Net connect Info:' in net and ('defaultNetSupplier_ is nullptr' in net or 'SupplierId:' not in net)
    return dict(wifi_enabled={'activated':True,'deactivated':False,'inactive':False}.get(active),
                wifi_connected=True if connection=='connected' else False if connection in ('disconnected','not connected') or active in ('deactivated','inactive') else None,
                ssid=match(r'^\s*Connection.ssid:\s*(.*)$',wifi),rssi_dbm=match(r'^\s*Connection.rssi:\s*(-?\d+)',wifi),
                band=match(r'^\s*Connection.band:\s*(.*)$',wifi),
                cellular_enabled={'TRUE':True,'FALSE':False}.get(enabled.upper() if enabled else None),
                available_network=any(x=='1' for x in available) if available else False if known_empty else None)

def reqable_snapshot(config):
    if config.get('network_mode')=='builtin':return {'status':'not_used','note':'内置模拟不依赖 Reqable'}
    if config.get('network_auto') and config.get('network_mode')=='reqable':
        try:
            client=reqable_control.Client(config);data=client.config();index=data['index']
            chosen=data['profiles'][index] if index>=0 else None
            profile=None
            if chosen:
                mode=reqable_control.MODES.index(chosen['mode']);name,up,down,delay,loss=PRESETS[mode]
                profile=dict(mode=mode,name=name,host=chosen['host'],upload=up,download=down,latency_ms=delay,loss_pct=loss)
            live=client.live()
            return dict(status='ok',enabled=data['isEnabled'],profile=profile,port=client.port,
                        local_port_open=live=='active',evidence='Reqable 官方 API 实时回读；不证明所有应用流量覆盖',source=DOC)
        except (OSError,ValueError) as exc:return dict(status='unavailable',error=str(exc))
    folder=Path(config.get('network_reqable_dir') or default_config_dir())
    try:
        data=json.loads((folder/'capture_network_condition').read_text(encoding='utf-8-sig'))
        capture=json.loads((folder/'capture_config').read_text(encoding='utf-8-sig'))
        conditions=data['conditions'];index=data['index'];enabled=data['isEnabled']
        if not isinstance(conditions,list) or type(index)!=int or type(enabled)!=bool:raise ValueError('结构不匹配')
        chosen=conditions[index] if 0<=index<len(conditions) else None
        port=capture['proxyPort']
        if type(port)!=int or not 1<=port<=65535:raise ValueError('代理端口无效')
        if chosen:
            mode=chosen['mode']
            if type(mode)!=int or not 0<=mode<len(PRESETS) or not isinstance(chosen.get('endpoint'),str):raise ValueError('不支持的规则结构')
            name,up,down,delay,loss=PRESETS[mode]
            profile=dict(mode=mode,name=name,host=chosen['endpoint'],upload=up,download=down,latency_ms=delay,loss_pct=loss)
        else:profile=None
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=1):listening=True
        except OSError:listening=False
        return dict(status='ok',enabled=enabled,profile=profile,port=port,local_port_open=listening,
                    evidence='本地配置文件与端口检查；不证明手机业务流量经过代理',source=DOC)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return dict(status='unavailable',error='无法读取 Reqable 配置：'+str(exc))

def snapshot(hdc,config,raw=None):
    raw=raw if raw is not None else []
    values=[];errors=[]
    for service in ('WifiDevice','TelephonyCellularData','NetConnManager'):
        try:values.append(hdc.shell('hidumper','-s',service,log=raw))
        except core.MeasurementError as exc:values.append('');errors.append(str(exc))
    return dict(at=core.timestamp(),device=hdc.target,**parse_device(*values),errors=errors,
                reqable=reqable_snapshot(config))

def check(point,config):
    mode=config.get('network_mode','current')
    if mode in ('wifi','reqable') and not(point['wifi_connected'] is True and point['available_network'] is True):
        return '需要连接 Wi-Fi 且系统存在可用网络；请先在手机上连接测试 Wi-Fi'
    if mode=='cellular' and not(point['wifi_enabled'] is False and point['cellular_enabled'] is True and point['available_network'] is True):
        return '蜂窝条件不满足：请关闭 Wi-Fi，开启移动数据并等待系统网络可用'
    if mode=='offline' and not(point['wifi_enabled'] is False and point['cellular_enabled'] is False and point['available_network'] is False):
        return '断网条件不满足或无法确认：请关闭 Wi-Fi、移动数据和其他联网方式'
    if mode=='builtin':
        if point['wifi_connected'] is not True:return '内置模拟需要保持 Wi-Fi 已连接'
        if not config.get('_network_applied'):return '内置模拟与手机代理尚未自动配置'
        if point.get('cellular_enabled') is not False:return '内置网络模拟要求关闭移动数据，避免蜂窝回退'
    if mode=='reqable':
        r=point['reqable'];profile=r.get('profile') or {}
        if r.get('status')!='ok':return r.get('error','Reqable 信息不可用')
        if not r.get('enabled') or not r.get('local_port_open'):return '请启动 Reqable 代理，并启用网络限制'
        if profile.get('mode')!=config['network_reqable_preset']:return 'Reqable 当前预设与所选预设不同，测试已停止'
        if not profile.get('host'):return 'Reqable 没有有效的域名匹配规则'
        if config.get('network_auto'):
            if not config.get('_network_applied'):return '尚未自动设置并回读手机代理，不能开始采集'
        elif config.get('network_proxy_confirmed') is not True:return '尚未确认目标业务流量经过 Reqable 并命中规则'
    return ''

def describe(point):
    show=lambda value:'未知' if value is None else '是' if value else '否'
    r=point.get('reqable',{});profile=r.get('profile') or {}
    text='Wi-Fi连接：%s；Wi-Fi开启：%s；移动数据开启：%s；系统有可用网络：%s' % (
        show(point.get('wifi_connected')),show(point.get('wifi_enabled')),show(point.get('cellular_enabled')),
        show(point.get('available_network')))
    if point.get('simulation'):
        sim=point['simulation'];text+='；内置模拟：'+sim['profile']+'；代理连接数：'+str(sim['stats']['connections'])
    elif r.get('status')!='not_used':text+='；Reqable：'+profile.get('name','未读取')+('（已开启）' if r.get('enabled') else '（未开启/未知）')
    return text

def signature(point,mode):
    keys=('wifi_enabled','wifi_connected','ssid','cellular_enabled','available_network')
    # The OS connectivity probe is expected to fail under offline/weak simulation.
    # Record it, but do not mistake that intended effect for a transport change.
    if mode=='builtin':keys=tuple(k for k in keys if k!='available_network')
    result={k:point.get(k) for k in keys}
    if mode=='reqable':result['reqable']=point.get('reqable',{})
    return result

def ping(hdc,host,raw):
    if not host:return {'status':'not_requested'}
    text=hdc.shell('ping','-c','4','-W','2','-w','8',host,log=raw,allow_nonzero=True)
    loss=re.search(r'([\d.]+)% packet loss',text)
    rtt=re.search(r'(?:rtt|round-trip).*?=\s*([\d.]+)/([\d.]+)/([\d.]+)',text)
    return dict(status='ok' if loss else 'unavailable',host=host,loss_pct=float(loss[1]) if loss else None,
                rtt_avg_ms=float(rtt[2]) if rtt else None,
                note='手机 ICMP 小样本探测，不经过 HTTP 代理，不能验证 Reqable 限速/延迟/丢包，也不是业务成功率')

class Guard:
    def __init__(self,hdc,config,out,data,emit):
        self.hdc,self.config,self.out,self.data,self.emit=hdc,config,Path(out),data,emit
        self.last=0;self.first=None
        data['network']=dict(mode=config['network_mode'],points=[],proxy_path_confirmation='自动设置并回读手机代理；业务覆盖未逐请求证明' if config.get('_network_applied') else '用户确认' if config.get('network_proxy_confirmed') else '未确认',
            note='仅检查链路条件，不能证明每个业务请求的实际路由或公网可达性；前后/阶段性快照不保证期间没有短暂变化')
    def checkpoint(self,stage,probe=False):
        raw=[];point=snapshot(self.hdc,self.config,raw);point['stage']=stage
        if self.config.get('_network_session_id'):
            from network_session import ACTIVE
            session=ACTIVE.get(self.config['_network_session_id'])
            if session:point['simulation']=session.evidence()
        if probe:
            try:point['ping']=ping(self.hdc,self.config.get('network_ping_host',''),raw)
            except core.MeasurementError as exc:point['ping']={'status':'unavailable','error':str(exc)}
        error=check(point,self.config)
        mode=self.config['network_mode'];sig=signature(point,mode)
        if self.first is not None and sig!=self.first:
            point['changed']=True
            if mode!='current':error=error or '网络条件或 Reqable 规则发生变化，本组结果需重新采集'
        if self.first is None:self.first=sig
        point['check']='mismatch' if error else 'conditions_match' if mode!='current' else 'record_only'
        point['check_note']=error
        self.data['network']['points'].append(point);self.last=time.monotonic()
        n=len(self.data['network']['points'])
        for name,obj in [('network_%04d.json'%n,dict(snapshot=point,commands=raw))]:
            (self.out/'logs'/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
        (self.out/'network.json').write_text(json.dumps(self.data['network'],ensure_ascii=False,indent=2),encoding='utf-8')
        self.emit('log','网络 ['+stage+']：'+describe(point))
        if error:raise ValueError(error)
        return point

def run_action(config,action,events,cancel):
    payload=dict(result=None,error='',cancelled=False)
    session=None
    try:
        from network_session import Session,recover
        if action=='restore':
            payload['message']=recover(config,events);return
        h=DesktopHdc(core.find_hdc(config.get('hdc') or None),config.get('target') or None,30,cancel);h.select_device()
        if config.get('network_auto'):
            if action=='prepare' and config['network_mode']!='current':
                folder=Path(config.get('out') or '.')/('network_check_'+time.strftime('%Y%m%d_%H%M%S'))
                session=Session(h,config,folder,cancel,lambda *a:events.put(a));session.begin()
            point=snapshot(h,config)
            if session:point['simulation']=session.evidence()
            payload['result']=point
            # A read-only check reports actual state, without pretending to apply a selection.
            payload['error']=check(point,config) if action=='prepare' else ''
            return
        if action=='prepare':
            mode=config['network_mode'];before=snapshot(h,config)
            if mode in ('wifi','reqable'):command='enable'
            elif mode=='cellular':
                if before['cellular_enabled'] is not True:raise ValueError('请先在手机上开启移动数据，再点击准备环境')
                command='disable'
            elif mode=='offline':
                if before['cellular_enabled'] is not False:raise ValueError('请先在手机上关闭移动数据，再点击准备环境')
                command='disable'
            else:command=None
            if command:
                h.shell('svc','wifi',command)
                if cancel.wait(3):raise KeyboardInterrupt
        point=snapshot(h,config)
        if config.get('network_ping_host'):point['ping']=ping(h,config['network_ping_host'],[])
        payload['result']=point;payload['error']=check(point,config)
    except KeyboardInterrupt:payload['cancelled']=True
    except Exception as exc:payload['error']=str(exc)
    finally:
        if session:
            try:session.finish();payload['message']='试切换完成，已恢复原设置'
            except Exception as exc:payload['error']+='；'+str(exc)
        events.put(('network_done',payload))

def warnings(before,after):
    a,b=before.get('network'),after.get('network')
    if not a and not b:return []
    notes=[]
    if not a or not b:return ['一组缺少网络记录，网络条件无法核对']
    if a['mode']!=b['mode']:notes.append('两组网络环境选择不同')
    for label,data in [('改造前',a),('改造后',b)]:
        points=data.get('points',[])
        if not points or points[-1].get('stage')!='end':notes.append(label+'缺少完整网络前后记录')
        if any(p.get('changed') or p.get('check')=='mismatch' for p in points):notes.append(label+'网络条件发生变化或不符合选择')
        if any(p.get('available_network') is None for p in points):notes.append(label+'网络信息存在缺失')
    if a.get('points') and b.get('points') and signature(a['points'][0],a['mode'])!=signature(b['points'][0],b['mode']):notes.append('两组开始时的网络条件或 Reqable 规则不同')
    if 'reqable' in (a['mode'],b['mode']):notes.append('Reqable 规则与手机代理配置不能证明所有业务流量都经过代理；非全设备链路丢包测量')
    elif 'builtin' not in (a['mode'],b['mode']) and any(p.get('reqable',{}).get('enabled') for d in (a,b) for p in d.get('points',[])):notes.append('检测到 Reqable 网络限制开启，若业务经过该代理，普通网络测试可能受其影响')
    if 'builtin' in (a['mode'],b['mode']):
        notes.append('内置模拟仅覆盖采用手机系统 HTTP 代理的流量；HTTPS 不解密。统计含设备其他应用，不能按包名归因；不模拟 UDP/QUIC 或底层 IP 丢包。')
        sims=[]
        for label,d in [('改造前',a),('改造后',b)]:
            points=d.get('points',[]);sim=points[-1].get('simulation') if points else None
            if not sim or not sim.get('stats',{}).get('connections'):notes.append(label+'未观察到经过内置代理的连接，不能确认网络模拟覆盖目标业务')
            if sim:sims.append({k:v for k,v in sim.items() if k not in ('stats','note')})
        if len(sims)==2 and sims[0]!=sims[1]:notes.append('两组内置网络模拟参数不同')
    for label,d in [('改造前',before),('改造后',after)]:
        if d.get('network_restore',{}).get('state')=='restore_failed':notes.append(label+'测试后网络恢复失败，请使用恢复上次环境')
    return notes
