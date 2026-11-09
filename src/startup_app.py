"""Windows desktop UI for device-reported HarmonyOS startup timings."""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import harmony_startup as core

BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
SETTINGS = BASE / 'settings.json'
MODES = {'冷热启动': 'both', '仅冷启动': 'cold', '仅热启动': 'hot'}


class DesktopHdc(core.Hdc):
    def __init__(self, executable, target=None, timeout=30, cancel=None):
        super().__init__(executable, target, timeout)
        self.cancel = cancel or threading.Event()

    def run(self, args, log=None, allow_nonzero=False):
        if self.cancel.is_set():
            raise KeyboardInterrupt
        cmd = [self.executable] + (['-t', self.target] if self.target else []) + list(args)
        started, began = core.timestamp(), time.monotonic()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags, shell=False)
        stopped, timed_out = False, False
        while True:
            try:
                data, _ = proc.communicate(timeout=.15)
                break
            except subprocess.TimeoutExpired:
                stopped = self.cancel.is_set()
                timed_out = time.monotonic() - began >= self.timeout
                if stopped or timed_out:
                    proc.kill()
                    data, _ = proc.communicate()
                    break
        text = data.decode('utf-8', errors='replace')
        if log is not None:
            log.append({'at': started, 'command': cmd, 'returncode': proc.returncode,
                        'cancelled': stopped, 'timeout': timed_out, 'output': text})
        if stopped:
            raise KeyboardInterrupt
        if timed_out:
            raise core.MeasurementError('命令超时，未将等待时间记为启动耗时。')
        if proc.returncode and not allow_nonzero:
            raise core.MeasurementError('hdc 命令失败：' + text.strip()[:600])
        if re.search(r'\[Fail\]|device not found|No connected devices|Connect server failed|Not match target', text, re.I):
            raise core.MeasurementError('设备连接失败：' + text.strip()[:600])
        return text


