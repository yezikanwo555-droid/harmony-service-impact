import os
import sys
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import ttk,messagebox
import webbrowser
import app_meta
import update_service
import update_installer

class UpdateMixin:
    def init_updates(self,saved):
        self.update_repo=tk.StringVar(value=app_meta.GITHUB_REPO or saved.get('update_repository',''))
        self.update_auto=tk.BooleanVar(value=saved.get('update_auto',True))
        self.update_token=tk.StringVar()
        self.update_status=tk.StringVar(value='检查 GitHub 上的正式版本，下载后会核对 SHA256。')
        self.update_progress=tk.DoubleVar(value=0)
        self.update_thread=None;self.update_cancel=threading.Event();self.update_release=None;self.update_file=None
        self.installing=False;self.update_busy=False
        self.update_repo.trace_add('write',lambda *a:self.clear_release())
    def clear_release(self):
        self.update_release=None
        if hasattr(self,'update_download_button'):self.update_download_button.configure(state='disabled')
    def updates_page(self,parent):
        box=ttk.Frame(parent,style='Card.TFrame',padding=24);box.pack(fill='x',pady=(0,16))
        ttk.Label(box,text='让工具保持最新',style='CardTitle.TLabel').pack(anchor='w')
        ttk.Label(box,text='当前版本  v'+app_meta.VERSION+'  ·  Windows x64',style='MutedCard.TLabel').pack(anchor='w',pady=(8,22))
        row=ttk.Frame(box,style='Card.TFrame');row.pack(fill='x')
        ttk.Label(row,text='GitHub 仓库',style='Card.TLabel').pack(side='left',padx=(0,18))
        ttk.Entry(row,textvariable=self.update_repo).pack(side='left',fill='x',expand=True)
        self.update_check_button=ttk.Button(row,text='检查更新',style='Primary.TButton',command=self.check_updates)
        self.update_check_button.pack(side='left',padx=(12,0))
        ttk.Checkbutton(box,text='启动时自动检查正式版本',variable=self.update_auto,command=self.save_update_preferences).pack(anchor='w',pady=16)
        advanced=ttk.Frame(box,style='Card.TFrame');advanced.pack(fill='x')
        ttk.Label(advanced,text='私有仓库令牌（可选）',style='MutedCard.TLabel').pack(side='left',padx=(0,12))
        ttk.Entry(advanced,textvariable=self.update_token,show='●',width=26).pack(side='left')
        ttk.Label(advanced,text='仅本次运行使用，不写入报告或设置',style='MutedCard.TLabel').pack(side='left',padx=12)
        notes=ttk.Frame(parent,style='Card.TFrame',padding=24);notes.pack(fill='both',expand=True)
        ttk.Label(notes,text='版本与更新说明',style='CardTitle.TLabel').pack(anchor='w')
        ttk.Label(notes,textvariable=self.update_status,style='MutedCard.TLabel',wraplength=800).pack(anchor='w',pady=12)
        bottom=ttk.Frame(notes,style='Card.TFrame');bottom.pack(side='bottom',fill='x')
        ttk.Progressbar(bottom,variable=self.update_progress,maximum=100,style='Accent.Horizontal.TProgressbar').pack(fill='x',pady=(12,10))
        actions=ttk.Frame(bottom,style='Card.TFrame');actions.pack(fill='x')
        self.update_download_button=ttk.Button(actions,text='一键更新并重启',command=self.download_update,style='Primary.TButton',state='disabled');self.update_download_button.pack(side='left')
        ttk.Button(actions,text='下载目录',command=self.open_updates).pack(side='left',padx=8)
        ttk.Button(actions,text='取消下载',command=lambda:self.update_cancel.set()).pack(side='left')
        ttk.Button(actions,text='发布页',command=self.open_releases).pack(side='right')
        ttk.Label(bottom,text='点击后自动下载、校验、替换程序并重启，无需手动解压。设置和结果保留。',style='MutedCard.TLabel',wraplength=700).pack(anchor='w',pady=(10,0))
        text_area=ttk.Frame(notes,style='Card.TFrame');text_area.pack(fill='both',expand=True)
        self.update_notes=tk.Text(text_area,height=1,wrap='word',bg='#ffffff',fg='#475569',relief='flat',font=('Microsoft YaHei UI',10),state='disabled')
        bar=ttk.Scrollbar(text_area,command=self.update_notes.yview);bar.pack(side='right',fill='y')
        self.update_notes.configure(yscrollcommand=bar.set);self.update_notes.pack(side='left',fill='both',expand=True)
    def save_update_preferences(self):
        try:
            import impact_engine
            impact_engine.save_json(self.settings_path,self.settings())
        except OSError as exc:self.update_status.set('设置保存失败：'+str(exc))
    def check_updates(self,quiet=False):
        if self.update_thread and self.update_thread.is_alive():return
        try:repo=update_service.repository(self.update_repo.get())
        except ValueError as exc:
            if not quiet:self.update_status.set(str(exc))
            return
        self.clear_release();self.update_progress.set(0);self.save_update_preferences()
        self.update_status.set('正在检查 GitHub 最新正式版本…');self.update_check_button.configure(state='disabled')
        token=self.update_token.get().strip()
        def run():
            try:self.events.put(('update_done',dict(action='check',repo=repo,result=update_service.check(repo,token=token),error='')))
            except Exception as exc:self.events.put(('update_done',dict(action='check',repo=repo,result=None,error=str(exc))))
        self.update_thread=threading.Thread(target=run,daemon=True);self.update_thread.start()
    def download_update(self):
        if not self.update_release or self.update_thread and self.update_thread.is_alive():return
        if self.worker and self.worker.is_alive():self.update_status.set('请等待当前采集与网络恢复完成，再下载更新。');return
        if not getattr(sys,'frozen',False):self.update_status.set('源码运行不替换 Python，请使用 Windows EXE 发行版的一键更新。');return
        import network_session
        journal=network_session.journal_path()
        try:
            if journal.exists():
                import json
                if json.loads(journal.read_text(encoding='utf8')).get('state')!='restored':
                    self.update_status.set('请先在网络设置中恢复上次环境，再更新重启。');return
        except (OSError,ValueError):self.update_status.set('无法确认上次网络环境已恢复，请先检查恢复记录。');return
        release=dict(self.update_release);self.update_cancel=threading.Event();self.update_file=None
        self.update_busy=True
        for w in self.controls:w.configure(state='disabled')
        self.update_download_button.configure(state='disabled');self.update_check_button.configure(state='disabled')
        self.update_status.set('正在下载 v'+release['version']+'…');token=self.update_token.get().strip()
        directory=self.settings_path.parent/'updates';cancel=self.update_cancel
        def run():
            last=[0.]
            def progress(done,total):
                now=time.monotonic()
                if now-last[0]>.15 or done==total:self.events.put(('update_progress',done/total*100));last[0]=now
            try:
                file=update_service.download(release,directory,cancel,progress,token)
                if cancel.is_set():raise InterruptedError('已取消更新')
                plan=update_installer.prepare(file,release,sys.executable,directory)
                if cancel.is_set():raise InterruptedError('已取消更新')
                self.events.put(('update_done',dict(action='install',repo=release['repo'],result=str(plan),error='')))
            except Exception as exc:self.events.put(('update_done',dict(action='download',repo=release['repo'],result=None,error=str(exc))))
        self.update_thread=threading.Thread(target=run,daemon=True);self.update_thread.start()
    def update_complete(self,value):
        self.update_check_button.configure(state='normal')
        if value['action'] in ('download','install'):
            self.update_busy=False
            for w in self.controls:w.configure(state='readonly' if isinstance(w,ttk.Combobox) else 'normal')
        if value.get('error'):
            self.update_status.set(value['error']);self.update_progress.set(0)
            if self.update_release and self.update_release.get('newer'):self.update_download_button.configure(state='normal')
            return
        if value['action']=='install':
            if self.update_cancel.is_set():self.update_status.set('已取消更新，旧程序保留。');return
            if self.worker and self.worker.is_alive():self.update_status.set('当前任务尚未结束，更新未执行。');return
            try:
                import impact_engine
                impact_engine.save_json(self.settings_path,self.settings())
                update_installer.launch_helper(value['result'])
            except (OSError,ValueError) as exc:
                self.update_status.set('更新启动失败：'+str(exc));self.update_download_button.configure(state='normal');return
            self.installing=True;self.destroy();return
        if value['action']=='download':
            self.update_file=value['result'];self.update_progress.set(100)
            self.update_status.set('下载完成，SHA256 校验通过。');return
        try:current_repo=update_service.repository(self.update_repo.get())
        except ValueError:return
        if value['repo']!=current_repo:self.update_status.set('仓库地址已变化，请重新检查更新。');return
        result=value['result'];self.update_release=result
        self.update_notes.configure(state='normal');self.update_notes.delete('1.0','end');self.update_notes.insert('end',result['notes']);self.update_notes.configure(state='disabled')
        self.update_status.set('发现新版本 v'+result['version'] if result['newer'] else '当前 v'+app_meta.VERSION+' 已是最新可用版本。')
        self.update_download_button.configure(state='normal' if result['newer'] else 'disabled')
        if result['newer']:self.append_log('发现新版 v'+result['version']+'，可在“关于与更新”中下载。')
    def open_updates(self):
        folder=self.settings_path.parent/'updates';folder.mkdir(parents=True,exist_ok=True)
        try:os.startfile(str(folder))
        except OSError as exc:messagebox.showerror('打开下载目录',str(exc),parent=self)
    def open_releases(self):
        try:repo=update_service.repository(self.update_repo.get())
        except ValueError as exc:self.update_status.set(str(exc));return
        webbrowser.open('https://github.com/'+repo+'/releases')
