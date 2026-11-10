"""Host-controlled random UITest events with per-event foreground validation."""
import json
from pathlib import Path
import random
import re
import time
import uuid
import harmony_startup as core
from storage_page import nodes,bounds


def validate(config):
    for key,low,high in [('monkey_seconds',5,86400),('monkey_events',1,100000),
                         ('monkey_interval_ms',50,60000),('monkey_seed',0,2147483647)]:
        value=int(config[key])
        if not low<=value<=high:raise ValueError('%s 的范围为 %s～%s'%(key,low,high))
        config[key]=value
    config['monkey_in_app']=in_app_mode(config)
    return config


def in_app_mode(config):
    value=config.get('monkey_in_app',False)
    if value in (True,1,'1','true','True'):return True
    if value in (False,0,'0','false','False',''):return False
    raise ValueError('应用内模式设置无效')


def focus_pid(text):
    focus=re.search(r'^Focus window:\s*(\d+)\s*$',text,re.M)
    if not focus:raise ValueError('无法识别前台窗口，已停止随机操作')
    window=int(focus[1]);matches=[]
    for line in text.splitlines():
        m=re.match(r'^\S+\s+\d+\s+(\d+)\s+(\d+)\s+\d+\s+',line)
        if m and int(m[2])==window:matches.append(int(m[1]))
    if len(set(matches))!=1:raise ValueError('无法确定前台窗口所属进程')
    return matches[0],window


def event_for(rng,rect,in_app=False):
    x1,y1,x2,y2=rect
    if x2-x1<100 or y2-y1<100:raise ValueError('目标应用可操作区域太小')
    def point():return [round(x1+(x2-x1)*rng.uniform(.12,.88)),round(y1+(y2-y1)*rng.uniform(.18,.82))]
    kind=rng.random()*(.9 if in_app else 1)
    if kind<.6:return ['click',*map(str,point())]
    if kind<.9:return ['swipe',*map(str,point()),*map(str,point()),'900']
    return ['keyEvent','Back']