def run_job(config, events, cancel, inspect=False):
    """Only this worker touches the device. UI updates use a queue."""
    out, rows, args = None, [], None
    interrupted, fatal = False, ''
    def emit(kind, value):
        events.put((kind, value))
    try:
        argv = ['--bundle', config['bundle'], '--count', config['count'], '--mode', MODES[config['mode']],
                '--out', config['out'], '--settle', config['settle'], '--background', config['background']]
        for key in ('hdc', 'target', 'ability', 'module'):
            if config.get(key, '').strip():
                argv += ['--' + key, config[key].strip()]
        args = core.parser().parse_args(argv)
        args.cancel = cancel
        hdc = DesktopHdc(core.find_hdc(args.hdc), args.target, args.timeout, cancel)
        emit('log', '正在连接设备并识别启动入口…')
        hdc.select_device()
        args.target = hdc.target
        if not re.search(r'(?<!\w)-W(?!\w)', hdc.shell('aa', 'start', '-h')):
            raise core.MeasurementError('设备不支持系统启动计时 aa start -W（需要 API 20+）。')
        info = core.bundle_json(hdc.shell('bm', 'dump', '-n', args.bundle))
        module, ability, process = core.resolve_entry(info, args.ability, args.module)
        args.module, args.ability = core.identifier(module), core.identifier(ability)
        args.process = core.identifier(process or args.bundle)
        emit('device', '%s · %s / %s · v%s' % (args.target, module, ability, info.get('versionName', '—')))
        if inspect:
            emit('log', '连接检查通过。未启动或停止应用。')
            emit('done', {'status': '连接检查通过', 'out': None, 'error': ''})
            return
        out = args.out.resolve() / (dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + args.bundle)
        (out / 'logs').mkdir(parents=True, exist_ok=False)
        emit('out', str(out))
        metadata = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k != 'cancel'}
        metadata.update(hdc=hdc.executable, app_version=info.get('versionName'), started_at=core.timestamp(),
                        metric='aa start -W TotalTime', timing_source=core.DOC_URL)
        (out / 'run_config.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        modes = ['cold', 'hot'] if args.mode == 'both' else [args.mode]
        with (out / 'samples.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=core.FIELDS)
            writer.writeheader()
            for mode in modes:
                for index in range(1, args.count + 1):
                    if cancel.is_set():
                        raise KeyboardInterrupt
                    label = '冷启动' if mode == 'cold' else '热启动'
                    emit('status', '%s %d / %d' % (label, index, args.count))
                    emit('log', '开始%s，第 %d / %d 轮。' % (label, index, args.count))
                    row, interrupted = core.measure(hdc, args, mode, index, out)
                    rows.append(row)
                    writer.writerow(row)
                    stream.flush()
                    core.save_summary(out, rows, args, interrupted)
                    emit('sample', row)
                    emit('summary', core.summarize(rows))
                    if interrupted:
                        raise KeyboardInterrupt
        emit('log', '全部轮次已结束，结果已保存。')
    except KeyboardInterrupt:
        interrupted = True
        emit('log', '已停止。已完成的记录保留；停止时应用界面不自动恢复。')
    except (Exception, SystemExit) as exc:
        fatal = str(exc) or '配置解析失败'
        emit('log', '错误：' + fatal)
    finally:
        if out and args:
            try:
                core.save_summary(out, rows, args, interrupted)
                (out / 'run_status.json').write_text(json.dumps({'interrupted': interrupted, 'error': fatal,
                    'finished_at': core.timestamp(), 'recorded_samples': len(rows)}, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                fatal = '无法保存汇总：' + str(exc)
                emit('log', fatal)
    if not inspect or fatal or interrupted:
        failed = sum(r['status'] != 'ok' for r in rows)
        status = '已停止' if interrupted else ('执行失败' if fatal else ('已完成 · %d 轮异常' % failed if failed else '测试完成'))
        emit('done', {'status': status, 'out': str(out) if out else None, 'error': fatal})


class StartupApp(tk.Tk):
    def __init__(self, settings_path=SETTINGS):
        super().__init__()
        self.title('启动耗时测试 · HarmonyOS')
        self.geometry('1000x820')
        self.minsize(880, 760)
        self.configure(bg='#f3f5f9')
        self.settings_path = Path(settings_path)
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.worker, self.last_out, self.close_pending = None, None, False
        self.completed, self.expected = 0, 0
        self.controls = []
        self._style()
        saved = {}
        try:
            saved = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            pass
        defaults = {'bundle': core.DEFAULT_BUNDLE, 'count': '10', 'mode': '冷热启动',
                    'out': str(BASE / 'results'), 'hdc': '', 'target': '', 'ability': '', 'module': '',
                    'settle': '3', 'background': '2'}
        self.values = {k: tk.StringVar(value=str(saved.get(k, v))) for k, v in defaults.items()}
        if self.values['mode'].get() not in MODES:
            self.values['mode'].set('冷热启动')
        self.status = tk.StringVar(value='准备就绪')
        self.device = tk.StringVar(value='连接手机并解锁，然后点击「检查连接」。')
        self.cold_summary = tk.StringVar(value='—')
        self.hot_summary = tk.StringVar(value='—')
        self._layout()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.after(100, self.poll)

    def _style(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('.', font=('Microsoft YaHei UI', 10), background='#f3f5f9', foreground='#18243a')
        s.configure('Card.TFrame', background='white')
        s.configure('Card.TLabel', background='white')
        s.configure('Muted.TLabel', foreground='#637189')
        s.configure('Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold'))
        s.configure('TEntry', padding=7, fieldbackground='white')
        s.configure('TSpinbox', padding=6, fieldbackground='white')
        s.configure('TCombobox', padding=6, fieldbackground='white')
        s.configure('TButton', padding=(14, 8), background='#e8edf5', borderwidth=0)
        s.map('TButton', background=[('active', '#dbe3ef')], foreground=[('disabled', '#94a0b3')])
        s.configure('Primary.TButton', background='#265bd9', foreground='white')
        s.map('Primary.TButton', background=[('disabled', '#b7c4e2'), ('active', '#194bbb')], foreground=[('disabled', '#eef2f9')])
        s.configure('Treeview', rowheight=30, background='white', fieldbackground='white', borderwidth=0)
        s.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'), padding=(8, 7), background='#eaf0f8')
        s.configure('Horizontal.TProgressbar', background='#265bd9', troughcolor='#e3e9f3', borderwidth=0)

    def entry(self, parent, key, row, col, width=None, span=1):
        widget = ttk.Entry(parent, textvariable=self.values[key], width=width)
        widget.grid(row=row, column=col, columnspan=span, sticky='ew', padx=(0, 10), pady=6)
        self.controls.append((widget, 'normal'))
        return widget

    def _layout(self):
        outer = ttk.Frame(self, padding=(26, 20))
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)
        ttk.Label(outer, text='启动耗时测试', style='Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(outer, text='HarmonyOS  ·  冷热启动批量测量与日志归档', style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=(4, 16))

        card = ttk.Frame(outer, style='Card.TFrame', padding=16)
        card.grid(row=2, column=0, sticky='ew')
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)
        for label, row, col in [('应用包名', 0, 0), ('测试方式', 1, 0), ('每种次数', 1, 2), ('日志保存到', 2, 0)]:
            ttk.Label(card, text=label, style='Card.TLabel').grid(row=row, column=col, sticky='w', padx=(0, 14))
        self.entry(card, 'bundle', 0, 1, span=4)
        combo = ttk.Combobox(card, textvariable=self.values['mode'], values=list(MODES), state='readonly', width=16)
        combo.grid(row=1, column=1, sticky='ew', padx=(0, 18), pady=6)
        self.controls.append((combo, 'readonly'))
        spin = ttk.Spinbox(card, textvariable=self.values['count'], from_=1, to=10000, width=9)
        spin.grid(row=1, column=3, sticky='w', pady=6)
        self.controls.append((spin, 'normal'))
        self.entry(card, 'out', 2, 1, span=3)
        browse = ttk.Button(card, text='选择目录', command=self.choose_out)
        browse.grid(row=2, column=4)
        self.controls.append((browse, 'normal'))
        self.advanced_button = ttk.Button(card, text='高级设置 ▸', command=self.toggle_advanced)
        self.advanced_button.grid(row=3, column=0, columnspan=2, sticky='w', pady=(7, 0))
        self.advanced = ttk.Frame(card, style='Card.TFrame')
        self.advanced.columnconfigure(1, weight=1)
        self.advanced.columnconfigure(3, weight=1)
        for text, row, col in [('hdc 路径', 0, 0), ('设备 ID', 1, 0), ('Ability', 1, 2),
                               ('模块', 2, 0), ('稳定等待 / 秒', 2, 2), ('后台停留 / 秒', 3, 0)]:
            ttk.Label(self.advanced, text=text, style='Card.TLabel').grid(row=row, column=col, sticky='w', padx=(0, 10))
        self.entry(self.advanced, 'hdc', 0, 1, span=3)
        b = ttk.Button(self.advanced, text='浏览', command=self.choose_hdc)
        b.grid(row=0, column=4)
        self.controls.append((b, 'normal'))
        for key, r, c in [('target', 1, 1), ('ability', 1, 3), ('module', 2, 1), ('settle', 2, 3), ('background', 3, 1)]:
            self.entry(self.advanced, key, r, c)
        ttk.Label(self.advanced, text='hdc、设备与入口留空时自动识别。', style='Card.TLabel').grid(row=3, column=2, columnspan=3, sticky='w')

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky='ew', pady=14)
        self.check_button = ttk.Button(actions, text='检查连接', command=lambda: self.start(True))
        self.check_button.pack(side='left')
        self.start_button = ttk.Button(actions, text='开始测试', style='Primary.TButton', command=self.start)
        self.start_button.pack(side='left', padx=10)
        self.stop_button = ttk.Button(actions, text='停止', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        self.open_button = ttk.Button(actions, text='打开结果目录', command=self.open_out)
        self.open_button.pack(side='right')

        stats = ttk.Frame(outer)
        stats.grid(row=4, column=0, sticky='ew')
        stats.columnconfigure(0, weight=1)
        stats.columnconfigure(1, weight=1)
        for col, title, value in [(0, '冷启动 · 平均耗时', self.cold_summary), (1, '热启动 · 平均耗时', self.hot_summary)]:
            pane = ttk.Frame(stats, style='Card.TFrame', padding=(16, 10))
            pane.grid(row=0, column=col, sticky='ew', padx=(0, 8) if col == 0 else (8, 0))
            ttk.Label(pane, text=title, style='Card.TLabel').pack(anchor='w')
            ttk.Label(pane, textvariable=value, style='Card.TLabel', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', pady=(4, 0))
        statusbar = ttk.Frame(outer)
        statusbar.grid(row=5, column=0, sticky='ew', pady=(12, 8))
        ttk.Label(statusbar, textvariable=self.status).pack(side='left')
        self.progress = ttk.Progressbar(statusbar, length=250)
        self.progress.pack(side='right', fill='x')

        tabs = ttk.Notebook(outer)
        tabs.grid(row=6, column=0, sticky='nsew')
        details = ttk.Frame(tabs)
        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(details, columns=('mode', 'round', 'actual', 'time', 'status', 'note'), show='headings', height=6)
        for key, name, width in [('mode', '测试类型', 90), ('round', '轮次', 55), ('actual', '实际类型', 85),
                                  ('time', '耗时 ms', 95), ('status', '结果', 90), ('note', '说明', 350)]:
            self.table.heading(key, text=name)
            self.table.column(key, width=width, minwidth=45, stretch=(key == 'note'), anchor='w' if key == 'note' else 'center')
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(details, orient='vertical', command=self.table.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.table.configure(yscrollcommand=scroll.set)
        self.table.tag_configure('bad', foreground='#b43c35')
        tabs.add(details, text='  测量明细  ')
        log_frame = ttk.Frame(tabs)
        self.log = tk.Text(log_frame, height=6, wrap='word', bg='white', fg='#24334d', relief='flat', padx=12, pady=10,
                           font=('Microsoft YaHei UI', 9), state='disabled')
        self.log.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=scrollbar.set)
        tabs.add(log_frame, text='  运行日志  ')
        ttk.Label(outer, textvariable=self.device, style='Muted.TLabel', wraplength=900).grid(row=7, column=0, sticky='w', pady=(10, 4))
        ttk.Label(outer, text='冷启动到首帧绘制完成；热启动到恢复前台。网页内容加载完成不在此计时范围内。',
                  style='Muted.TLabel', wraplength=900).grid(row=8, column=0, sticky='w')
        ttk.Label(outer, text='测试时保持手机解锁。冷启动会停止目标进程，保留应用数据；每次运行建立独立结果文件夹。',
                  style='Muted.TLabel', wraplength=900).grid(row=9, column=0, sticky='w', pady=(3, 0))

    def toggle_advanced(self):
        if self.advanced.winfo_ismapped():
            self.advanced.grid_remove()
            self.advanced_button.configure(text='高级设置 ▸')
            self.minsize(880, 760)
        else:
            self.advanced.grid(row=4, column=0, columnspan=5, sticky='ew', pady=(8, 0))
            self.advanced_button.configure(text='高级设置 ▾')
            self.minsize(880, 920)

    def choose_out(self):
        chosen = filedialog.askdirectory(title='选择日志和结果保存目录', initialdir=self.values['out'].get() if Path(self.values['out'].get()).is_dir() else str(BASE))
        if chosen:
            self.values['out'].set(chosen)

    def choose_hdc(self):
        chosen = filedialog.askopenfilename(title='选择 hdc.exe', filetypes=[('hdc', 'hdc.exe'), ('所有文件', '*.*')])
        if chosen:
            self.values['hdc'].set(chosen)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', '[%s] %s\n' % (dt.datetime.now().strftime('%H:%M:%S'), text))
        self.log.see('end')
        self.log.configure(state='disabled')

    def set_busy(self, busy):
        for widget, normal in self.controls:
            widget.configure(state='disabled' if busy else normal)
        self.check_button.configure(state='disabled' if busy else 'normal')
        self.start_button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if busy else 'disabled')

    def start(self, inspect=False):
        if self.worker and self.worker.is_alive():
            return
        config = {k: v.get().strip() for k, v in self.values.items()}
        try:
            core.identifier(config['bundle'])
            count = core.positive_int(config['count'])
            if count > 10000:
                raise ValueError('测试次数请设置为 1～10000。')
            core.positive_float(config['settle'])
            core.positive_float(config['background'])
            for key in ('module', 'ability'):
                if config[key]:
                    core.identifier(config[key])
            if not config['out']:
                raise ValueError('请选择日志保存目录。')
            if not inspect:
                path = Path(config['out']).expanduser().resolve()
                path.mkdir(parents=True, exist_ok=True)
                # Verify writability before touching the phone.
                import tempfile
                with tempfile.TemporaryFile(dir=path):
                    pass
                config['out'] = str(path)
        except (ValueError, OSError, core.argparse.ArgumentTypeError) as exc:
            messagebox.showerror('请检查设置', str(exc), parent=self)
            return
        try:
            self.settings_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            self.append_log('当前程序目录不可写，设置无法自动记忆；不影响指定目录保存结果。')
        self.cancel = threading.Event()
        self.completed = 0
        self.expected = count * (2 if config['mode'] == '冷热启动' else 1)
        if not inspect:
            self.table.delete(*self.table.get_children())
            self.cold_summary.set('—')
            self.hot_summary.set('—')
            self.last_out = None
        self.progress.configure(value=0, maximum=self.expected)
        self.status.set('检查连接…' if inspect else '准备测试…')
        self.set_busy(True)
        self.worker = threading.Thread(target=run_job, args=(config, self.events, self.cancel, inspect), daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel.set()
        self.stop_button.configure(state='disabled')
        self.status.set('正在停止并保存结果…')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.append_log(value)
                elif kind == 'device':
                    self.device.set(value)
                    self.append_log('连接成功：' + value)
                elif kind == 'status':
                    self.status.set(value)
                elif kind == 'out':
                    self.last_out = value
                    self.append_log('结果保存到：' + value)
                elif kind == 'sample':
                    self.completed += 1
                    self.progress.configure(value=self.completed)
                    labels = {'cold': '冷启动', 'hot': '热启动'}
                    ok = value['status'] == 'ok'
                    result = '有效' if ok else ('已停止' if value['status'] == 'interrupted' else '异常')
                    self.table.insert('', 'end', values=(labels.get(value['requested_mode'], ''), value['round'],
                        labels.get(value['actual_mode'], '—'), value['total_time_ms'] if value['total_time_ms'] != '' else '—', result,
                        value['error'] or '已保存'), tags=() if ok else ('bad',))
                    self.table.yview_moveto(1)
                    self.append_log('%s #%s：%s，%s' % (labels[value['requested_mode']], value['round'], result,
                                    str(value['total_time_ms']) + ' ms' if ok else value['error']))
                elif kind == 'summary':
                    for mode, variable in [('cold', self.cold_summary), ('hot', self.hot_summary)]:
                        stat = value.get(mode)
                        if stat:
                            variable.set(('%.1f ms  ·  %d 轮有效' % (stat['mean_ms'], stat['valid'])) if stat['valid'] else '暂无有效样本')
                elif kind == 'done':
                    self.status.set(value['status'])
                    self.set_busy(False)
                    if value['error']:
                        messagebox.showerror('执行失败', value['error'], parent=self)
                    if self.close_pending:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def open_out(self):
        path = Path(self.last_out or self.values['out'].get())
        if not path.is_dir():
            messagebox.showinfo('结果目录', '当前还没有结果，请先开始测试。', parent=self)
            return
        try:
            os.startfile(str(path.resolve()))
        except OSError as exc:
            messagebox.showerror('无法打开目录', str(exc), parent=self)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.close_pending = True
            self.stop()
        else:
            self.destroy()


if __name__ == '__main__':
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    StartupApp().mainloop()
