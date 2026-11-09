"""Read the focused application's verified bundle without changing device UI."""
import re
import harmony_startup as core
from monkey_runner import focus_pid
from startup_app import DesktopHdc


def foreground_bundles(text):
    result=set()
    for record in re.split(r'AbilityRecord ID\s*#\d+',text)[1:]:
        if not re.search(r'^\s*state\s+#FOREGROUND\b',record,re.M):continue
        names=re.findall(r'^\s*bundle name\s+\[([^\]]+)\]',record,re.M)
        if len(names)==1:result.add(core.identifier(names[0]))
    return sorted(result)


def detect(hdc):
    focused=focus_pid(hdc.shell('hidumper','-s','WindowManagerService','-a','-a'))
    status=hdc.shell('cat','/proc/%d/status'%focused[0])
    match=re.search(r'^Uid:\s+(\d+)',status,re.M)
    if not match:raise ValueError('无法读取当前应用身份，请解锁并打开目标应用后重试')
    uid=int(match[1]);matches=[]
    candidates=foreground_bundles(hdc.shell('aa','dump','-l'))
    if len(candidates)>20:raise ValueError('前台应用信息异常，请关闭多窗口后重试')
    for bundle in candidates:
        info=core.bundle_json(hdc.shell('bm','dump','-n',bundle))
        installed_uid=info.get('uid') or info.get('applicationInfo',{}).get('uid')
        if installed_uid==uid:
            matches.append(dict(bundle=bundle,version=info.get('versionName',''),device=hdc.target))
    if len(matches)!=1:
        raise ValueError('未能唯一识别当前应用。请解锁手机、打开目标应用，并关闭通知栏或系统弹窗后重试')
    if focus_pid(hdc.shell('hidumper','-s','WindowManagerService','-a','-a'))!=focused:
        raise ValueError('识别过程中前台应用发生变化，请保持目标页面后重试')
    return matches[0]


def run(config,role,events,cancel):
    payload=dict(role=role,result=None,error='',cancelled=False)
    try:
        hdc=DesktopHdc(core.find_hdc(config.get('hdc') or None),config.get('target') or None,30,cancel)
        hdc.select_device()
        payload['result']=detect(hdc)
        if cancel.is_set():raise KeyboardInterrupt
    except KeyboardInterrupt:payload['result']=None;payload['cancelled']=True
    except Exception as exc:payload['error']=str(exc)
    finally:events.put(('detect_done',payload))
