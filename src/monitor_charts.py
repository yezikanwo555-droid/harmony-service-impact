"""Live resource charts: zero-based axes, explicit gaps, no synthetic samples."""
import math
import tkinter as tk
from tkinter import ttk

INK='#14233b'; MUTED='#64748b'; GRID='#e8edf5'
METRICS=(('cpu_pct','CPU','%','#4f46e5'),('pss_mib','PSS 内存','MiB','#087f8c'),('cache_mib','磁盘缓存','MiB','#b45309'))
RANGES={'最近 60 秒':60,'最近 5 分钟':300,'全部数据':None}

def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def fmt(value):
    return ('%.2f'%value).rstrip('0').rstrip('.') if number(value) else '—'

def visible_rows(rows,span):
    valid=[r for r in rows if number(r.get('elapsed_s')) and r['elapsed_s']>=0]
    end=max((r['elapsed_s'] for r in valid),default=0)
    start=max(0,end-span) if span is not None else 0
    return [r for r in valid if start<=r['elapsed_s']<=end],start,max(end,start+1)

def segments(rows,key,start,end,pixels):
    """Min/max per horizontal pixel, preserving extrema and missing-value gaps."""
    runs=[];run=[]
    for row in rows:
        t,v=row.get('elapsed_s'),row.get(key)
        if not number(t) or not number(v) or v<0:
            if run:runs.append(run);run=[]
            continue
        if run and t<run[-1][0]:runs.append(run);run=[]
        run.append((t,v))
    if run:runs.append(run)
    result=[]
    for run in runs:
        buckets={}
        for i,p in enumerate(run):
            bucket=int((p[0]-start)/max(1,end-start)*max(1,pixels))
            buckets.setdefault(bucket,[]).append((i,p))
        reduced=[]
        for group in buckets.values():
            choices=(group[0],min(group,key=lambda x:x[1][1]),max(group,key=lambda x:x[1][1]),group[-1])
            reduced.extend(p for _,p in sorted(dict(choices).items()))
        result.append(reduced)
    return result

