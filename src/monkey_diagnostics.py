"""Read-only Monkey HiLog capture and time/identity-scoped Faultlogger evidence."""
import datetime as dt
import html
import json
import re
import subprocess
import threading
import time
from pathlib import Path
import harmony_startup as core
from startup_app import DesktopHdc

FAULT_RE=re.compile(r'^(jscrash|cppcrash|appfreeze)-([A-Za-z0-9_.:@-]+)-(\d+)-(\d{13,17})(?:\.log)?$')
ERROR_RE=re.compile(r'permission denied|not permitted|\[fail\]|not support|unknown option|invalid argument|CODE:\s*-\d+',re.I)
KINDS={'jscrash':'JS 崩溃','cppcrash':'Native 崩溃','appfreeze':'应用无响应（AppFreeze）','suspected_freeze':'疑似无响应 / 诊断命令超时'}
LOG_LIMIT=100*1024*1024
FILE_LIMIT=20*1024*1024

def write(path,value):
    path=Path(path);temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(path)

def parse_clock(text):
    m=re.fullmatch(r'\s*(\d{10}(?:\.\d+)?)\s+([+-])(\d{2})(\d{2})\s*',text)
    if not m:raise ValueError('无法读取设备时间及所在时区')
    minutes=(int(m[3])*60+int(m[4]))*(1 if m[2]=='+' else -1)
    return float(m[1]),dt.timezone(dt.timedelta(minutes=minutes))

def stamp_time(stamp,zone):
    if len(stamp)==13:return int(stamp)/1000
    pattern='%Y%m%d%H%M%S%f' if len(stamp)==17 else '%Y%m%d%H%M%S'
    if len(stamp) not in (14,17):raise ValueError('故障时间格式不可识别')
    return dt.datetime.strptime(stamp,pattern).replace(tzinfo=zone).timestamp()

def parse_listing(text):
    if ERROR_RE.search(text) or 'Fault log list:' not in text:raise ValueError('系统故障列表不可读或格式不支持')
    return {line.strip() for line in text.splitlines() if FAULT_RE.fullmatch(line.strip())}

def log_epoch(line,zone):
    # Use the device's displayed wall time: some production HiLog epoch formats
    # apply a fixed timezone offset even when the device timezone changes.
    m=re.match(rb'^(?:\S+ )?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s',line)
    if not m:return None
    try:return dt.datetime.fromisoformat(m[1].decode('ascii')).replace(tzinfo=zone).timestamp()
    except ValueError:return None

def matches_process(name,bundle,process):
    return any(name==p or name.startswith(p+':') for p in {bundle,process} if p)

def assess_fault(name,text,bundle,uid,process,start,end,zone):
    """Only typed OS evidence with matching UID, exact process family, and time confirms."""
    m=FAULT_RE.fullmatch(name)
    if not m or int(m[3])!=uid or not matches_process(m[2],bundle,process):return None
    when=stamp_time(m[4],zone)
    if when>end or when+(1 if len(m[4])==14 else 0)<start:return None
    fields={}
    for line in text.splitlines()[:100]:
        key,sep,value=line.partition(':')
        if sep and key in ('Uid','Pid','Module name','Process name','Timestamp','Reason'):fields[key]=value.strip()
    identity=fields.get('Process name') or fields.get('Module name','')
    if fields.get('Uid')!=str(uid) or not matches_process(identity,bundle,process):raise ValueError('故障文件头部身份与目标应用不一致')
    timestamp=fields.get('Timestamp','').replace(' ', 'T',1)
    # Some system versions use a colon before milliseconds.
    timestamp=re.sub(r'(T\d{2}:\d{2}:\d{2}):(\d{3})$',r'\1.\2',timestamp)
    try:header_time=dt.datetime.fromisoformat(timestamp).replace(tzinfo=zone).timestamp()
    except ValueError:raise ValueError('故障文件缺少可验证的发生时间') from None
    if not start<=header_time<=end or abs(header_time-when)>1:raise ValueError('故障文件时间与本轮范围不一致')
    return dict(kind=m[1],confirmed=True,at=dt.datetime.fromtimestamp(header_time,zone).isoformat(),
                pid=fields.get('Pid'),uid=uid,process=identity,reason=fields.get('Reason','详见系统故障日志')[:1000],source_file=name)

