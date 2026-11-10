"""Shared before/after chart model for the desktop and self-contained reports."""
import copy
import html
import tkinter as tk
from tkinter import ttk
from monitor_charts import number,fmt,segments,METRICS,RANGES,INK,MUTED,GRID

SERIES=(('before','改造前','#2563eb',()),('after','改造后','#c45a0a',(6,3)))
TRAFFIC=(('upload_kib_s','上传速率','KiB/s',''),('download_kib_s','下载速率','KiB/s',''),('traffic_total_mib','累计流量消耗','MiB',''))
ALL_METRICS=METRICS+TRAFFIC

def cache_is_snapshot(data):
    return data.get('cache',{}).get('source')=='system_storage_page'

def rows_for(data,key):
    return [] if key=='cache_mib' and cache_is_snapshot(data) else data.get('resources',[])

def model(datasets,key,span=None):
    rows={role:rows_for(datasets.get(role,{}),key) for role,_,_,_ in SERIES}
    end=max([1]+[r['elapsed_s'] for group in rows.values() for r in group if number(r.get('elapsed_s')) and r['elapsed_s']>=0])
    start=max(0,end-span) if span else 0
    visible={role:[r for r in group if number(r.get('elapsed_s')) and start<=r['elapsed_s']<=end] for role,group in rows.items()}
    values=[r[key] for group in visible.values() for r in group if number(r.get(key)) and r[key]>=0]
    top=100 if key=='cpu_pct' else max(1,max(values,default=0)*1.15)
    return visible,start,end,top

def snapshot_text(data):
    if not cache_is_snapshot(data):return '目录采样曲线' if data else '等待本组数据'
    points={p.get('stage'):p for p in data.get('cache',{}).get('checkpoints',[])}
    def val(stage):
        p=points.get(stage,{})
        return p.get('raw_text',fmt(p.get('mib'))+' MiB') if p.get('status')=='ok' else ('不可用' if p else '待采集')
    a,b=points.get('start',{}),points.get('end',{})
    growth=fmt(b['mib']-a['mib'])+' MiB' if all(p.get('status')=='ok' and number(p.get('mib')) for p in (a,b)) else '—'
    return '前 '+val('start')+' → 后 '+val('end')+'；增长 '+growth

def svg_chart(before,after,key,title):
    datasets={'before':before,'after':after};rows,start,end,top=model(datasets,key)
    esc=lambda v:html.escape(str(v),quote=True)
    out='<svg viewBox="0 0 760 240" role="img" aria-label="'+esc(title)+'：改造前后同轴对比"><title>'+esc(title)+'</title>'
    for role,label,color,dash in SERIES:
        x=55 if role=='before' else 380
        out+=f'<line x1="{x}" y1="16" x2="{x+28}" y2="16" stroke="{color}" stroke-width="3"'+(' stroke-dasharray="6 3"' if dash else '')+'/>'
        out+=f'<text x="{x+36}" y="20" font-size="12">{label}<title>{esc(datasets[role].get("environment",{}).get("bundle","未提供包名"))}</title></text>'
    snapshot=key=='cache_mib' and any(cache_is_snapshot(d) for d in datasets.values())
    upper=80 if snapshot else 45;bottom=207
    if snapshot:
        for i,(role,label,color,_) in enumerate(SERIES):
            out+=f'<text x="55" y="{42+i*19}" fill="{color}" font-size="12">{label}：{esc(snapshot_text(datasets[role]))}</text>'
    for frac in (0,.5,1):
        y=bottom-frac*(bottom-upper)
        out+=f'<line x1="60" y1="{y}" x2="735" y2="{y}" stroke="{GRID}"/><text x="52" y="{y+4}" text-anchor="end" font-size="11">{fmt(top*frac)}</text>'
    out+=f'<text x="60" y="231" font-size="11">0 秒</text><text x="735" y="231" text-anchor="end" font-size="11">{fmt(end)} 秒</text>'
    any_points=False
    for role,label,color,dash in SERIES:
        for run in segments(rows[role],key,start,end,675):
            any_points=True
            points=[(60+t/end*675,bottom-v/top*(bottom-upper),t,v) for t,v in run]
            if len(points)>1:
                out+='<polyline fill="none" stroke="'+color+'" stroke-width="2"'+(' stroke-dasharray="6 3"' if dash else '')+' points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y,_,_ in points)+'"/>'
            for x,y,t,v in points:
                out+=f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="{color}"><title>{label} · {fmt(t)} 秒：{fmt(v)}</title></circle>'
    if not any_points:
        message='系统存储页快照不绘制连续曲线' if snapshot else '暂无有效资源采样数据'
        out+=f'<text x="395" y="150" text-anchor="middle" fill="{MUTED}" font-size="13">{message}</text>'
    out+='</svg><p class="legend">横轴按各组资源监控开始后的秒数对齐，非同一时刻并行采集。缺失值断开；较短序列结束后不补线。</p>'
    out+='<details><summary>展开原始采样值与原因</summary><table><tr><th>组别</th><th>经过 / 秒</th><th>数值</th><th>说明</th></tr>'
    note={'cpu_pct':'cpu_note','pss_mib':'memory_note','cache_mib':'cache_note'}.get(key,'traffic_note')
    for role,label,_,_ in SERIES:
        for r in rows[role]:out+='<tr><td>'+label+'</td><td>'+fmt(r.get('elapsed_s'))+'</td><td>'+fmt(r.get(key))+'</td><td>'+esc(r.get(note,''))+'</td></tr>'
    return out+'</table></details>'

