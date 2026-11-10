"""Application UID network-counter deltas from HiDumper; never device totals."""
import re
import time
import harmony_startup as core

FIELDS=('traffic_upload_bytes','traffic_download_bytes','traffic_total_bytes','traffic_total_mib',
        'upload_kib_s','download_kib_s','traffic_note')
SOURCE='https://github.com/openharmony/hiviewdfx_hidumper/blob/master/frameworks/native/src/executor/traffic_dumper.cpp'

def size(value):
    if value is None:return '—'
    if value<1024:return str(int(value))+' B'
    if value<1048576:return '%.2f KiB'%(value/1024)
    return '%.2f MiB'%(value/1048576)

def parse(text):
    if re.search(r'fail|error|denied|not support',text,re.I):raise ValueError('设备流量诊断返回错误')
    pairs=[]
    for name in ('Received Bytes','Sent Bytes'):
        values=re.findall(r'^\s*'+name+r':\s*(\d+)\s*$',text,re.M)
        if len(values)!=1:raise ValueError('应用流量计数缺失或不唯一')
        pairs.append(int(values[0]))
    return {'rx':pairs[0],'tx':pairs[1]}

class Tracker:
    def __init__(self,hdc,uid,clock=time.monotonic):
        self.hdc=hdc;self.uid=int(uid);self.clock=clock;self.base=None;self.previous=None;self.last=None
        self.reset=False;self.errors=[];self.start_stage=None;self.last_stage=None;self.last_ok=False;self.points=[]
    def counters(self,raw):
        ps=self.hdc.shell('ps','-A','-o','PID,UID,NAME')
        pids=[int(m[1]) for line in ps.splitlines() if (m:=re.match(r'^\s*(\d+)\s+(\d+)\s+',line)) and int(m[2])==self.uid]
        if not pids:raise ValueError('目标 UID 没有运行进程，无法读取流量')
        pid=pids[0]
        def check_uid():
            status=self.hdc.shell('cat',f'/proc/{pid}/status')
            m=re.search(r'^Uid:\s+(\d+)',status,re.M)
            if not m or int(m[1])!=self.uid:raise ValueError('流量采样期间 PID/UID 已变化')
        check_uid();text=self.hdc.shell('hidumper','--net',str(pid),log=raw);check_uid()
        value=parse(text);value.update(at=self.clock(),recorded_at=core.timestamp(),pid=pid,uid=self.uid)
        return value
    def sample(self,stage,raw):
        result={k:None for k in FIELDS};result['traffic_note']=''
        try:
            point=self.counters(raw);point['stage']=stage
            if self.base is None:self.base=point;self.start_stage=stage
            if self.last and any(point[k]<self.last[k] for k in ('rx','tx')):self.reset=True
            if not self.reset:
                upload=point['tx']-self.base['tx'];download=point['rx']-self.base['rx']
                result.update(traffic_upload_bytes=upload,traffic_download_bytes=download,traffic_total_bytes=upload+download,traffic_total_mib=(upload+download)/1048576)
            else:result['traffic_note']='系统流量计数回退，本轮累计量不可用'
            if self.previous and not self.reset:
                seconds=point['at']-self.previous['at']
                if seconds>0:
                    result.update(upload_kib_s=(point['tx']-self.previous['tx'])/seconds/1024,download_kib_s=(point['rx']-self.previous['rx'])/seconds/1024)
            self.previous=self.last=point;self.last_stage=stage;self.last_ok=True;self.points.append(point)
            if self.start_stage!='before_tests':result['traffic_note']+='；启动前基线缺失，仅统计首次有效读数之后的消耗'
            return result
        except (ValueError,core.MeasurementError) as exc:
            self.previous=None;self.last_ok=False;self.errors.append(str(exc));result['traffic_note']=str(exc)
            return result
    def summary(self):
        available=self.base is not None and self.last is not None and self.last is not self.base and not self.reset
        upload=self.last['tx']-self.base['tx'] if available else None
        download=self.last['rx']-self.base['rx'] if available else None
        complete=available and self.last_ok and self.last_stage=='end' and self.start_stage=='before_tests'
        return dict(source='hidumper_uid',uid=self.uid,status='complete' if complete else 'partial' if available else 'unavailable',
                    upload_bytes=upload,download_bytes=download,total_bytes=upload+download if available else None,
                    started_at=self.base['recorded_at'] if self.base else None,ended_at=self.last['recorded_at'] if self.last else None,
                    elapsed_s=self.last['at']-self.base['at'] if available else None,start_stage=self.start_stage,end_stage=self.last_stage,
                    counter_reset=self.reset,errors=list(dict.fromkeys(self.errors)),
                    scope='应用 UID 全部接口字节计数差；非运营商计费流量，可能包含代理/回环；共享 UID 不能区分应用',
                    coverage='启动前至资源阶段结束' if complete else '仅有效首末读数之间，完整测试覆盖未确认',documentation=SOURCE)
