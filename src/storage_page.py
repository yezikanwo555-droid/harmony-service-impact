"""Read-only cache snapshots from the verified HarmonyOS Settings UI.

Only opens Storage, expands its app list, scrolls and selects an exact bundle ID.
Never clicks clear-cache, uninstall, permission switches or consent dialogs.
"""
import json
import re
import time
import uuid
from decimal import Decimal
import harmony_startup as core

SETTINGS = 'com.huawei.hmos.settings'
ABILITY = SETTINGS + '.MainAbility'


def nodes(tree):
    if not isinstance(tree,dict):return
    yield tree.get('attributes', {})
    for child in tree.get('children', []):yield from nodes(child)


def bounds(node):
    match=re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]',node.get('bounds',''))
    if not match:raise ValueError('无法识别系统页面控件位置')
    return tuple(map(int,match.groups()))


def parse_size(text, unit_base=1000):
    """Retain displayed precision. MB base is explicit, not claimed byte-exact."""
    if unit_base not in (1000,1024):raise ValueError('页面单位基数只能为 1000 或 1024')
    cleaned=re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]','',text).strip()
    match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB|字节)',cleaned,re.I)
    if not match:raise ValueError('缓存尚未统计完成或单位无法识别：'+text)
    number,unit=Decimal(match[1]),match[2].upper()
    power=0 if unit in ('B','字节') else {'K':1,'M':2,'G':3,'T':4}[unit[0]]
    base=1024 if 'I' in unit else unit_base
    return float(number * base**power)


def parse_detail(tree,bundle,unit_base=1000):
    prefix='Setting.AppItem.CacheDataGroup.'+bundle+'_0_'
    items=list(nodes(tree))
    labels=[n for n in items if n.get('id')==prefix+'cacheSizeData.title']
    values=[n for n in items if n.get('id')==prefix+'cacheSizeData.result']
    if len(labels)!=1 or len(values)!=1 or labels[0].get('text')!='缓存':
        raise ValueError('未找到目标包的独立缓存项；不使用总计或数据大小替代')
    raw=values[0].get('text','')
    size=parse_size(raw,unit_base)
    selected=[{k:n.get(k,'') for k in ('id','text','bounds')} for n in items
              if n.get('id','').startswith(('Setting.AppItem.CacheDataGroup.'+bundle+'_0_cacheSizeData',
                                          'Setting.AppItem.AppDataGroup.'+bundle+'_0_'))
              or n.get('id')=='Setting.AppItem.title_id']
    return dict(status='ok',bundle=bundle,raw_text=raw,bytes_estimate=size,mib=size/(1024**2),
                unit_base=unit_base,precision='系统页面舍入值；B 为直接读数，KB/MB/GB 按所设基数换算',
                evidence=selected,source='system_storage_page',recorded_at=core.timestamp())