class MonkeySession:
    def __init__(self,hdc,config,out,cancel):
        self.hdc,self.config,self.cancel=hdc,dict(config),cancel
        validate(self.config);core.identifier(self.config['bundle'])
        self.in_app=in_app_mode(self.config)
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.remote='/data/local/tmp/impact_monkey_'+uuid.uuid4().hex+'.json'
        self.rng=random.Random(self.config['monkey_seed'])
        self.started=None;self.actual=0;self.recoveries=0;self.stopped=False;self.error='';self.result=None
        self.uid=None;self.module=None;self.ability=None;self.recovery_successes=0
        self.log=(self.out/'monkey_events.jsonl').open('w',encoding='utf-8')

    def record(self,row):
        row['at']=core.timestamp();self.log.write(json.dumps(row,ensure_ascii=False)+'\n');self.log.flush()

    def start(self):
        if self.hdc.shell('pidof','wukong',allow_nonzero=True).strip():raise ValueError('设备已有 Wukong 运行，请等待其结束后再开始')
        help_text=self.hdc.shell('uitest','help')
        if 'dumpLayout' not in help_text or 'uiInput' not in help_text:raise ValueError('设备缺少 UITest 随机操作能力')
        info=core.bundle_json(self.hdc.shell('bm','dump','-n',self.config['bundle']))
        self.uid=info.get('uid') or info.get('applicationInfo',{}).get('uid')
        if not isinstance(self.uid,int) or self.uid<=0:raise ValueError('无法确定目标包 UID')
        self.module,self.ability,_=core.resolve_entry(info,self.config.get('ability') or None,self.config.get('module') or None)
        self.started=time.monotonic()
        self.record({'kind':'start','bundle':self.config['bundle'],'uid':self.uid,'seed':self.config['monkey_seed'],'in_app_mode':self.in_app})

    def running(self):
        return self.started is not None and not self.stopped and not self.cancel.is_set() and not self.error and self.actual<self.config['monkey_events'] and time.monotonic()-self.started<self.config['monkey_seconds']

    def foreground(self):
        pid,window=focus_pid(self.hdc.shell('hidumper','-s','WindowManagerService','-a','-a'))
        status=self.hdc.shell('cat','/proc/%d/status'%pid)
        uid=re.search(r'^Uid:\s+(\d+)',status,re.M)
        if not uid:raise ValueError('前台进程 UID 无法读取')
        return int(uid[1])==self.uid,window

    def ensure_foreground(self,phase):
        """Check even after the last event; recover once, then verify boundedly."""
        if self.cancel.is_set():raise KeyboardInterrupt
        valid,window=self.foreground()
        self.record({'kind':'foreground_check','phase':phase,'event_index':self.actual,'valid':valid,'window':window})
        if valid:return window
        self.recoveries+=1
        self.record({'kind':'restore_foreground','phase':phase,'event_index':self.actual,'attempt':self.recoveries,'from_window':window})
        try:
            if self.cancel.is_set():raise KeyboardInterrupt
            output=self.hdc.shell('aa','start','-b',self.config['bundle'],'-a',self.ability,'-m',self.module)
            if 'start ability successfully' not in output:raise ValueError('目标包恢复前台失败：'+output[:180])
            for _ in range(3):
                if self.cancel.wait(.3):raise KeyboardInterrupt
                valid,window=self.foreground()
                if valid:
                    self.recovery_successes+=1
                    self.record({'kind':'restore_result','phase':phase,'event_index':self.actual,'success':True,'window':window})
                    return window
            raise ValueError('拉回后前台仍不是目标应用（可能存在系统弹窗），已停止随机操作')
        except Exception as exc:
            self.record({'kind':'restore_result','phase':phase,'event_index':self.actual,'success':False,'error':str(exc)})
            raise

    def step(self):
        if not self.running():return None
        try:
            window=self.ensure_foreground('before_event')
            if not self.running():return None
            result=self.hdc.shell('uitest','dumpLayout','-b',self.config['bundle'],'-p',self.remote)
            if 'DumpLayout saved to:' not in result:raise ValueError('无法读取目标应用控件范围')
            tree=json.loads(self.hdc.shell('cat',self.remote))
            candidates=[bounds(n) for n in nodes(tree) if n.get('hostWindowId')==str(window) and n.get('visible')!='false' and n.get('bounds')]
            if not candidates:raise ValueError('当前目标窗口没有可验证的控件区域')
            rect=max(candidates,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))
            action=event_for(self.rng,rect,self.in_app)
            still_valid,still_window=self.foreground()
            if not still_valid or still_window!=window:
                if self.in_app:
                    self.record({'kind':'event_skipped','reason':'前台窗口已变化，本次事件未发送','window':still_window})
                    self.ensure_foreground('before_injection')
                    return None
                raise ValueError('前台窗口已变化，本次事件未发送')
            if self.cancel.is_set():raise KeyboardInterrupt
            if not self.running():return None
            output=self.hdc.shell('uitest','uiInput',*action)
            if 'No Error' not in output:raise ValueError('UITest 未确认事件成功：'+output[:180])
            self.actual+=1
            row={'kind':'event','index':self.actual,'action':action,'elapsed_s':round(time.monotonic()-self.started,3)}
            self.record(row)
            if self.in_app:
                self.ensure_foreground('after_event')
            remaining=max(0,self.config['monkey_seconds']-(time.monotonic()-self.started))
            if self.actual<self.config['monkey_events'] and self.cancel.wait(min(remaining,self.config['monkey_interval_ms']/1000)):
                raise KeyboardInterrupt
            return row
        except KeyboardInterrupt:raise
        except Exception as exc:
            self.error=str(exc);self.record({'kind':'error','error':self.error});raise

    def stop(self):self.stopped=True

    def finish(self,cancelled=False):
        if self.result is not None:return self.result
        elapsed=time.monotonic()-self.started if self.started else 0
        if cancelled or self.cancel.is_set():status,reason='interrupted','用户停止；不再发送后续事件'
        elif self.error:status,reason='error',self.error
        elif self.actual>=self.config['monkey_events']:status,reason='complete','达到事件次数上限'
        elif self.started and elapsed>=self.config['monkey_seconds']:status,reason='complete','达到时长上限（不再发起新事件）'
        else:status,reason='error','随机测试未启动或提前结束'
        if status=='complete' and self.actual==0:status,reason='error','时长内未成功发送事件，请增加测试时长或检查设备状态'
        self.result=dict(status=status,stop_reason=reason,actual_events=self.actual,elapsed_s=round(elapsed,3),
                         seed=self.config['monkey_seed'],event_limit=self.config['monkey_events'],
                         time_limit_s=self.config['monkey_seconds'],interval_ms=self.config['monkey_interval_ms'],
                         engine='UITest 逐条随机事件',in_app_mode=self.in_app,
                         event_mix='随机点击约 67%，滑动约 33%，禁用随机返回；操作前后检查前台 UID/窗口' if self.in_app else '随机点击 60%，滑动 30%，返回 10%；每次检查前台 UID/窗口',
                         foreground_recovery_successes=self.recovery_successes,
                         foreground_recoveries=self.recoveries,event_log='logs/monkey_events.jsonl',cleanup_note='',
                         count_note='实际次数为 UITest 确认成功的操作次数，恢复前台不计入随机事件；不代表业务处理成功')
        self.record({'kind':'finish',**self.result});self.log.close()
        if not self.cancel.is_set():
            try:self.hdc.shell('rm','-f',self.remote)
            except core.MeasurementError:pass
        return self.result