class Session:
    def __init__(self,hdc,bundle,uid,process,out,emit=lambda *args:None):
        # A separate cancellation token permits bounded evidence collection after Stop.
        self.hdc=DesktopHdc(hdc.executable,hdc.target,timeout=8,cancel=threading.Event())
        self.bundle,self.uid,self.process=bundle,int(uid),process or bundle
        self.out=Path(out)/'diagnostics';self.out.mkdir(parents=True,exist_ok=True)
        self.emit=emit;self.proc=None;self.reader=None;self.stop=threading.Event();self.finished=None
        self.errors=[];self.incidents=[];self.baseline=set();self.baseline_ok=False;self.list_ok=False
        self.begin=None;self.end=None;self.zone=dt.timezone.utc;self.began_mono=None;self.clock_ok=True
        self.lines=0;self.bytes=0;self.truncated=False;self.stream_error='';self.stream_started=False
        self.capture_begin=0;self.capture_end=None;self.commands=[]

    def clock(self):return parse_clock(self.hdc.shell('date','+%s.%N %z',log=self.commands))

    def listing(self):
        text=self.hdc.shell('hidumper','-s','1201','-a','-p Faultlogger %s -LogSuffixWithMs',log=self.commands)
        if 'Fault log list:' not in text:
            text=self.hdc.shell('hidumper','-s','1201','-a','-p Faultlogger',log=self.commands)
        return parse_listing(text)

    def start(self):
        try:self.baseline=self.listing();self.baseline_ok=True
        except Exception as exc:self.errors.append('起始故障列表：'+str(exc))
        try:self.begin,self.zone=self.clock();self.capture_begin=self.begin;self.began_mono=time.monotonic()
        except Exception as exc:self.clock_ok=False;self.errors.append(str(exc))
        write(self.out/'baseline.json',dict(files=sorted(self.baseline),device_epoch=self.begin,timezone=str(self.zone)))
        try:
            command=[self.hdc.executable]+(['-t',self.hdc.target] if self.hdc.target else [])+['shell','hilog -v time -v year -v zone']
            self.proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.stream_started=True;self.reader=threading.Thread(target=self._capture,daemon=True);self.reader.start()
        except Exception as exc:self.stream_error=str(exc);self.errors.append('系统日志启动失败：'+str(exc))
        self.emit('log','Monkey 系统日志自动保存中；结束时核对本轮目标应用崩溃 / AppFreeze 记录。')

    def _capture(self):
        pending=b'';in_window=False;seen_header=False
        try:
            with (self.out/'system_hilog.log').open('wb') as output,(self.out/'hilog_status.txt').open('wb') as status:
                status_bytes=0
                while chunk:=self.proc.stdout.read1(65536):
                    pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1);line+=b'\n'
                        epoch=log_epoch(line,self.zone)
                        if epoch is not None:
                            seen_header=True
                            in_window=epoch>=self.capture_begin and (self.capture_end is None or epoch<=self.capture_end)
                        elif not seen_header and ERROR_RE.search(line.decode('utf8','replace')):
                            self.stream_error='HiLog 返回错误，详见 hilog_status.txt'
                        if not in_window:
                            if status_bytes<32768:status.write(line);status_bytes+=len(line)
                            continue
                        if self.bytes+len(line)>LOG_LIMIT:self.truncated=True;continue
                        output.write(line);self.bytes+=len(line);self.lines+=1
                    output.flush();status.flush()
                    if len(pending)>1024*1024:
                        self.stream_error='系统日志单行过长，部分数据未保存';pending=b''
                if pending:self.stream_error=self.stream_error or '日志末行不完整，详见采集覆盖状态'
            if not self.stop.is_set():self.stream_error=self.stream_error or 'HiLog 在测试结束前退出，日志覆盖不完整'
        except Exception as exc:self.stream_error='系统日志保存失败：'+str(exc)

    def _read_fault(self,name,index,deadline):
        # Export through the documented Hiview query; never clear device logs.
        path=self.out/('fault_%03d.log'%index)
        import shlex
        args=['hidumper','-s','1201','-a','-p Faultlogger -f '+name]
        command=[self.hdc.executable]+(['-t',self.hdc.target] if self.hdc.target else [])+['shell',' '.join(shlex.quote(x) for x in args)]
        with path.open('wb') as output:
            proc=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                while proc.poll() is None:
                    if time.monotonic()>=deadline or path.stat().st_size>FILE_LIMIT:raise TimeoutError('故障导出达到时长或单文件大小上限')
                    time.sleep(.05)
            finally:
                if proc.poll() is None:proc.kill()
                proc.wait(timeout=3)
        if proc.returncode or path.stat().st_size>FILE_LIMIT:raise ValueError('故障导出失败或超出文件上限')
        with path.open('rb') as stream:text=stream.read(65536).decode('utf8','replace')
        return path,text

    def finish(self,monkey=None):
        if self.finished is not None:return self.finished
        try:
            self.end,zone=self.clock();self.capture_end=self.end
            if self.begin is None or zone!=self.zone or abs((self.end-self.begin)-(time.monotonic()-self.began_mono))>5:
                self.clock_ok=False;self.errors.append('设备时间或时区发生变化，无法确认本轮故障归属')
        except Exception as exc:self.clock_ok=False;self.errors.append('结束时间：'+str(exc))
        if self.proc and self.proc.poll() is not None:self.stream_error=self.stream_error or 'HiLog 在测试结束前退出，日志覆盖不完整'
        self.stop.set()
        if self.proc:
            try:
                if self.proc.poll() is None:self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=3)
            if self.reader:self.reader.join(timeout=3)
            if self.reader and self.reader.is_alive():self.stream_error='系统日志读取线程未及时退出'
            else:self.proc.stdout.close()
        # Faultlogger may flush after the triggering operation; this is outside event timing.
        time.sleep(2)
        try:
            files=self.listing();self.list_ok=True
            write(self.out/'final_index.json',dict(files=sorted(files),device_epoch=self.end))
            candidates=[]
            for name in sorted(files-self.baseline):
                m=FAULT_RE.fullmatch(name)
                if int(m[3])!=self.uid or not matches_process(m[2],self.bundle,self.process):continue
                when=stamp_time(m[4],self.zone)
                if self.clock_ok and (when>self.end or when+(1 if len(m[4])==14 else 0)<self.begin):continue
                candidates.append(name)
            deadline=time.monotonic()+20
            for index,name in enumerate(candidates[:20],1):
                try:
                    if time.monotonic()>=deadline:raise TimeoutError('故障日志导出达到总时长上限')
                    path,text=self._read_fault(name,index,min(deadline,time.monotonic()+8))
                    if not self.clock_ok:raise ValueError('已保存候选故障，但设备时间不可验证，不能确认属于本轮')
                    item=assess_fault(name,text,self.bundle,self.uid,self.process,self.begin,self.end,self.zone)
                    if item:
                        item['evidence']='logs/diagnostics/'+path.name;self.incidents.append(item)
                        self.emit('log','检测到'+KINDS[item['kind']]+'：'+item['at']+'；证据 '+item['evidence'])
                except Exception as exc:self.errors.append(name+'：'+str(exc))
            if len(candidates)>20:self.errors.append('候选故障超过 20 个，已限制导出数量')
        except Exception as exc:self.errors.append('结束故障列表：'+str(exc))
        reason=(monkey or {}).get('stop_reason','')
        if '超时' in reason or re.search(r'timeout|timed out',reason,re.I):
            self.incidents.append(dict(kind='suspected_freeze',confirmed=False,at=core.timestamp(),
                reason='Monkey 诊断或输入命令超时；可能为应用无响应，也可能为 USB / 设备 / 工具异常。'+reason,
                evidence='logs/monkey_events.jsonl',event_index=(monkey or {}).get('actual_events')))
        if self.stream_error:self.errors.append(self.stream_error)
        if not self.lines:self.errors.append('未收到本轮时间范围内的 HiLog 输出，无法确认系统日志覆盖；不将空日志视为无异常')
        if self.truncated:self.errors.append('系统日志达到 100 MiB 上限，后续内容未保存')
        coverage='complete' if self.clock_ok and self.baseline_ok and self.list_ok and not self.errors and self.lines>0 else 'partial'
        self.finished=dict(status=coverage,bundle=self.bundle,uid=self.uid,started_epoch=self.begin,ended_epoch=self.end,
            started_device_time=dt.datetime.fromtimestamp(self.begin,self.zone).isoformat() if self.begin is not None else None,
            ended_device_time=dt.datetime.fromtimestamp(self.end,self.zone).isoformat() if self.end is not None else None,
            device_timezone=str(self.zone),incidents=self.incidents,errors=list(dict.fromkeys(self.errors)),
            crash_count=sum(i['kind'] in ('jscrash','cppcrash') and i['confirmed'] for i in self.incidents),
            freeze_count=sum(i['kind']=='appfreeze' and i['confirmed'] for i in self.incidents),
            suspected_count=sum(not i['confirmed'] for i in self.incidents),
            hilog=dict(file='logs/diagnostics/system_hilog.log',lines=self.lines,bytes=self.bytes,truncated=self.truncated,
                       status='captured' if self.lines and not self.stream_error and not self.truncated else 'partial' if self.lines else 'unavailable'),
            scope='Monkey 阶段（含准备）；系统现有 HiLog 输出与已落盘 Faultlogger。只确认目标 UID、进程及设备时间范围匹配的故障；未发现不等于无异常。')
        write(self.out/'commands.json',self.commands);write(self.out/'summary.json',self.finished)
        self.emit('log','稳定性日志：已确认崩溃 %d / AppFreeze %d / 疑似异常 %d；覆盖 %s'%
                  (self.finished['crash_count'],self.finished['freeze_count'],self.finished['suspected_count'],coverage))
        return self.finished