class PairCharts(ttk.Frame):
    def __init__(self,parent,get_data):
        super().__init__(parent);self.get_data=get_data;self.pending=None;self.frozen=None;self.bounds={};self.visible={};self.selected_time=None
        self.range=tk.StringVar(value='全部数据');self.info=tk.StringVar(value='两组按各自监控经过时间对齐；悬停或 ← / → 查看，缺失值断开。')
        self.category=tk.StringVar(value='资源占用')
        bar=ttk.Frame(self);bar.pack(fill='x',pady=(0,6))
        ttk.Label(bar,text='━━ 改造前',foreground=SERIES[0][2]).pack(side='left')
        ttk.Label(bar,text='┄┄ 改造后',foreground=SERIES[1][2]).pack(side='left',padx=16)
        self.pause=ttk.Button(bar,text='暂停查看',command=self.toggle_pause);self.pause.pack(side='right')
        choice=ttk.Combobox(bar,textvariable=self.range,values=list(RANGES),state='readonly',width=13);choice.pack(side='right',padx=8)
        choice.bind('<<ComboboxSelected>>',lambda e:self.request_draw())
        group=ttk.Combobox(bar,textvariable=self.category,values=['资源占用','网络流量'],state='readonly',width=10);group.pack(side='right',padx=4)
        group.bind('<<ComboboxSelected>>',lambda e:self.change_category())
        footer=ttk.Frame(self,height=44);footer.pack(side='bottom',fill='x');footer.pack_propagate(False)
        self.info_label=ttk.Label(footer,textvariable=self.info,foreground=MUTED,wraplength=750);self.info_label.pack(fill='x',pady=(6,0))
        footer.bind('<Configure>',lambda e:self.info_label.configure(wraplength=max(150,e.width-8)))
        host=ttk.Frame(self);host.pack(fill='both',expand=True)
        self.viewport=tk.Canvas(host,bg='#f4f6fb',highlightthickness=0,yscrollincrement=20)
        scroll=ttk.Scrollbar(host,command=self.viewport.yview);scroll.pack(side='right',fill='y');self.viewport.pack(side='left',fill='both',expand=True)
        self.viewport.configure(yscrollcommand=scroll.set);plots=ttk.Frame(self.viewport)
        item=self.viewport.create_window((0,0),window=plots,anchor='nw')
        self.viewport.bind('<Configure>',lambda e:self.viewport.itemconfigure(item,width=e.width,height=max(510,e.height)))
        plots.bind('<Configure>',lambda e:self.viewport.configure(scrollregion=self.viewport.bbox('all')))
        self.canvases={}
        for i,(key,title,unit,_) in enumerate(ALL_METRICS):
            i=i%3
            plots.rowconfigure(i,weight=1,uniform='chart');plots.columnconfigure(0,weight=1)
            c=tk.Canvas(plots,bg='white',height=165,takefocus=True,highlightthickness=1,highlightbackground=GRID,highlightcolor='#2563eb')
            c.grid(row=i,column=0,sticky='nsew',pady=(0,6));self.canvases[key]=c
            c.bind('<Configure>',lambda e:self.request_draw());c.bind('<Motion>',lambda e,k=key:self.inspect(e.x,k))
            c.bind('<Button-1>',lambda e:e.widget.focus_set());c.bind('<Left>',lambda e:self.step(-1));c.bind('<Right>',lambda e:self.step(1))
            c.bind('<MouseWheel>',lambda e:self.viewport.yview_scroll(-int(e.delta/120),'units'))
            c.bind('<FocusIn>',lambda e:self.reveal(e.widget,plots))
            c.bind('<Leave>',lambda e:self.clear_cursor());c.bind('<Escape>',lambda e:self.clear_cursor())
        self.change_category()
    def change_category(self):
        keys={k for k,_,_,_ in (TRAFFIC if self.category.get()=='网络流量' else METRICS)}
        for key,c in self.canvases.items():
            if key in keys:c.grid()
            else:c.grid_remove()
        self.viewport.yview_moveto(0);self.request_draw()
    def reveal(self,c,plots):
        y=c.winfo_y();height=c.winfo_height();top=self.viewport.canvasy(0);available=self.viewport.winfo_height()
        if y<top:self.viewport.yview_moveto(y/max(1,plots.winfo_height()))
        elif y+height>top+available:self.viewport.yview_moveto((y+height-available)/max(1,plots.winfo_height()))
    def reset(self):
        self.frozen=None;self.selected_time=None;self.pause.configure(text='暂停查看');self.request_draw()
    def toggle_pause(self):
        self.frozen=copy.deepcopy(self.get_data()) if self.frozen is None else None
        self.pause.configure(text='继续实时' if self.frozen is not None else '暂停查看')
        self.info.set('图表已冻结，采集继续。' if self.frozen is not None else '已回到最新数据。');self.request_draw()
    def request_draw(self):
        if self.pending is None:self.pending=self.after(100,self.draw)
    def draw(self):
        self.pending=None;data=self.frozen if self.frozen is not None else self.get_data();self.data=data
        for key,title,unit,_ in ALL_METRICS:
            c=self.canvases[key];c.delete('all');w=max(280,c.winfo_width());h=max(165,c.winfo_height())
            c.create_text(14,17,text=title+' / '+unit,anchor='w',fill=INK,font=('Microsoft YaHei UI',10,'bold'))
            rows,start,end,top=model(data,key,RANGES[self.range.get()]);self.visible[key]=rows
            snapshots=key=='cache_mib' and any(cache_is_snapshot(d) for d in data.values())
            upper=85 if snapshots else 40;bottom=h-25;left=60;right=w-20
            self.bounds[key]=(left,right,upper,bottom,start,end,top)
            if snapshots:
                for i,(role,label,color,_) in enumerate(SERIES):
                    c.create_text(14,39+i*21,text=label+'：'+snapshot_text(data.get(role,{})),anchor='w',fill=color,font=('Microsoft YaHei UI',9))
            for frac in (0,.5,1):
                y=bottom-frac*(bottom-upper);c.create_line(left,y,right,y,fill=GRID)
                c.create_text(left-8,y,text=fmt(top*frac),anchor='e',fill=MUTED,font=('Segoe UI',9))
                c.create_text(left+frac*(right-left),bottom+14,text=fmt(start+frac*(end-start))+'s',fill=MUTED,font=('Segoe UI',9))
            found=False
            for role,label,color,dash in SERIES:
                for run in segments(rows[role],key,start,end,int(right-left)):
                    found=True;points=[(left+(t-start)/(end-start)*(right-left),bottom-v/top*(bottom-upper)) for t,v in run]
                    if len(points)>1:c.create_line(*[v for p in points for v in p],fill=color,width=2,dash=dash,tags=('series',role))
                    if len(points)<=50:
                        for x,y in points:c.create_oval(x-2,y-2,x+2,y+2,fill=color,outline='',tags=('point',role))
            status='快照不连线' if snapshots and not found else '等待另一组数据' if any(not rows[r] for r,_,_,_ in SERIES) else '同轴对比'
            c.create_text(right,17,text=status,anchor='e',fill=MUTED,font=('Microsoft YaHei UI',9))
            if not found:c.create_text((left+right)/2,(upper+bottom)/2,text='测试前后快照 · 非连续曲线' if snapshots else '采集或导入两组结果后显示',fill=MUTED,font=('Microsoft YaHei UI',9))
    def clear_cursor(self):
        for c in self.canvases.values():c.delete('cursor')
    def inspect(self,x,key):
        if key not in self.bounds:return
        left,right,upper,bottom,start,end,top=self.bounds[key]
        if not left<=x<=right:return
        return self.at_time(start+(x-left)/(right-left)*(end-start),key)
    def step(self,delta):
        key=next((k for k,c in self.canvases.items() if c==self.focus_get()),'cpu_pct')
        times=sorted({r['elapsed_s'] for rows in self.visible.get(key,{}).values() for r in rows})
        if times:
            index=min(range(len(times)),key=lambda i:abs(times[i]-(self.selected_time if self.selected_time is not None else times[0])))
            self.at_time(times[max(0,min(len(times)-1,index+delta))],key)
        return 'break'
    def at_time(self,t,key):
        self.selected_time=t;self.clear_cursor();left,right,upper,bottom,start,end,top=self.bounds[key]
        c=self.canvases[key];x=left+(t-start)/(end-start)*(right-left);c.create_line(x,upper,x,bottom,fill=MUTED,dash=(2,3),tags='cursor')
        parts=[next(title+' / '+unit for k,title,unit,_ in ALL_METRICS if k==key)+' · 对齐 '+fmt(t)+'s']
        for role,label,color,_ in SERIES:
            rows=self.visible[key][role]
            if not rows or t<rows[0]['elapsed_s'] or t>rows[-1]['elapsed_s']:parts.append(label+'：此区间无采样');continue
            row=min(rows,key=lambda r:abs(r['elapsed_s']-t));value=row.get(key)
            parts.append(label+' '+fmt(value)+'（实际 '+fmt(row['elapsed_s'])+'s）')
            if number(value) and value>=0:
                px=left+(row['elapsed_s']-start)/(end-start)*(right-left);py=bottom-value/top*(bottom-upper)
                c.create_oval(px-4,py-4,px+4,py+4,fill=color,outline='white',tags='cursor')
        self.info.set('；'.join(parts))
    def destroy(self):
        if self.pending is not None:self.after_cancel(self.pending)
        super().destroy()
