"""Desktop before/after performance comparison for HarmonyOS services."""
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
import impact_engine as engine
import harmony_startup as core
import comparison_job
import monkey_runner
import foreground_app
import network_env
import app_meta
from update_ui import UpdateMixin

BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
CACHE_MODES={'系统页面：测试前后自动读取':'system_ui','缓存目录：资源阶段实时监控':'directory'}
TEST_MODES={label:key for key,label in engine.TEST_NAMES.items()}


class App(UpdateMixin,tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(app_meta.APP_NAME+' · v'+app_meta.VERSION)
        self.geometry('%dx%d'%(min(1280,self.winfo_screenwidth()-80),min(900,self.winfo_screenheight()-100)))
        self.minsize(1000, 720)
        self.configure(bg='#f3f6fa')
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.worker = None
        self.closing = False
        self.batch_running = False
        self.last_out, self.last_report = None, None
        self.datasets = {}
        self.live_role = None
        self.controls = []
        self.sample_rows = []
        self.cache_snapshots = []
        self.page_cache_active = False
        self.settings_path = (Path(os.environ.get('LOCALAPPDATA',str(BASE)))/'ServiceImpact' if getattr(sys,'frozen',False) else BASE)/'impact_settings.json'
        saved = {}
        try:
            settings_source=self.settings_path if self.settings_path.exists() else BASE/'impact_settings.json'
            saved = json.loads(settings_source.read_text(encoding='utf-8'))
            if not isinstance(saved, dict): saved = {}
        except (OSError, ValueError):
            pass
        defaults = dict(count='10', duration='60', interval='5', settle='3',
                        scenario='首页停留', out=str(BASE/'results'), threshold='10', hdc='', target='',
                        cache_mode='系统页面：测试前后自动读取',storage_unit_base='1000',test_mode='冷热启动',
                        monkey_seconds='60',monkey_events='100',monkey_interval_ms='500',monkey_seed='42',monkey_in_app='1',
                        network_mode='当前网络（只记录）',network_reqable_preset='较慢',network_ping_host='',
                        network_reqable_dir=network_env.default_config_dir(),network_api_port='9090',network_proxy_host='')
        self.v = {k:tk.StringVar(value=str(saved.get(k,v))) for k,v in defaults.items()}
        if 'network_mode' not in saved or self.v['network_mode'].get()=='Reqable 弱网（Wi-Fi 代理）':
            self.v['network_mode'].set('内置网络模拟（自动代理）')
        self.network_proxy_confirmed=tk.BooleanVar(value=False)
        self.network_status=tk.StringVar(value='尚未检查网络')
        for key in ('network_mode','network_reqable_preset','network_reqable_dir'):
            self.v[key].trace_add('write',lambda *args:self.network_proxy_confirmed.set(False))
        self.monitor_only = tk.BooleanVar(value=False)
        self.group = {}
        for role,label in [('before','改造前'),('after','改造后')]:
            group_saved = saved.get(role,{})
            if not isinstance(group_saved,dict): group_saved = {}
            self.group[role] = {key:tk.StringVar(value=str(group_saved.get(key, default))) for key,default in
                                [('bundle',saved.get('bundle',core.DEFAULT_BUNDLE if role=='before' else '')),
                                 ('ability',saved.get('ability','')),('module',saved.get('module','')),
                                 ('cache_path',saved.get('cache_path','')),
                                 ('label',label),('artifact',''),('cache_start',''),('cache_end',''),('result','')]}
        self.state_text = tk.StringVar(value='填写两个已安装包名，选择测试方式，点击一键对比自动测试并生成报告。')
        self.summary_text = tk.StringVar(value='等待两组数据')
        self.cpu, self.memory, self.cache = [tk.StringVar(value='—') for _ in range(3)]
        self.traffic_text=tk.StringVar(value='应用 UID 流量：等待采集 · 显示上传 / 下载速率及累计消耗')
        self.init_updates(saved)
        self.build_ui()
        if self.update_auto.get() and self.update_repo.get():self.after(1800,lambda:self.check_updates(quiet=True))
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.after(100,self.poll)

    def build_ui(self):
        import desktop_view
        desktop_view.build(self)

    def label(self,parent,text,row,col):
        ttk.Label(parent,text=text).grid(row=row,column=col,sticky='w',padx=(0,8),pady=5)

    def entry(self,parent,var,row,col,span=1,width=None):
        e=ttk.Entry(parent,textvariable=var,width=width)
        e.grid(row=row,column=col,columnspan=span,sticky='ew',padx=(0,8),pady=5)
        self.controls.append(e)
        return e

    def button(self,parent,text,command,row,col,span=1,primary=False):
        b=ttk.Button(parent,text=text,command=command,style='Primary.TButton' if primary else 'TButton')
        b.grid(row=row,column=col,columnspan=span,sticky='ew',padx=3,pady=5)
        self.controls.append(b)
        return b

    def choose_out(self):
        p=filedialog.askdirectory(parent=self,title='选择保存目录')
        if p:self.v['out'].set(p)

    def network_config(self,strict=True):
        c={k:self.v[k].get().strip() for k in ('hdc','target','network_mode','network_reqable_preset','network_reqable_dir','network_ping_host','network_api_port','network_proxy_host')}
        c['network_auto']=True
        c['out']=self.v['out'].get().strip()
        if c['network_mode'] not in network_env.MODES:raise ValueError('请选择网络环境')
        c['network_mode']=network_env.MODES[c['network_mode']]
        names=[p[0] for p in network_env.PRESETS]
        if c['network_reqable_preset'] not in names:raise ValueError('请选择 Reqable 预设')
        c['network_reqable_preset']=names.index(c['network_reqable_preset'])
        c['network_proxy_confirmed']=self.network_proxy_confirmed.get()
        if strict:network_env.validate(c)
        else:
            check=dict(c,network_proxy_confirmed=True);network_env.validate(check)
        return c

    def network_action(self,action):
        if self.update_busy:return
        if self.worker and self.worker.is_alive():return
        try:c=self.network_config(strict=False)
        except ValueError as exc:messagebox.showerror('网络设置',str(exc),parent=self);return
        self.cancel=threading.Event();self.network_status.set('检查中，请稍候…')
        for w in self.controls:w.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.worker=threading.Thread(target=network_env.run_action,args=(c,action,self.events,self.cancel),daemon=True)
        self.worker.start()

    def network_dialog(self):
        win=tk.Toplevel(self);win.title('自动网络环境 · 免费内置模拟');win.geometry('890x730');win.transient(self);win.grab_set()
        f=ttk.Frame(win,padding=20);f.pack(fill='both',expand=True);f.columnconfigure(1,weight=1)
        for row,(key,title,values) in enumerate([('network_mode','测试网络',[k for k,v in network_env.MODES.items() if v!='reqable']),('network_reqable_preset','内置网络预设',[p[0] for p in network_env.network_proxy.PROFILES])]):
            ttk.Label(f,text=title).grid(row=row,column=0,sticky='w',pady=6)
            ttk.Combobox(f,textvariable=self.v[key],values=values,state='readonly',width=45).grid(row=row,column=1,sticky='ew')
        preset_info=tk.StringVar()
        def describe_preset(*args):
            p=next((p for p in network_env.network_proxy.PROFILES if p[0]==self.v['network_reqable_preset'].get()),None)
            if p:preset_info.set('上传 %s kbps；下载 %s kbps；新连接延迟 %s ms；连接失败模拟 %s%%。'%p[1:])
        describe_preset();trace=self.v['network_reqable_preset'].trace_add('write',describe_preset)
        ttk.Label(f,textvariable=preset_info,wraplength=780).grid(row=2,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(f,text='免费内置代理，自动分配端口，通过 USB 连接手机；无需 Reqable、令牌或电脑 IP。',wraplength=820).grid(row=3,column=0,columnspan=2,sticky='w',pady=10)
        ttk.Label(f,text='选好环境后直接点击主界面“一键对比”。工具会备份原设置、切换预设、配置代理，两组测试完成或停止后恢复。异常退出后可点击“恢复上次环境”。',wraplength=820).grid(row=4,column=0,columnspan=2,sticky='w',pady=10)
        ttk.Label(f,text='手机保持解锁亮屏、USB 连接和已连接 Wi-Fi。内置模拟会关闭移动数据以避免回退；实际请求通过电脑网络出站。蜂窝模式需要有效 SIM 卡，开关不可用时会停止。',wraplength=820).grid(row=5,column=0,columnspan=2,sticky='w',pady=10)
        ttk.Label(f,text='支持 2G/3G/4G/5G、Wi-Fi 模拟、较快、较慢、极差、断网共 9 个预设。每组重置随机种子和计数；带宽为所有代理连接的合计上限。',wraplength=820).grid(row=6,column=0,columnspan=2,sticky='w',pady=10)
        ttk.Label(f,text='HTTPS 直接透传，不解密、不安装证书、不改正式包。代理只覆盖采用系统 HTTP 代理的流量，不覆盖绕过代理的 UDP/QUIC。连接失败率不等于底层 IP 丢包率，模拟制式不等于真实 3G/4G/5G。',wraplength=820,foreground='#63748d').grid(row=7,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(f,text='报告记录配置、转发连接数与字节数；这些计数包含设备其他应用，不能证明所有目标业务已被覆盖。',wraplength=820,foreground='#63748d').grid(row=8,column=0,columnspan=2,sticky='w',pady=8)
        actions=ttk.Frame(f);actions.grid(row=9,column=0,columnspan=2,sticky='ew',pady=8)
        ttk.Button(actions,text='检查当前环境',command=lambda:self.network_action('check')).pack(side='left',padx=4)
        ttk.Button(actions,text='试切换并恢复',command=lambda:self.network_action('prepare')).pack(side='left',padx=4)
        ttk.Button(actions,text='恢复上次环境',command=lambda:self.network_action('restore')).pack(side='left',padx=4)
        ttk.Button(actions,text='停止检查',command=lambda:self.cancel.set()).pack(side='left',padx=4)
        ttk.Label(f,textvariable=self.network_status,wraplength=790).grid(row=10,column=0,columnspan=2,sticky='w',pady=8)
        def close():
            self.v['network_reqable_preset'].trace_remove('write',trace)
            win.destroy()
        ttk.Button(f,text='完成',command=close).grid(row=11,column=1,sticky='e',pady=8)
        win.protocol('WM_DELETE_WINDOW',close)

    def detect_bundle(self,role):
        if self.update_busy:return
        if self.worker and self.worker.is_alive():return
        config={key:self.v[key].get().strip() for key in ('hdc','target')}
        self.cancel=threading.Event()
        for widget in self.controls:widget.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.state_text.set('正在读取手机当前前台应用，请保持目标页面…')
        self.worker=threading.Thread(target=foreground_app.run,args=(config,role,self.events,self.cancel),daemon=True)
        self.worker.start()

    def choose_artifact(self,role):
        p=filedialog.askopenfilename(parent=self,title='选择'+('改造前' if role=='before' else '改造后')+' APP/HAP',filetypes=[('应用包','*.app *.hap')])
        if p:self.group[role]['artifact'].set(p)

    def advanced(self):
        win=tk.Toplevel(self); win.title('连接与缓存设置'); win.geometry('760x420'); win.transient(self); win.grab_set()
        f=ttk.Frame(win,padding=20); f.pack(fill='both',expand=True); f.columnconfigure(1,weight=1)
        for r,(key,label) in enumerate([('hdc','hdc.exe 路径'),('target','设备 ID'),('settle','稳定等待 / 秒'),('storage_unit_base','页面 KB/MB 基数')]):
            self.label(f,label,r,0)
            ttk.Entry(f,textvariable=self.v[key]).grid(row=r,column=1,sticky='ew',pady=7)
        def pick():
            p=filedialog.askopenfilename(parent=win,title='选择 hdc.exe',filetypes=[('hdc','hdc.exe')])
            if p:self.v['hdc'].set(p)
        ttk.Button(f,text='浏览',command=pick).grid(row=0,column=2,padx=7)
        ttk.Label(f,text='hdc、设备留空时自动识别；启动入口在每组的本包设置中填写。\n页面 KB/MB 默认按 1000 换算；确认系统采用二进制时填 1024。\n页面显示值可能经过舍入，报告始终保留原文。',wraplength=650).grid(row=4,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Button(f,text='完成',command=win.destroy).grid(row=5,column=1,sticky='e')

    def group_advanced(self,role):
        win=tk.Toplevel(self);win.title(('改造前' if role=='before' else '改造后')+' · 本包设置')
        win.geometry('760x470');win.transient(self);win.grab_set()
        f=ttk.Frame(win,padding=20);f.pack(fill='both',expand=True);f.columnconfigure(1,weight=1)
        for row,(key,label) in enumerate([('ability','启动 Ability'),('module','模块'),('cache_path','设备缓存绝对路径'),('cache_start','目录模式补录开始 / MiB'),('cache_end','目录模式补录结束 / MiB')]):
            self.label(f,label,row,0)
            ttk.Entry(f,textvariable=self.group[role][key]).grid(row=row,column=1,sticky='ew',pady=7)
        ttk.Label(f,text='仅影响本组。入口留空时按本组包名自动识别；缓存目录与补录仅用于目录模式；系统页面模式自动读取缓存。',wraplength=680).grid(row=5,column=0,columnspan=2,pady=12)
        ttk.Button(f,text='完成',command=win.destroy).grid(row=6,column=1,sticky='e')

    @staticmethod
    def cache_number(text):
        if not text.strip():return None
        value=float(text)
        if not math.isfinite(value) or value<0:raise ValueError('缓存读数需要是非负数字（MiB）')
        return value

    def settings(self):
        return {'update_repository':self.update_repo.get(),'update_auto':self.update_auto.get(),**{k:v.get() for k,v in self.v.items()},**{role:{k:v.get() for k,v in vals.items()} for role,vals in self.group.items()}}

    def start(self,role):
        if self.update_busy:return
        if self.worker and self.worker.is_alive():return
        try:
            c=self.get_config(role)
        except (ValueError,OSError,core.argparse.ArgumentTypeError) as exc:
            messagebox.showerror('检查设置',str(exc),parent=self); return
        try:engine.save_json(self.settings_path,self.settings())
        except OSError:pass
        self.batch_running=False;self.cancel=threading.Event()
        self.prepare_live(c)
        for w in self.controls:w.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.worker=threading.Thread(target=engine.collect,args=(c,self.events,self.cancel),daemon=True)
        self.worker.start()

    def get_config(self,role):
        c={k:v.get().strip() for k,v in self.v.items()}
        c.update({k:v.get().strip() for k,v in self.group[role].items()})
        core.identifier(c['bundle'])
        c['count']=int(c['count']);c['duration']=float(c['duration']);c['interval']=float(c['interval']);c['settle']=float(c['settle'])
        if c['cache_mode'] not in CACHE_MODES:raise ValueError('请选择有效的缓存采集方式')
        c['cache_mode']=CACHE_MODES[c['cache_mode']]
        if c['test_mode'] not in TEST_MODES:raise ValueError('请选择有效的测试方式')
        c['test_mode']=TEST_MODES[c['test_mode']]
        c['monitor_only']=c['test_mode'] in ('monkey','resources')
        if c['test_mode'] in ('monkey','both_monkey'):monkey_runner.validate(c)
        c['storage_unit_base']=int(c['storage_unit_base'])
        if c['storage_unit_base'] not in (1000,1024):raise ValueError('页面单位基数只能为 1000 或 1024')
        if not 1<=c['count']<=1000:raise ValueError('次数范围为 1～1000')
        if not 5<=c['duration']<=86400:raise ValueError('监控时长范围为 5～86400 秒')
        if not 1<=c['interval']<=c['duration']:raise ValueError('采样间隔至少 1 秒，且不能大于监控时长')
        core.positive_float(c['settle'])
        threshold=float(c['threshold'])
        if not math.isfinite(threshold) or threshold<0:raise ValueError('关注阈值需要是非负有限数值')
        if not c['scenario'] or not c['label']:raise ValueError('请填写操作场景与版本备注')
        if not c['out']:raise ValueError('请选择结果保存目录')
        if c['artifact'] and (not Path(c['artifact']).is_file() or Path(c['artifact']).suffix.lower() not in ('.app','.hap')):
            raise ValueError('包文件不存在，或不是 APP/HAP 文件')
        for key in ('ability','module'):
            if c[key]:core.identifier(c[key])
        c['cache_start']=self.cache_number(c['cache_start']);c['cache_end']=self.cache_number(c['cache_end']);c['role']=role
        path=Path(c['out']).expanduser().resolve();path.mkdir(parents=True,exist_ok=True)
        import tempfile
        with tempfile.TemporaryFile(dir=path):pass
        c['out']=str(path)
        c.update(self.network_config())
        return c

    def prepare_live(self,c):
        self.live_role=c['role'];self.datasets.pop(c['role'],None)
        self.sample_rows=[];self.cache_snapshots=[]
        self.page_cache_active=c['cache_mode']=='system_ui'
        self.monitor_charts.reset(c)
        self.pair_charts.reset()
        self.cpu.set('—'); self.memory.set('—'); self.cache.set('—')
        self.traffic_text.set('应用 UID 流量：等待当前组有效基线；历史累计计数不会作为本轮消耗。')
        self.resource_table.delete(*self.resource_table.get_children()); self.draw_cache()
        self.state_text.set('正在采集'+('改造前' if c['role']=='before' else '改造后')+'：'+c['bundle'])
        self.tabs.select(1)

    def start_batch(self):
        if self.update_busy:return
        if self.worker and self.worker.is_alive():return
        try:
            configs=[self.get_config(role) for role in ('before','after')]
            if configs[0]['bundle']==configs[1]['bundle']:raise ValueError('一键对比需要两个不同包名；同包名更换版本请分别采集')
        except (ValueError,OSError,core.argparse.ArgumentTypeError) as exc:
            messagebox.showerror('检查两组设置',str(exc),parent=self);return
        try:engine.save_json(self.settings_path,self.settings())
        except OSError:pass
        self.cancel=threading.Event();self.batch_running=True;self.datasets={}
        for role in self.group:self.group[role]['result'].set('')
        self.compare_table.delete(*self.compare_table.get_children());self.summary_text.set('本轮一键对比正在采集')
        self.prepare_live(configs[0])
        for w in self.controls:w.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.worker=threading.Thread(target=comparison_job.run_pair,args=(configs,self.events,self.cancel,float(self.v['threshold'].get())),daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel.set(); self.stop_button.configure(state='disabled'); self.state_text.set('正在停止并保存…')

    def append_log(self,text):
        self.log.configure(state='normal'); self.log.insert('end',text+'\n'); self.log.see('end'); self.log.configure(state='disabled')

    def draw_cache(self):
        self.monitor_charts.request_draw()
        self.monitor_charts.pair.request_draw()
        self.pair_charts.request_draw()

    def chart_datasets(self):
        data=dict(self.datasets)
        if self.live_role:
            data[self.live_role]={'resources':self.sample_rows,
                'environment':{'bundle':self.group[self.live_role]['bundle'].get()},
                'cache':{'source':'system_storage_page' if self.page_cache_active else 'directory','checkpoints':self.cache_snapshots}}
        return data

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='update_done':
                    self.update_complete(value)
                    if self.installing:return
                elif kind=='update_progress':self.update_progress.set(value)
                elif kind=='log':self.append_log(value)
                elif kind=='out':self.last_out=value
                elif kind=='phase':self.state_text.set(value)
                elif kind=='group_start':self.prepare_live(value)
                elif kind=='network_done':
                    for w in self.controls:w.configure(state='readonly' if isinstance(w,ttk.Combobox) else 'normal')
                    self.stop_button.configure(state='disabled')
                    if self.closing:self.destroy();return
                    text='已取消网络检查' if value['cancelled'] else network_env.describe(value['result']) if value['result'] else ''
                    if value.get('result',{}):
                        p=value['result'].get('ping')
                        if p:text+='\nPing：'+json.dumps(p,ensure_ascii=False)
                    if value['error']:text+='\n'+value['error']
                    if value.get('message'):text+='\n'+value['message']
                    self.network_status.set(text);self.append_log(text);self.state_text.set('网络检查结束，请查看网络设置中的结果')
                elif kind=='detect_done':
                    for w in self.controls:w.configure(state='readonly' if isinstance(w,ttk.Combobox) else 'normal')
                    self.stop_button.configure(state='disabled')
                    if self.closing:self.destroy();return
                    if value['cancelled']:self.state_text.set('已取消获取包名')
                    elif value['error']:
                        self.state_text.set('获取包名失败，原输入已保留')
                        self.append_log(value['error'])
                        messagebox.showerror('获取当前应用包名',value['error'],parent=self)
                    else:
                        role=value['role'];result=value['result'];group=self.group[role]
                        if group['bundle'].get().strip()!=result['bundle']:
                            for key in ('ability','module','cache_path','artifact','cache_start','cache_end','result'):group[key].set('')
                            self.datasets.pop(role,None)
                            self.draw_cache()
                            self.compare_table.delete(*self.compare_table.get_children())
                            self.summary_text.set('目标包已更新，请重新采集该组数据')
                            self.last_report=None
                        group['bundle'].set(result['bundle'])
                        self.state_text.set(('改造前' if role=='before' else '改造后')+'包名已获取：'+result['bundle'])
                        self.append_log('当前应用：'+result['bundle']+'；版本 '+str(result['version'])+'；设备 '+result['device'])
                        try:engine.save_json(self.settings_path,self.settings())
                        except OSError:pass
                elif kind=='monkey_event':self.state_text.set('随机 Monkey：已执行 %s 个事件，耗时 %.1f 秒'%(value['index'],value['elapsed_s']))
                elif kind=='startup':
                    self.append_log('%s 第%s轮：%s，%s ms' %(value['requested_mode'],value['round'],value['status'],value['total_time_ms']))
                    self.state_text.set('启动测量：%s 第 %s 轮'%(value['requested_mode'],value['round']))
                elif kind=='cache_snapshot':
                    self.cache_snapshots.append(value)
                    self.cache.set(value['raw_text'] if value['status']=='ok' else '页面读取失败')
                    self.draw_cache()
                elif kind=='resource':
                    from traffic_monitor import size
                    self.traffic_text.set('当前组流量：上传 '+size(value.get('traffic_upload_bytes'))+' / 下载 '+size(value.get('traffic_download_bytes'))+' / 合计 '+size(value.get('traffic_total_bytes'))+'；速率 ↑ '+engine.fmt(value.get('upload_kib_s'))+' / ↓ '+engine.fmt(value.get('download_kib_s'))+' KiB/s'+('；'+value['traffic_note'] if value.get('traffic_note') else ''))
                    self.sample_rows.append(value)
                    self.cpu.set(engine.fmt(value['cpu_pct'])+' %' if value['cpu_pct'] is not None else '等待 / 不可用')
                    self.memory.set(engine.fmt(value['pss_mib'])+' MiB' if value['pss_mib'] is not None else '不可用')
                    if not self.page_cache_active:self.cache.set(engine.fmt(value['cache_mib'])+' MiB' if value['cache_mib'] is not None else '无法自动读取')
                    note='；'.join(filter(None,[value.get('cpu_note'),value.get('memory_note'),value.get('cache_note'),value.get('traffic_note')]))
                    self.resource_table.insert('', 'end', values=(engine.fmt(value['elapsed_s']),engine.fmt(value['cpu_pct']),engine.fmt(value['pss_mib']),engine.fmt(value['cache_mib']),note))
                    self.resource_table.yview_moveto(1); self.draw_cache()
                elif kind in ('done','group_done'):
                    self.live_role=None
                    if kind=='done':
                        for w in self.controls:w.configure(state='readonly' if isinstance(w,ttk.Combobox) else 'normal')
                        self.stop_button.configure(state='disabled')
                    data=value['result']
                    if data:
                        role=data['role'];self.datasets[role]=data
                        self.group[role]['result'].set(str(Path(value['out'])/'result.json'))
                        self.state_text.set({'complete':'采集完成','interrupted':'已停止','error':'采集失败'}.get(data['state'],data['state']))
                        if data['error']:self.append_log(data['error'])
                        self.refresh_compare(silent=True)
                    else:self.state_text.set('采集失败，请查看日志')
                    if self.closing and kind=='done':self.destroy();return
                elif kind=='traffic_summary':
                    from traffic_monitor import size
                    self.traffic_text.set('本组消耗：上传 '+size(value.get('upload_bytes'))+' / 下载 '+size(value.get('download_bytes'))+' / 合计 '+size(value.get('total_bytes'))+'；'+value.get('coverage','覆盖范围未知'))
                elif kind=='batch_done':
                    self.live_role=None
                    self.batch_running=False
                    for w in self.controls:w.configure(state='readonly' if isinstance(w,ttk.Combobox) else 'normal')
                    self.stop_button.configure(state='disabled')
                    self.datasets={role:d for role,d in value['results'].items() if d}
                    self.last_out=value.get('out');self.last_report=value.get('report')
                    self.refresh_compare(silent=True)
                    self.state_text.set({'complete':'一键对比完成，报告已生成','interrupted':'已停止，已保留本轮结果','error':'一键对比未完成，请查看日志和报告'}[value['state']])
                    if value['error']:self.append_log(value['error'])
                    if self.last_report:
                        self.append_log('本轮报告：'+self.last_report)
                        if not self.closing and value['state']!='interrupted':webbrowser.open(Path(self.last_report).as_uri())
                    if self.closing:self.destroy();return
        except queue.Empty:pass
        self.after(120,self.poll)

    def import_result(self,role):
        if self.worker and self.worker.is_alive():return
        path=filedialog.askopenfilename(parent=self,title='选择 '+role+' 的 result.json',filetypes=[('采集结果','*.json')])
        if not path:return
        try:
            data=engine.load_result(path);self.datasets[role]=data;self.group[role]['result'].set(path)
            self.live_role=None
            self.monitor_charts.pair.reset();self.pair_charts.reset()
            self.group[role]['bundle'].set(data['environment'].get('bundle',''))
            self.group[role]['label'].set(data.get('label',role))
            for key in ('ability','module','cache_path'):
                self.group[role][key].set(data.get('config',{}).get(key) or '')
            self.group[role]['cache_start'].set('' if data.get('cache',{}).get('manual_start_mib') is None else str(data['cache']['manual_start_mib']))
            self.group[role]['cache_end'].set('' if data.get('cache',{}).get('manual_end_mib') is None else str(data['cache']['manual_end_mib']))
            self.refresh_compare(silent=True)
        except (ValueError,OSError,KeyError,TypeError) as exc:messagebox.showerror('无法导入',str(exc),parent=self)

    def refresh_compare(self,silent=False):
        self.draw_cache()
        if 'before' not in self.datasets or 'after' not in self.datasets:
            self.summary_text.set('请先采集或导入改造前、改造后两组 result.json。')
            if not silent:messagebox.showinfo('缺少数据',self.summary_text.get(),parent=self)
            return None
        try:
            for role,data in self.datasets.items():
                data.setdefault('cache',{})['manual_start_mib']=self.cache_number(self.group[role]['cache_start'].get())
                data['cache']['manual_end_mib']=self.cache_number(self.group[role]['cache_end'].get())
            threshold=float(self.v['threshold'].get())
            result=engine.comparison(self.datasets['before'],self.datasets['after'],threshold)
            self.compare_table.delete(*self.compare_table.get_children())
            for r in result['rows']:
                self.compare_table.insert('', 'end',values=(r['label']+' / '+r['unit'],engine.fmt(r['before']),engine.fmt(r['after']),engine.fmt(r['delta'],True),engine.fmt(r['percent'],True),r['status']))
            self.summary_text.set(result['conclusion']+'。'+'；'.join((result['warnings']+result.get('notes',[]))[:3]))
            return result
        except (ValueError,KeyError,TypeError) as exc:
            if not silent:messagebox.showerror('无法对比',str(exc),parent=self)
            else:self.append_log('对比数据不完整：'+str(exc))
            return None

    def make_report(self):
        if self.refresh_compare() is None:return
        try:
            output=engine.report(self.datasets['before'],self.datasets['after'],self.v['out'].get(),float(self.v['threshold'].get()))
            self.last_report=str(output/'report.html');self.last_out=str(output)
            self.append_log('报告已保存：'+self.last_report)
            webbrowser.open(Path(self.last_report).as_uri())
        except (OSError,ValueError) as exc:messagebox.showerror('报告生成失败',str(exc),parent=self)

    def open_out(self):
        p=Path(self.last_out or self.v['out'].get())
        try:
            if p.is_dir():os.startfile(str(p.resolve()))
            else:messagebox.showinfo('结果目录','请先采集数据或选择有效目录。',parent=self)
        except OSError as exc:messagebox.showerror('无法打开目录',str(exc),parent=self)

    def close(self):
        self.update_cancel.set()
        if self.worker and self.worker.is_alive():self.closing=True;self.stop()
        else:self.destroy()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--apply-update':
        import update_installer
        raise SystemExit(update_installer.main(sys.argv[2]))
    if os.name=='nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
    App().mainloop()
