"""Desktop presentation layer; measurement and device control stay in the engine."""
import tkinter as tk
from tkinter import ttk
import app_meta

BG='#f4f6fb';INK='#14233b';MUTED='#64748b';ACCENT='#4f46e5';NAV='#111c32'

def label(parent,text,style='Card.TLabel',**kwargs):
    return ttk.Label(parent,text=text,style=style,**kwargs)

def card(parent,title,subtitle=None):
    f=ttk.Frame(parent,style='Card.TFrame',padding=20)
    label(f,title,'CardTitle.TLabel').pack(anchor='w')
    if subtitle:label(f,subtitle,'MutedCard.TLabel',wraplength=840).pack(anchor='w',pady=(6,14))
    body=ttk.Frame(f,style='Card.TFrame');body.pack(fill='both',expand=True,pady=(12 if not subtitle else 0,0))
    return f,body

def setup_style(app):
    s=ttk.Style(app);s.theme_use('clam')
    s.configure('.',font=('Microsoft YaHei UI',10),background=BG,foreground=INK)
    s.configure('TFrame',background=BG)
    s.configure('Card.TFrame',background='white',bordercolor='#e4e9f2')
    s.configure('White.TFrame',background='white')
    for name,color,size,weight in [('Card.TLabel',INK,10,'normal'),('CardTitle.TLabel',INK,13,'bold'),('MutedCard.TLabel',MUTED,9,'normal'),('Metric.TLabel',INK,20,'bold')]:
        s.configure(name,background='white',foreground=color,font=('Microsoft YaHei UI',size,weight))
    s.configure('White.TLabel',background='white')
    s.configure('TLabel',background=BG)
    s.configure('TEntry',padding=(10,8),fieldbackground='white',bordercolor='#dce2ed',lightcolor='#dce2ed',darkcolor='#dce2ed')
    s.configure('TCombobox',padding=7,fieldbackground='white',arrowcolor=MUTED,bordercolor='#dce2ed')
    s.map('TCombobox',fieldbackground=[('readonly','white')],foreground=[('readonly',INK)])
    s.configure('TButton',padding=(15,9),background='#e9edf8',foreground='#354766',borderwidth=0,focuscolor=ACCENT)
    s.map('TButton',background=[('active','#dde4f4'),('disabled','#eef1f7')],foreground=[('disabled','#94a3b8')])
    s.configure('Primary.TButton',background=ACCENT,foreground='white',font=('Microsoft YaHei UI',10,'bold'))
    s.map('Primary.TButton',background=[('active','#4338ca'),('disabled','#c2c5e9')],foreground=[('disabled','#ffffff')])
    s.configure('Danger.TButton',background='#fff1f2',foreground='#be123c')
    s.configure('TCheckbutton',background='white')
    s.configure('Workspace.TNotebook',background=BG,borderwidth=0,tabmargins=0)
    s.layout('Workspace.TNotebook.Tab',[])
    s.configure('Treeview',background='white',fieldbackground='white',foreground=INK,rowheight=34,borderwidth=0,font=('Microsoft YaHei UI',10))
    s.configure('Treeview.Heading',background='#edf1f8',foreground='#52627a',padding=(10,11),font=('Microsoft YaHei UI',9,'bold'),relief='flat')
    s.map('Treeview',background=[('selected','#e5e7ff')],foreground=[('selected','#312e81')])
    s.configure('Accent.Horizontal.TProgressbar',background=ACCENT,troughcolor='#e8ecf6',borderwidth=0)