def report_section(before,after):
    esc=lambda value:html.escape(str(value))
    result='<section><h2>Monkey 系统日志与稳定性异常</h2>'
    for label,data in [('改造前',before),('改造后',after)]:
        d=data.get('diagnostics',{});items=d.get('incidents',[])
        result+='<h3>'+label+'</h3><p>日志覆盖：'+esc({'complete':'已采集（受系统日志可见范围限制）','partial':'不完整','not_selected':'未选择 Monkey'}.get(d.get('status'),'未记录'))+'</p>'
        result+='<p>设备时间范围：'+esc(d.get('started_device_time') or '—')+' → '+esc(d.get('ended_device_time') or '—')+'</p>'
        if items:
            result+='<table><tr><th>类型</th><th>判定</th><th>时间</th><th>原因 / 证据文件</th></tr>'
            for item in items:
                color='#b91c1c' if item.get('confirmed') else '#92400e'
                result+='<tr style="color:'+color+'"><td>'+esc(KINDS.get(item.get('kind'),'异常'))+'</td><td>'+('系统故障日志确认' if item.get('confirmed') else '疑似，需核查')+'</td><td>'+esc(item.get('at','—'))+'</td><td>'+esc(item.get('reason',''))+'<br>'+esc(item.get('evidence',''))+'</td></tr>'
            result+='</table>'
        else:result+='<p>未发现已确认的本轮异常；日志缺失、系统未上报或延迟落盘的故障可能无法发现。</p>'
        result+='<p>系统日志：'+esc(d.get('hilog',{}).get('file','未记录'))+'；保存行数：'+esc(d.get('hilog',{}).get('lines','—'))+'。'+esc('；'.join(d.get('errors',[])))+'</p>'
    return result+'<p>红色为系统证据确认的崩溃 / AppFreeze；棕色为疑似异常。证据文件位于各组原始结果目录，HTML 不内嵌整份系统日志。自动拉回不代表崩溃；画面不变不作为卡死证据。日志仅保存在本机。</p></section>'
