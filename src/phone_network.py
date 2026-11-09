"""HarmonyOS 7 Chinese Settings adapter, using observed controls only."""
import json
import time
import uuid
from storage_page import nodes,bounds,SETTINGS,ABILITY

ENTRY='Setting.wifi_config_menu.wifi_config_group_three.wifi_setup_proxy_entry'
TITLE='Setting.wifi_config_menu.title_id'
FIELDS={'host':'underline_title_wifi_proxy_hostname_label','port':'underline_title_wifi_proxy_port_label',
        'exclude':'underline_title_proxy_exclusionlist_label'}

class PhoneNetwork:
    def __init__(self,hdc,cancel):
        self.hdc,self.cancel=hdc,cancel
        self.remote='/data/local/tmp/impact_network_'+uuid.uuid4().hex+'.json'
    def pause(self,s=.35):
        if self.cancel.wait(s):raise KeyboardInterrupt
    def layout(self):
        for _ in range(4):
            if self.cancel.is_set():raise KeyboardInterrupt
            output=self.hdc.shell('uitest','dumpLayout','-b',SETTINGS,'-p',self.remote)
            if 'DumpLayout saved to:' not in output:raise ValueError('无法读取设置页，请保持手机解锁亮屏')
            tree=json.loads(self.hdc.shell('cat',self.remote));ns=list(nodes(tree))
            if len(ns)>=3:return ns
            self.pause(.5)
        raise ValueError('设置页未就绪或手机已锁屏；自动网络操作已停止')
    def find(self,key,items=None):
        items=self.layout() if items is None else items
        found=[n for n in items if n.get('id')==key]
        if len(found)!=1:raise ValueError('当前系统页面不兼容，找不到唯一控件：'+key)
        return found[0]
    def click(self,n):
        x,y,x2,y2=bounds(n)
        if n.get('enabled')=='false' or n.get('visible')=='false' or x2<=x or y2<=y:raise ValueError('系统控件不可操作')
        result=self.hdc.shell('uitest','uiInput','click',str((x+x2)//2),str((y+y2)//2))
        if 'No Error' not in result:raise ValueError('系统网络控件点击失败')
        self.pause()
    def key(self,*args):
        if 'No Error' not in self.hdc.shell('uitest','uiInput','keyEvent',*args):raise ValueError('系统页面按键失败')
        self.pause()
    def open_home(self):
        # Reset Settings navigation only; target applications are never modified.
        self.hdc.shell('aa','force-stop',SETTINGS)
        output=self.hdc.shell('aa','start','-b',SETTINGS,'-a',ABILITY)
        if 'start ability successfully' not in output:raise ValueError('无法打开设置，请解锁并保持手机亮屏')
        self.pause(.7);self.find('Setting.Home')
    def open_proxy(self,ssid):
        self.open_home();self.click(self.find('wifi_entry'))
        items=self.layout();self.find('Setting.Wlan.title_id',items)
        connected=[n for n in items if n.get('type')=='Row' and n.get('id','').startswith('Setting.Wlan.AvailableWlan.')
                   and n.get('text','').startswith(ssid+', 已连接')]
        if len(connected)!=1:raise ValueError('当前已连接 Wi-Fi 与备份不一致，停止修改代理：'+ssid)
        self.click(connected[0])
        for _ in range(6):
            items=self.layout()
            if self.find(TITLE,items).get('text')!=ssid:raise ValueError('Wi-Fi 详情不是目标网络')
            target=[n for n in items if n.get('id')==ENTRY and 380<bounds(n)[1]<bounds(n)[3]<2700]
            if len(target)==1:self.click(target[0]);return
            root=items[0];_,_,w,h=bounds(root)
            self.hdc.shell('uitest','uiInput','swipe',str(w//2),str(int(h*.85)),str(w//2),str(int(h*.32)),'1200')
            self.pause()
        raise ValueError('未找到可见的 Wi-Fi 代理入口')
    def read_proxy_page(self,ssid):
        ns=self.layout();self.find('button_1_key_save_button',ns)
        if not any(n.get('text')=='代理' and n.get('type')=='Text' for n in ns):raise ValueError('当前不是代理页')
        selected=[n.get('text') for n in ns if n.get('type')=='Column' and n.get('checked')=='true' and n.get('text') in ('无','手动')]
        if len(selected)!=1:raise ValueError('代理选项无法识别，保留原设置')
        value=dict(ssid=ssid,mode='none' if selected[0]=='无' else 'manual')
        if value['mode']=='manual':
            value.update({k:self.find(v,ns).get('text','') for k,v in FIELDS.items()})
        return value
    def read_proxy(self,ssid):
        # Changing Wi-Fi proxy can recreate the Settings activity on this firmware.
        # Retry navigation only; never retry a partially completed mutation here.
        for attempt in range(3):
            try:
                self.open_proxy(ssid);return self.read_proxy_page(ssid)
            except ValueError as exc:
                transient=any(s in str(exc) for s in ('设置页未就绪','Setting.Home','Setting.wifi_config_menu.title_id'))
                if not transient or attempt==2:raise
                self.pause(1)
    def input(self,key,text):
        n=self.find(key)
        self.click(n)
        # Actual HarmonyOS inputText appends; explicitly select all before replacing.
        self.key('2072','2017')
        if text:self.hdc.shell('uitest','uiInput','text',text)
        else:self.key('2055')
        self.pause()
        if self.find(key).get('text','')!=text:raise ValueError('代理字段写入后回读不一致，尚未保存')
    def set_proxy(self,value):
        current=self.read_proxy(value['ssid'])
        if current==value:return current
        text='无' if value['mode']=='none' else '手动'
        selected=[n for n in self.layout() if n.get('text')==text and n.get('type')=='Column']
        if len(selected)!=1:raise ValueError('系统代理选项不兼容')
        if selected[0].get('checked')!='true':self.click(selected[0])
        if value['mode']=='manual':
            for key in ('host','port','exclude'):self.input(FIELDS[key],str(value.get(key,'')))
            self.key('Back') # dismiss keyboard; re-find save, never use stale coordinates
        if self.read_proxy_page(value['ssid'])!=value:raise ValueError('代理保存前校验失败')
        self.click(self.find('button_1_key_save_button'));self.pause(.7)
        # This firmware may close Settings after committing a proxy change.
        # Reopen the exact SSID instead of assuming a navigation destination.
        actual=self.read_proxy(value['ssid'])
        if actual!=value:raise ValueError('代理保存后回读不一致')
        return actual
    def home(self):self.key('Home')
    def set_cellular(self,enabled):
        self.open_home();self.click(self.find('mobile_network_entry'))
        self.click(self.find('Setting.MobileNetwork.air_plane_group.mobile_data_settings_entry'))
        ns=self.layout()
        rows=[n for n in ns if n.get('type')=='ListItem' and n.get('text','').startswith('移动数据, 启用后')]
        if len(rows)!=1:raise ValueError('移动数据开关页面不兼容')
        left,top,right,bottom=bounds(rows[0])
        switches=[n for n in ns if n.get('type')=='Toggle' and top<=bounds(n)[1]<bounds(n)[3]<=bottom]
        if len(switches)!=1 or switches[0].get('enabled')!='true':
            raise ValueError('移动数据开关不可用，请检查 SIM 卡；未执行蜂窝测试')
        if switches[0].get('checked') not in ('true','false'):raise ValueError('移动数据开关状态无法读取')
        if (switches[0]['checked']=='true')!=enabled:self.click(switches[0])
        self.pause(1)