def build(app):
    from impact_app import CACHE_MODES,TEST_MODES
    setup_style(app)
    shell=ttk.Frame(app);shell.pack(fill='both',expand=True)
    sidebar=tk.Frame(shell,bg=NAV,width=192);sidebar.pack(side='left',fill='y');sidebar.pack_propagate(False)
    tk.Label(sidebar,text='◈',bg=NAV,fg='#a5b4fc',font=('Segoe UI Symbol',32)).pack(anchor='w',padx=24,pady=(24,0))
    tk.Label(sidebar,text='元服务\n性能对比',bg=NAV,fg='white',font=('Microsoft YaHei UI',20,'bold'),justify='left').pack(anchor='w',padx=24,pady=(5,10))
    tk.Label(sidebar,text='性能 · 网络 · 缓存',bg=NAV,fg='#8da1c1',font=('Microsoft YaHei UI',9)).pack(anchor='w',padx=24,pady=(0,28))
    nav=tk.Frame(sidebar,bg=NAV);nav.pack(fill='x',padx=12)
    tk.Label(sidebar,text='HarmonyOS / OpenHarmony\nWindows 64 位  ·  v'+app_meta.VERSION,bg=NAV,fg='#7e91b0',font=('Microsoft YaHei UI',8),justify='left').pack(side='bottom',anchor='w',padx=24,pady=22)
    content=ttk.Frame(shell,padding=(26,22,26,16));content.pack(side='left',fill='both',expand=True)
    header=ttk.Frame(content);header.pack(fill='x',pady=(0,18))
    app.page_title=tk.StringVar(value='测试工作台');app.page_caption=tk.StringVar(value='选择两个版本，在相同条件下比较启动与资源表现。')
    titlebox=ttk.Frame(header);titlebox.pack(side='left',fill='x',expand=True)
    ttk.Label(titlebox,textvariable=app.page_title,font=('Microsoft YaHei UI',23,'bold')).pack(anchor='w')
    ttk.Label(titlebox,textvariable=app.page_caption,foreground=MUTED).pack(anchor='w',pady=(5,0))
    b=ttk.Button(header,text='设备与高级设置',command=app.advanced);b.pack(side='right');app.controls.append(b)
    # A stable footer keeps start/stop reachable while the configuration scrolls.
    footer=ttk.Frame(content,padding=(0,14,0,0));footer.pack(side='bottom',fill='x')
    status=ttk.Frame(footer);status.pack(fill='x',pady=(0,10))
    ttk.Label(status,text='●',foreground='#6366f1').pack(side='left',padx=(0,8))
    ttk.Label(status,textvariable=app.state_text,wraplength=790,foreground=MUTED).pack(side='left',fill='x',expand=True)
    action=ttk.Frame(footer);action.pack(fill='x')
    ttk.Button(action,text='打开结果目录',command=app.open_out).pack(side='left')
    app.stop_button=ttk.Button(action,text='停止采集',command=app.stop,style='Danger.TButton',state='disabled');app.stop_button.pack(side='right')
    start=ttk.Button(action,text='开始一键对比  →',command=app.start_batch,style='Primary.TButton');start.pack(side='right',padx=10);app.controls.append(start)
    app.tabs=ttk.Notebook(content,style='Workspace.TNotebook');app.tabs.pack(fill='both',expand=True)
    pages=[ttk.Frame(app.tabs) for _ in range(5)]
    names=['测试工作台','实时监控','版本对比','运行日志','关于与更新']
    captions=['选择两个版本，在相同条件下比较启动与资源表现。','跟踪资源采样与磁盘缓存，随时查看执行状态。','查看差值、变化率和可比性，导出完整报告。','每一步都有记录，帮助定位失败原因。','从 GitHub 获取正式版本，保持工具持续更新。']
    buttons=[]
    for i,(page,name) in enumerate(zip(pages,names)):
        app.tabs.add(page,text=name)
        button=tk.Button(nav,text='%02d   %s'%(i+1,name),command=lambda index=i:app.tabs.select(index),anchor='w',padx=14,pady=12,
                         bg=NAV,fg='#aebcd2',activebackground='#263453',activeforeground='white',relief='flat',borderwidth=0,font=('Microsoft YaHei UI',10))
        button.pack(fill='x',pady=4);buttons.append(button)
    def changed(*args):
        index=app.tabs.index(app.tabs.select());app.page_title.set(names[index]);app.page_caption.set(captions[index])
        for i,b in enumerate(buttons):b.configure(bg='#293452' if i==index else NAV,fg='white' if i==index else '#aebcd2')
    app.tabs.bind('<<NotebookTabChanged>>',changed);changed()
    # Configuration workspace.
    canvas=tk.Canvas(pages[0],bg=BG,highlightthickness=0)
    scroll=ttk.Scrollbar(pages[0],orient='vertical',command=canvas.yview);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
    canvas.configure(yscrollcommand=scroll.set);setup=ttk.Frame(canvas,padding=(0,0,10,0));item=canvas.create_window((0,0),window=setup,anchor='nw')
    setup.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')));canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
    app.setup_canvas=canvas
    def wheel(event):
        if app.tabs.index(app.tabs.select())!=0:return
        # Scroll only this page and leave combobox dropdowns alone.
        if isinstance(event.widget,(ttk.Combobox,tk.Text)):return
        if str(event.widget).startswith(str(setup)):canvas.yview_scroll(-int(event.delta/120),'units')
    app.bind('<MouseWheel>',wheel,add='+')
    versions=ttk.Frame(setup);versions.pack(fill='x',pady=(0,16));versions.columnconfigure((0,1),weight=1,uniform='versions')
    for col,(role,name) in enumerate([('before','改造前'),('after','改造后')]):
        outer,body=card(versions,name,'基线版本' if role=='before' else '待验证版本');outer.grid(row=0,column=col,sticky='nsew',padx=(0,8) if col==0 else (8,0))
        body.columnconfigure(0,weight=1)
        label(body,'应用包名').grid(row=0,column=0,sticky='w',pady=(0,7))
        app.entry(body,app.group[role]['bundle'],1,0,span=2)
        app.button(body,'获取当前应用包名',lambda r=role:app.detect_bundle(r),2,0,span=2)
        label(body,'版本备注','MutedCard.TLabel').grid(row=3,column=0,sticky='w',pady=(12,2))
        app.entry(body,app.group[role]['label'],4,0,span=2)
        label(body,'包文件 · 可选，用于包体比较','MutedCard.TLabel').grid(row=5,column=0,columnspan=2,sticky='w',pady=(8,2))
        app.entry(body,app.group[role]['artifact'],6,0);app.button(body,'选择',lambda r=role:app.choose_artifact(r),6,1)
        controls=ttk.Frame(body,style='Card.TFrame');controls.grid(row=7,column=0,columnspan=2,sticky='ew',pady=(12,0))
        for text,callback in [('单独采集',lambda r=role:app.start(r)),('导入结果',lambda r=role:app.import_result(r)),('更多设置',lambda r=role:app.group_advanced(r))]:
            b=ttk.Button(controls,text=text,command=callback);b.pack(side='left',padx=(0,6));app.controls.append(b)
        ttk.Label(body,textvariable=app.group[role]['result'],style='MutedCard.TLabel',wraplength=360).grid(row=8,column=0,columnspan=2,sticky='w',pady=(12,0))
    frame,body=card(setup,'测试与网络','两组使用同一组测试参数，按顺序执行。');frame.pack(fill='x',pady=(0,16))
    for col in range(3):body.columnconfigure(col,weight=1,uniform='parameters')
    for col,(text,key) in enumerate([('测试方式','test_mode'),('每种启动次数','count'),('监控时长 / 秒','duration')]):
        label(body,text,'MutedCard.TLabel').grid(row=0,column=col,sticky='w')
        if key=='test_mode':
            c=ttk.Combobox(body,textvariable=app.v[key],values=list(TEST_MODES),state='readonly');c.grid(row=1,column=col,sticky='ew',padx=(0,10),pady=8);app.controls.append(c)
        else:app.entry(body,app.v[key],1,col,width=10)
    monkey=ttk.Frame(body,style='Card.TFrame');monkey.grid(row=2,column=0,columnspan=3,sticky='ew')
    for col,(text,key) in enumerate([('Monkey 时长 / 秒','monkey_seconds'),('最多事件','monkey_events'),('事件间隔 / ms','monkey_interval_ms'),('随机种子','monkey_seed')]):
        monkey.columnconfigure(col,weight=1);label(monkey,text,'MutedCard.TLabel').grid(row=0,column=col,sticky='w');app.entry(monkey,app.v[key],1,col,width=8)
    app.monkey_in_app_check=ttk.Checkbutton(monkey,text='应用内模式：关闭随机返回，操作后检查；跳出自动拉回，失败停止',variable=app.v['monkey_in_app'],onvalue='1',offvalue='0')
    app.monkey_in_app_check.grid(row=2,column=0,columnspan=4,sticky='w',pady=(2,6));app.controls.append(app.monkey_in_app_check)
    label(monkey,'自动保存系统及故障日志，报告标记崩溃 / 无响应。日志仅存本机。','MutedCard.TLabel').grid(row=3,column=0,columnspan=4,sticky='w',pady=(0,4))
    label(monkey,'事件次数或时长先到即停。随机操作可能触发业务，请使用测试账号。','MutedCard.TLabel').grid(row=4,column=0,columnspan=4,sticky='w',pady=(0,10))
    def mode_changed(*a):
        if 'Monkey' in app.v['test_mode'].get():monkey.grid()
        else:monkey.grid_remove()
    app.v['test_mode'].trace_add('write',mode_changed);mode_changed()
    label(body,'网络环境','MutedCard.TLabel').grid(row=3,column=0,sticky='w',pady=(10,0))
    app.network_badge=tk.StringVar()
    def network_changed(*a):app.network_badge.set(app.v['network_mode'].get()+' · '+app.v['network_reqable_preset'].get() if '内置' in app.v['network_mode'].get() else app.v['network_mode'].get())
    for key in ('network_mode','network_reqable_preset'):app.v[key].trace_add('write',network_changed)
    network_changed();ttk.Label(body,textvariable=app.network_badge,style='Card.TLabel',wraplength=550).grid(row=4,column=0,columnspan=2,sticky='w',pady=8)
    app.button(body,'配置网络环境',app.network_dialog,4,2)
    frame,body=card(setup,'采集与保存');frame.pack(fill='x',pady=(0,8));body.columnconfigure(1,weight=1)
    for row,text,key in [(0,'操作场景','scenario'),(1,'采样间隔 / 秒','interval'),(3,'保存目录','out')]:
        label(body,text,'MutedCard.TLabel').grid(row=row,column=0,sticky='w',padx=(0,20));app.entry(body,app.v[key],row,1)
    label(body,'缓存方式','MutedCard.TLabel').grid(row=2,column=0,sticky='w');c=ttk.Combobox(body,textvariable=app.v['cache_mode'],values=list(CACHE_MODES),state='readonly');c.grid(row=2,column=1,sticky='ew',padx=(0,8),pady=8);app.controls.append(c)
    app.button(body,'选择目录',app.choose_out,3,2)
    # Monitoring workspace.
    monitor=pages[1];metrics=ttk.Frame(monitor);metrics.pack(fill='x',pady=(0,16))
    for col,(text,var) in enumerate([('应用 CPU · 整机容量',app.cpu),('应用 PSS 内存',app.memory),('应用磁盘缓存',app.cache)]):
        metrics.columnconfigure(col,weight=1,uniform='metric');f=ttk.Frame(metrics,style='Card.TFrame',padding=(18,10));f.grid(row=0,column=col,sticky='ew',padx=(0,10))
        label(f,text,'MutedCard.TLabel').pack(anchor='w');ttk.Label(f,textvariable=var,style='Metric.TLabel').pack(anchor='w',pady=(10,0))
    from monitor_charts import MonitorCharts
    ttk.Label(monitor,textvariable=app.traffic_text,foreground=INK,wraplength=900).pack(fill='x',pady=(0,8))
    app.monitor_charts=MonitorCharts(monitor,app);app.monitor_charts.pack(fill='both',expand=True)
    # Comparison workspace.
    compare=pages[2];action=ttk.Frame(compare);action.pack(fill='x',pady=(0,12))
    ttk.Label(action,text='关注阈值 %').pack(side='left');entry=ttk.Entry(action,textvariable=app.v['threshold'],width=7);entry.pack(side='left',padx=10);app.controls.append(entry)
    for text,callback,style in [('刷新对比',app.refresh_compare,'TButton'),('生成对比报告',app.make_report,'Primary.TButton')]:
        b=ttk.Button(action,text=text,command=callback,style=style);b.pack(side='right',padx=4);app.controls.append(b)
    ttk.Label(compare,textvariable=app.summary_text,wraplength=850,foreground=MUTED).pack(anchor='w',pady=(0,14))
    compare_views=ttk.Notebook(compare);compare_views.pack(fill='both',expand=True)
    plots=ttk.Frame(compare_views,padding=8);numbers=ttk.Frame(compare_views,padding=8)
    compare_views.add(plots,text='双包曲线');compare_views.add(numbers,text='指标明细')
    from pair_charts import PairCharts
    app.pair_charts=PairCharts(plots,app.chart_datasets);app.pair_charts.pack(fill='both',expand=True)
    app.compare_table=table(numbers,[('metric','指标',200),('before','改造前',100),('after','改造后',100),('delta','差值',90),('pct','变化率 %',100),('status','提示',220)])
    # Logs and release workspace.
    logs=pages[3];app.log=tk.Text(logs,state='disabled',wrap='word',font=('Consolas',10),bg='#172238',fg='#cbd5e1',relief='flat',padx=20,pady=18)
    bar=ttk.Scrollbar(logs,command=app.log.yview);bar.pack(side='right',fill='y');app.log.pack(side='left',fill='both',expand=True);app.log.configure(yscrollcommand=bar.set)
    app.updates_page(pages[4])

def table(parent,columns):
    host=ttk.Frame(parent);host.pack(fill='both',expand=True)
    t=ttk.Treeview(host,columns=tuple(c[0] for c in columns),show='headings',height=8)
    for key,title,width in columns:t.heading(key,text=title);t.column(key,width=width,minwidth=55,stretch=True)
    scroll=ttk.Scrollbar(host,command=t.yview);scroll.pack(side='right',fill='y');t.pack(side='left',fill='both',expand=True);t.configure(yscrollcommand=scroll.set)
    return t