class MonitorCharts(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent);self.app=app
        self.frozen=None;self.pending=None;self.selected=None;self.visible=[];self.bounds={}
        self.range=tk.StringVar(value='最近 5 分钟');self.view=tk.StringVar(value='双包对比')
        self.context=tk.StringVar(value='等待采集 · 当前组')
        self.detail=tk.StringVar(value='悬停查看读数，或聚焦图表后按 ← / →；缺失读数显示断点。')
        tools=ttk.Frame(self);tools.pack(fill='x',pady=(0,10))
        ttk.Label(tools,textvariable=self.context,foreground=INK).pack(side='left')
        self.pause=ttk.Button(tools,text='暂停查看',command=self.toggle_pause);self.pause.pack(side='right',padx=(8,0))
        select=ttk.Combobox(tools,textvariable=self.range,values=list(RANGES),state='readonly',width=13)
        select.pack(side='right',padx=(8,0));select.bind('<<ComboboxSelected>>',lambda e:self.changed())
        switch=ttk.Combobox(tools,textvariable=self.view,values=['双包对比','曲线','采样明细'],state='readonly',width=10)
        switch.pack(side='right');switch.bind('<<ComboboxSelected>>',lambda e:self.switch_view())
        self.caption=ttk.Label(self,textvariable=self.detail,foreground=MUTED,wraplength=900)
        self.caption.pack(side='bottom',anchor='w',pady=(8,0))
        self.body=ttk.Frame(self);self.body.pack(fill='both',expand=True)
        self.plot_host=ttk.Frame(self.body);self.plot_host.pack(fill='both',expand=True)
        self.viewport=tk.Canvas(self.plot_host,highlightthickness=0,bg='#f4f6fb',yscrollincrement=20)
        scroll=ttk.Scrollbar(self.plot_host,command=self.viewport.yview);scroll.pack(side='right',fill='y')
        self.viewport.pack(side='left',fill='both',expand=True);self.viewport.configure(yscrollcommand=scroll.set)
        self.plots=ttk.Frame(self.viewport);item=self.viewport.create_window((0,0),window=self.plots,anchor='nw')
        self.viewport.bind('<Configure>',lambda e:self.viewport.itemconfigure(item,width=e.width,height=max(450,e.height)))
        self.plots.bind('<Configure>',lambda e:self.viewport.configure(scrollregion=self.viewport.bbox('all')))
        app.bind('<MouseWheel>',self.scroll,add='+')
        self.canvases={}
        for i,(key,title,unit,color) in enumerate(METRICS):
            self.plots.rowconfigure(i,weight=1,uniform='plot');self.plots.columnconfigure(0,weight=1)
            c=tk.Canvas(self.plots,bg='white',height=145,highlightthickness=1,highlightbackground=GRID,
                        highlightcolor=color,takefocus=True,cursor='crosshair')
            c.grid(row=i,column=0,sticky='nsew',pady=(0,8 if i<2 else 0));self.canvases[key]=c
            c.bind('<Configure>',lambda e:self.request_draw())
            c.bind('<Motion>',lambda e,k=key:self.hover(e,k))
            c.bind('<Leave>',lambda e:self.clear_cursor())
            c.bind('<Button-1>',lambda e:e.widget.focus_set())
            c.bind('<Left>',lambda e:self.step(-1));c.bind('<Right>',lambda e:self.step(1))
            c.bind('<Home>',lambda e:self.step('first'));c.bind('<End>',lambda e:self.step('last'))
            c.bind('<Escape>',lambda e:self.clear_cursor())
            c.bind('<FocusIn>',self.reveal_focus)
        self.table_host=ttk.Frame(self.body)
        from desktop_view import table
        app.resource_table=table(self.table_host,[('sec','经过 / 秒',80),('cpu','CPU %',90),('memory','PSS MiB',110),('cache','缓存 MiB',110),('note','采样说明',350)])
        from pair_charts import PairCharts
        self.pair=PairCharts(self.body,app.chart_datasets)
        self.range_control=select;self.switch_view()

    def reset(self,config):
        self.frozen=None;self.selected=None;self.pause.configure(text='暂停查看')
        self.context.set(('改造前' if config['role']=='before' else '改造后')+' · 实时资源')
        self.detail.set('悬停或按 ← / → 查看采样值；系统缓存为测试前后快照。')
        self.request_draw()
        self.pair.reset()

    def source(self):
        if self.frozen is not None:return self.frozen
        return self.app.sample_rows,self.app.cache_snapshots,self.app.page_cache_active

    def toggle_pause(self):
        if self.frozen is None:
            self.frozen=([dict(r) for r in self.app.sample_rows],[dict(s) for s in self.app.cache_snapshots],self.app.page_cache_active)
            self.pause.configure(text='继续实时');self.detail.set('图表已冻结；采集和采样明细仍在继续，点击“继续实时”回到最新数据。')
        else:
            self.frozen=None;self.pause.configure(text='暂停查看');self.detail.set('已返回实时曲线；悬停或按 ← / → 查看采样值。')
        self.selected=None;self.request_draw()

    def changed(self):
        self.selected=None;self.request_draw()

    def switch_view(self):
        self.table_host.pack_forget();self.plot_host.pack_forget();self.pair.pack_forget()
        paired=self.view.get()=='双包对比'
        target=self.pair if paired else self.plot_host if self.view.get()=='曲线' else self.table_host
        target.pack(fill='both',expand=True)
        if paired:self.pause.pack_forget();self.range_control.pack_forget();self.caption.pack_forget()
        else:
            self.pause.pack(side='right',padx=(8,0));self.range_control.pack(side='right',padx=(8,0));self.caption.pack(side='bottom',anchor='w',pady=(8,0))
        self.request_draw()

    def scroll(self,event):
        if str(event.widget).startswith(str(self.plots)):
            self.viewport.yview_scroll(-int(event.delta/120),'units')

    def reveal_focus(self,event):
        top=self.viewport.canvasy(0);bottom=top+self.viewport.winfo_height()
        y=event.widget.winfo_y();height=event.widget.winfo_height();total=max(1,self.plots.winfo_height())
        if y<top:self.viewport.yview_moveto(y/total)
        elif y+height>bottom:self.viewport.yview_moveto((y+height-self.viewport.winfo_height())/total)

    def request_draw(self):
        if self.pending is None:self.pending=self.after(100,self.draw)

    def draw(self):
        self.pending=None
        rows,snapshots,page_mode=self.source()
        self.visible,start,end=visible_rows(rows,RANGES[self.range.get()])
        for key,title,unit,color in METRICS:
            c=self.canvases[key];c.delete('all');w=max(260,c.winfo_width());h=max(80,c.winfo_height())
            c.create_text(16,17,text=title+' / '+unit,anchor='w',fill=INK,font=('Microsoft YaHei UI',10,'bold'))
            if key=='cache_mib' and page_mode:self.draw_snapshots(c,snapshots,w,h);self.bounds.pop(key,None);continue
            values=[r[key] for r in self.visible if number(r.get(key)) and r[key]>=0]
            top=100 if key=='cpu_pct' else max(1,max(values,default=0)*1.15)
            left,right,bottom,upper=65,w-22,h-25,39
            if bottom<=upper:bottom=upper+10
            self.bounds[key]=(left,right,upper,bottom,start,end,top)
            for frac in (0,.5,1):
                y=bottom-(bottom-upper)*frac
                c.create_line(left,y,right,y,fill=GRID)
                c.create_text(left-10,y,text=fmt(top*frac),anchor='e',fill=MUTED,font=('Segoe UI',9))
            for frac in (0,.5,1):
                x=left+(right-left)*frac
                c.create_text(x,bottom+14,text=fmt(start+(end-start)*frac)+'s',fill=MUTED,font=('Segoe UI',9))
            if values:
                status='峰值 '+fmt(max(values))+' '+unit+'  ·  '+str(len(values))+' 个有效点'
            else:status='等待采样' if not rows else '暂无有效读数'
            c.create_text(right,17,text=status,anchor='e',fill=MUTED,font=('Microsoft YaHei UI',9))
            for run in segments(self.visible,key,start,end,int(right-left)):
                points=[(left+(t-start)/(end-start)*(right-left),bottom-v/top*(bottom-upper)) for t,v in run]
                if len(points)>1:c.create_line(*[v for p in points for v in p],fill=color,width=2,tags='series')
                if len(points)<=40:
                    for x,y in points:c.create_oval(x-2,y-2,x+2,y+2,fill=color,outline='',tags='point')
            if not values:c.create_text((left+right)/2,(upper+bottom)/2,text='采集开始后自动绘制' if not rows else '读数不可用，查看采样明细中的原因',fill=MUTED,font=('Microsoft YaHei UI',10))
        if self.selected is not None and self.visible:
            self.selected=min(self.selected,len(self.visible)-1);self.draw_cursor()

    def draw_snapshots(self,c,snapshots,w,h):
        latest={s.get('stage'):s for s in snapshots}
        c.create_text(w-20,17,text='系统页面快照 · 非连续曲线',anchor='e',fill=MUTED,font=('Microsoft YaHei UI',9))
        for x,stage,label in ((w*.23,'start','测试前'),(w*.58,'end','测试后')):
            s=latest.get(stage,{})
            value=s.get('raw_text','—') if s.get('status')=='ok' else ('读取失败' if s else '等待读取')
            c.create_text(x,52,text=label,fill=MUTED,font=('Microsoft YaHei UI',9))
            c.create_text(x,78,text=value,fill=INK,font=('Microsoft YaHei UI',13,'bold'))
        a,b=latest.get('start',{}),latest.get('end',{})
        growth=fmt(b['mib']-a['mib'])+' MiB' if all(s.get('status')=='ok' and number(s.get('mib')) for s in (a,b)) else '—'
        c.create_text(w*.85,52,text='增长量',fill=MUTED,font=('Microsoft YaHei UI',9))
        c.create_text(w*.85,78,text=growth,fill='#b45309',font=('Microsoft YaHei UI',13,'bold'))
        if h>=125:c.create_text(16,h-16,text='按系统显示值计算；任一端缺失时不计算增长量。',anchor='w',fill=MUTED,font=('Microsoft YaHei UI',9))

    def hover(self,event,key):
        if key not in self.bounds or not self.visible:return
        left,right,_,_,start,end,_=self.bounds[key]
        if event.x<left or event.x>right:return self.clear_cursor()
        t=start+(event.x-left)/(right-left)*(end-start)
        self.selected=min(range(len(self.visible)),key=lambda i:abs(self.visible[i]['elapsed_s']-t));self.draw_cursor()

    def step(self,direction):
        if not self.visible:return 'break'
        index=len(self.visible)-1 if self.selected is None else self.selected
        self.selected=0 if direction=='first' else len(self.visible)-1 if direction=='last' else max(0,min(len(self.visible)-1,index+direction))
        self.draw_cursor();return 'break'

    def draw_cursor(self):
        row=self.visible[self.selected];parts=[('已暂停 · ' if self.frozen is not None else '')+'时间 '+fmt(row['elapsed_s'])+' 秒']
        for key,title,unit,color in METRICS:
            c=self.canvases[key];c.delete('cursor')
            if key not in self.bounds:continue
            left,right,upper,bottom,start,end,top=self.bounds[key]
            x=left+(row['elapsed_s']-start)/(end-start)*(right-left)
            c.create_line(x,upper,x,bottom,fill=MUTED,dash=(3,3),tags='cursor')
            value=row.get(key);parts.append(title+' '+(fmt(value)+' '+unit if number(value) else '不可用'))
            if number(value) and value>=0:
                y=bottom-value/top*(bottom-upper);c.create_oval(x-4,y-4,x+4,y+4,fill=color,outline='white',width=2,tags='cursor')
        self.detail.set('  ·  '.join(parts))

    def clear_cursor(self):
        self.selected=None
        for c in self.canvases.values():c.delete('cursor')
        self.detail.set('图表已冻结；采集继续。点击“继续实时”返回。' if self.frozen is not None else '悬停或按 ← / → 查看采样值；缺失读数显示断点。')

    def destroy(self):
        if self.pending is not None:self.after_cancel(self.pending)
        super().destroy()