class StoragePageReader:
    def __init__(self,hdc,bundle,cancel,notify=lambda message:None,unit_base=1000):
        self.hdc,self.bundle,self.cancel,self.notify=hdc,bundle,cancel,notify
        self.unit_base=unit_base
        self.remote='/data/local/tmp/impact_cache_'+uuid.uuid4().hex+'.json'
        self.deadline=0

    def check(self):
        if self.cancel.is_set():raise KeyboardInterrupt
        if time.monotonic()>self.deadline:raise ValueError('系统存储页定位超时（180 秒），未读取缓存')

    def pause(self,seconds=.35):
        if self.cancel.wait(seconds):raise KeyboardInterrupt

    def layout(self):
        self.check()
        result=self.hdc.shell('uitest','dumpLayout','-b',SETTINGS,'-p',self.remote)
        if 'DumpLayout saved to:' not in result:raise ValueError('无法读取系统页面；请保持手机解锁亮屏')
        try:tree=json.loads(self.hdc.shell('cat',self.remote))
        except (ValueError,TypeError) as exc:raise ValueError('系统页面控件数据无效') from exc
        if not isinstance(tree,dict):raise ValueError('未获得系统页面控件树')
        return tree

    def click(self,node):
        self.check()
        x1,y1,x2,y2=bounds(node)
        if node.get('enabled')=='false' or x2<=x1 or y2<=y1:raise ValueError('目标系统控件不可操作')
        output=self.hdc.shell('uitest','uiInput','click',str((x1+x2)//2),str((y1+y2)//2))
        if 'No Error' not in output:raise ValueError('系统页面点击失败：'+output[:160])
        self.pause()

    def read(self):
        self.deadline=time.monotonic()+180
        # Confirm the installed Settings actually advertises the observed route.
        info=core.bundle_json(self.hdc.shell('bm','dump','-n',SETTINGS))
        abilities=[a for m in info.get('hapModuleInfos',[]) for a in m.get('abilityInfos',[])]
        compatible=any(a.get('name')==ABILITY and any(u.get('scheme')=='settings' and u.get('host')=='storage_manager'
                       for s in a.get('skills',[]) for u in s.get('uris',[])) for a in abilities)
        if not compatible:raise ValueError('当前设置版本未声明已验证的存储页入口，不能自动采集')
        output=self.hdc.shell('aa','start','-b',SETTINGS,'-a',ABILITY,'-U','settings://storage_manager')
        if 'start ability successfully' not in output:raise ValueError('无法打开系统存储页，请检查锁屏状态：'+output[:200])
        self.pause(.7)
        target_id='Setting.Storage.AppGroup.'+self.bundle+',0.title'
        previous=None;repeats=0;expanded=False;name=None
        for page in range(40):
            tree=self.layout();items=list(nodes(tree))
            titles=[n for n in items if n.get('id')=='Setting.Storage.title_id']
            if len(titles)!=1:raise ValueError('当前不在预期存储页，已停止自动操作')
            _,_,width,height=bounds(tree['attributes'])
            header_bottom=bounds(titles[0])[3]+35
            def usable(n):
                if n.get('visible')=='false':return False
                x1,y1,x2,y2=bounds(n)
                return 0<=x1<x2<=width and header_bottom<y1<y2<height-70
            targets=[n for n in items if n.get('id')==target_id and usable(n)]
            if len(targets)==1:
                name=targets[0].get('text');self.click(targets[0]);break
            if len(targets)>1:raise ValueError('目标包条目重复，无法确定应用实例')
            more=[n for n in items if n.get('text')=='查看更多' and n.get('type')=='Text' and usable(n)]
            if len(more)==1 and not expanded:
                self.click(more[0]);expanded=True;previous=None;continue
            signature=[(n.get('id'),n.get('bounds')) for n in items if n.get('id','').startswith('Setting.Storage.AppGroup.')]
            repeats=repeats+1 if signature==previous else 0
            if repeats>=2:raise ValueError('已到存储列表末尾，未找到目标包 '+self.bundle)
            previous=signature
            self.notify('正在定位系统存储列表中的目标包，第 %d 页'%(page+1))
            output=self.hdc.shell('uitest','uiInput','swipe',str(width//2),str(int(height*.88)),
                                  str(width//2),str(max(header_bottom+50,int(height*.28))),'1200')
            if 'No Error' not in output:raise ValueError('系统列表滚动失败')
            self.pause()
        else:raise ValueError('存储列表超过 40 页，自动定位已停止')
        last=None;stable=0;error='缓存读数尚未就绪'
        for _ in range(6):
            tree=self.layout()
            items=list(nodes(tree))
            # Reject navigation to a different page even if user interrupts the run.
            if not any(n.get('id')=='Setting.AppItem.title_id' and n.get('text')==name for n in items):
                raise ValueError('目标存储详情页发生变化，已停止读取')
            try:
                result=parse_detail(tree,self.bundle,self.unit_base)
                stable=stable+1 if result['raw_text']==last else 1
                last=result['raw_text']
                if stable>=2:
                    result['settings_version']=info.get('versionName')
                    result['app_name']=name
                    # Leave settings without opening the target app or touching its data.
                    self.hdc.shell('uitest','uiInput','keyEvent','Home')
                    return result
            except ValueError as exc:error=str(exc)
            self.pause(.7)
        raise ValueError('缓存读数未稳定：'+error)
