"""Read-only resource sampling and before/after comparison; no tracking-event tests."""
import csv
import datetime as dt
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import time
import harmony_startup as core
from startup_app import DesktopHdc
from storage_page import StoragePageReader
from monkey_runner import MonkeySession
import network_env

MIB = 1024 * 1024
SCHEMA = 'harmony-service-impact-v1'
TEST_NAMES={'both':'冷热启动','cold':'仅冷启动','hot':'仅热启动','monkey':'随机 Monkey','both_monkey':'冷热启动 + Monkey','resources':'仅资源监控'}


def test_mode(config):
    mode=config.get('test_mode', 'resources' if config.get('monitor_only') else 'both')
    if mode not in TEST_NAMES:raise ValueError('未知测试方式：'+str(mode))
    return mode


def startup_modes(config):
    return {'both':('cold','hot'),'cold':('cold',),'hot':('hot',),'monkey':(),
            'both_monkey':('cold','hot'),'resources':()}[test_mode(config)]


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_stat(text):
    end = text.rfind(')')
    fields = text[end + 2:].split()
    if end < 0 or len(fields) < 20:
        raise ValueError('无法读取进程 CPU 累计时间')
    return int(fields[11]) + int(fields[12]), int(fields[19])


def parse_total_cpu(text):
    m = re.search(r'^cpu\s+([\d ]+)', text, re.M)
    if not m:
        raise ValueError('无法读取整机 CPU 时钟')
    values = [int(x) for x in m.group(1).split()]
    if len(values) < 4:
        raise ValueError('整机 CPU 时钟字段不足')
    return sum(values[:8])  # guest/guest_nice already included in user/nice


def cpu_percent(previous, current):
    if not previous or not current or previous['identities'] != current['identities']:
        return None
    total = current['total'] - previous['total']
    used = current['ticks'] - previous['ticks']
    if total <= 0 or used < 0 or used > total:
        return None
    return 100 * used / total


def parse_pss(text):
    m = re.search(r'^\s*Total\s+(\d+)\s+', text, re.M)
    if not m:
        raise ValueError('hidumper 未返回 PSS Total')
    return int(m.group(1)) / 1024


def parse_du(text):
    if re.search(r'permission denied|not permitted|no such file|invalid bundle|\[fail\]|not found', text, re.I):
        raise ValueError(text.strip()[:250])
    m = re.fullmatch(r'\s*(\d+)\s+[^\r\n]+\s*', text)
    if not m:
        raise ValueError('未返回可识别的缓存目录大小')
    return int(m.group(1)) / 1024


def artifact_info(filename, cancel):
    if not filename:
        return {'status': 'missing', 'reason': '未选择本地 APP/HAP 文件'}
    p = Path(filename)
    if p.suffix.lower() not in ('.app', '.hap') or not p.is_file():
        raise ValueError('请选择存在的 .app 或 .hap 文件')
    before = p.stat()
    digest = hashlib.sha256()
    with p.open('rb') as stream:
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    after = p.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ValueError('计算包体时文件发生变化，请选择固定的构建产物')
    return {'status': 'ok', 'name': p.name, 'format': p.suffix.lower(), 'bytes': after.st_size,
            'mib': after.st_size / MIB, 'sha256': digest.hexdigest(),
            'source': '用户选择的本地构建产物；未自动验证与设备安装版本相同'}


class ResourceMonitor:
    def __init__(self, hdc, bundle, uid, cache_path, page_cache=False):
        self.hdc, self.bundle, self.uid = hdc, bundle, int(uid)
        self.cache_path = cache_path
        self.previous = None
        self.cache_error = None
        self.page_cache = page_cache

    def sample(self, elapsed, raw):
        row = {'recorded_at': core.timestamp(), 'elapsed_s': round(elapsed, 3), 'pids': '',
               'cpu_pct': None, 'pss_mib': None, 'rss_mib': None, 'cache_mib': None,
               'cpu_note': '', 'memory_note': '', 'cache_note': ''}
        try:
            ps = self.hdc.shell('ps', '-A', '-o', 'PID,UID,NAME', log=None)
            pids = [int(m[1]) for line in ps.splitlines()
                    if (m := re.match(r'^\s*(\d+)\s+(\d+)\s+', line)) and int(m[2]) == self.uid]
            raw.append({'matching_uid': self.uid, 'pids': pids})
            row['pids'] = ','.join(map(str, sorted(pids)))
            if not pids:
                raise ValueError('目标 UID 下无进程；本次采样不记为 0')
            ticks, identities, mems, rss = 0, [], [], []
            cpu_errors, mem_errors = [], []
            for pid in sorted(pids):
                try:
                    used, identity = parse_stat(self.hdc.shell('cat', '/proc/%d/stat' % pid, log=raw))
                    ticks += used
                    identities.append((pid, identity))
                except (ValueError, core.MeasurementError) as exc:
                    cpu_errors.append(str(exc))
                try:
                    mems.append(parse_pss(self.hdc.shell('hidumper', '--mem', str(pid), log=raw)))
                except (ValueError, core.MeasurementError) as exc:
                    mem_errors.append(str(exc))
                try:
                    status = self.hdc.shell('cat', '/proc/%d/status' % pid, log=raw)
                    m = re.search(r'^VmRSS:\s+(\d+)\s+kB', status, re.M)
                    if m:
                        rss.append(int(m[1]) / 1024)
                except core.MeasurementError:
                    pass
            try:
                total = parse_total_cpu(self.hdc.shell('cat', '/proc/stat', log=raw))
                current = {'ticks': ticks, 'total': total, 'identities': identities} if not cpu_errors else None
                row['cpu_pct'] = cpu_percent(self.previous, current)
                row['cpu_note'] = '' if row['cpu_pct'] is not None else '首个采样、进程集合变化或 CPU 数据不可读'
                self.previous = current
            except (ValueError, core.MeasurementError) as exc:
                row['cpu_note'], self.previous = str(exc), None
            if len(mems) == len(pids):
                row['pss_mib'] = sum(mems)
            else:
                row['memory_note'] = '部分进程内存不可读，未用部分值代替整包：' + '; '.join(mem_errors)
            if len(rss) == len(pids):
                row['rss_mib'] = sum(rss)
        except (ValueError, core.MeasurementError) as exc:
            row['cpu_note'] = row['memory_note'] = str(exc)
            self.previous = None
        if self.page_cache:
            row['cache_note'] = '系统存储页仅在测试前后读取，资源采样期间不切换页面'
        elif self.cache_error:
            row['cache_note'] = self.cache_error
        else:
            try:
                row['cache_mib'] = parse_du(self.hdc.shell('du', '-sk', self.cache_path, log=raw, allow_nonzero=True))
            except (ValueError, core.MeasurementError) as exc:
                self.cache_error = '缓存无法自动读取：' + str(exc)
                row['cache_note'] = self.cache_error
        return row


def collect(config, events, cancel):
    """One installed version per run. Never install, uninstall or clear app data."""
    def emit(kind, payload):
        events.put((kind, payload))
    out, data, resource_rows, startup_rows = None, None, [], []
    monkey=None
    network=None
    network_session=None
    try:
        core.identifier(config['bundle'])
        mode=test_mode(config)
        use_monkey=mode in ('monkey','both_monkey')
        out = Path(config['out']).resolve() / ('%s_%s' % (config['role'], dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')))
        (out / 'logs').mkdir(parents=True)
        data = {'schema': SCHEMA, 'role': config['role'], 'label': config['label'], 'config': config,
                'started_at': core.timestamp(), 'state': 'running', 'error': '', 'startup': [], 'resources': [],
                'artifact': {'status': 'missing'}, 'environment': {}, 'cache': {}, 'monkey':{'status':'not_selected'},
                'metric_definition': {'cpu': '同 UID 可见进程 CPU 累计时间差 / 整机全部 CPU 时间差，整机容量百分比（0～100）',
                                      'memory': '同 UID 可见进程 hidumper PSS Total 之和，MiB',
                                      'cache': '指定 cache 目录 du -sk 磁盘占用，MiB；不代表所有应用数据',
                                      'startup': 'aa start -W TotalTime；冷到首帧，热到前台'}}
        save_json(out / 'result.json', data)
        emit('out', str(out))
        hdc = DesktopHdc(core.find_hdc(config.get('hdc') or None), config.get('target') or None, 30, cancel)
        hdc.select_device()
        raw = []
        info = core.bundle_json(hdc.shell('bm', 'dump', '-n', config['bundle']))
        module, ability, process = core.resolve_entry(info, config.get('ability') or None, config.get('module') or None)
        uid = info.get('uid') or info.get('applicationInfo', {}).get('uid')
        if not isinstance(uid, int) or uid <= 0:
            raise ValueError('包信息中无法确定应用 UID，不能进行整包进程监控')
        if config.get('network_auto') and config.get('network_mode','current')!='current' and not config.get('_network_session_id'):
            from network_session import Session
            network_session=Session(hdc,config,out,cancel,emit);network_session.begin()
        if config.get('_network_session_id'):
            from network_session import ACTIVE
            session=ACTIVE.get(config['_network_session_id'])
            if session:session.new_group()
        if 'network_mode' in config:
            network_env.validate(config)
            network=network_env.Guard(hdc,config,out,data,emit)
            network.checkpoint('start',probe=True)
        user = uid // 200000
        cache_path = config.get('cache_path') or '/data/app/el2/%d/base/%s/cache' % (user, config['bundle'])
        if not cache_path.startswith('/') or '\n' in cache_path or '\r' in cache_path:
            raise ValueError('缓存路径需要设备上的绝对目录路径')
        page_cache = config.get('cache_mode') == 'system_ui'
        if page_cache:
            data['metric_definition']['cache']='系统存储页目标包缓存项的测试前后快照；保留原文，按明确基数换算 MiB，不代表精确字节量'
        data['cache'] = {'path': None if page_cache else cache_path, 'source': 'system_storage_page' if page_cache else 'auto',
                         'scope': '整个测试（启动与资源监控）' if page_cache else '资源监控阶段',
                         'unit_base': int(config.get('storage_unit_base',1000)), 'checkpoints': [],
                         'manual_start_mib': config.get('cache_start'),
                         'manual_end_mib': config.get('cache_end')}
        hashes = sorted((m.get('moduleName'), m.get('hashValue')) for m in info.get('hapModuleInfos', []) if m.get('hashValue'))
        data['environment'] = {'device': hdc.target, 'bundle': config['bundle'], 'version_name': info.get('versionName'),
            'version_code': info.get('versionCode'), 'module': module, 'ability': ability, 'uid': uid,
            'system': hdc.shell('param', 'get', 'const.product.software.version').strip(),
            'model': hdc.shell('param', 'get', 'const.product.model').strip(),
            'installed_hash': hashlib.sha256(json.dumps(hashes).encode()).hexdigest() if hashes else None}
        data['artifact'] = artifact_info(config.get('artifact'), cancel)
        emit('log', '%s：设备 %s，已安装版本 %s，入口 %s/%s' % (config['label'], hdc.target, info.get('versionName'), module, ability))
        save_json(out / 'result.json', data)
        args = core.parser().parse_args(['--bundle', config['bundle'], '--count', str(config['count'])])
        args.module, args.ability, args.process = module, ability, process or config['bundle']
        args.target, args.cancel = hdc.target, cancel
        args.settle = float(config['settle'])
        def snapshot(stage):
            emit('phase', '自动读取'+('测试前' if stage=='start' else '测试后')+'缓存，请勿操作手机')
            reader=StoragePageReader(hdc,config['bundle'],cancel,lambda msg:emit('log',msg),data['cache']['unit_base'])
            try:
                point=reader.read()
            except (ValueError, core.MeasurementError) as exc:
                point={'status':'unavailable','mib':None,'raw_text':'','recorded_at':core.timestamp(),'error':str(exc)}
            finally:
                if not cancel.is_set():
                    try:hdc.shell('rm','-f',reader.remote)
                    except core.MeasurementError:pass
            point['stage']=stage
            data['cache']['checkpoints'].append(point)
            with (out/'cache_snapshots.csv').open('w',newline='',encoding='utf-8-sig') as stream:
                fields=['stage','recorded_at','status','raw_text','mib','unit_base','error']
                writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
                writer.writeheader();writer.writerows(data['cache']['checkpoints'])
            save_json(out/'logs'/('cache_'+stage+'.json'),point)
            save_json(out/'result.json',data)
            emit('cache_snapshot',point)
            emit('log',('测试前' if stage=='start' else '测试后')+'缓存：'+(point['raw_text'] if point['status']=='ok' else '不可用，'+point.get('error','')))
        if page_cache:snapshot('start')
        if network:network.checkpoint('before_tests')
        if startup_modes(config):
            if '-W' not in hdc.shell('aa', 'start', '-h'):
                raise ValueError('设备不支持 aa start -W')
            with (out / 'startup.csv').open('w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=core.FIELDS)
                writer.writeheader()
                for mode in startup_modes(config):
                    for i in range(1, int(config['count']) + 1):
                        if cancel.is_set():
                            raise KeyboardInterrupt
                        emit('log', '%s %d/%s' % ('冷启动' if mode == 'cold' else '热启动', i, config['count']))
                        row, interrupted = core.measure(hdc, args, mode, i, out)
                        startup_rows.append(row)
                        writer.writerow(row)
                        f.flush()
                        data['startup'] = startup_rows
                        save_json(out / 'result.json', data)
                        emit('startup', row)
                        if interrupted:
                            raise KeyboardInterrupt
        # Resource sampling is a separate phase so it does not inflate launch times.
        if network:network.checkpoint('before_resources')
        core.start(hdc, args, raw)
        core.pause(args, args.settle)
        save_json(out / 'logs' / 'resource_prepare.json', raw)
        emit('log', ('Monkey 资源监控开始，请勿手动操作手机。场景：' if use_monkey else '资源监控开始：请按两版相同的操作步骤使用元服务。当前场景：') + config['scenario'])
        emit('phase', '资源监控中；结束后自动读取缓存' if page_cache else '资源与缓存监控中')
        monitor = ResourceMonitor(hdc, args.bundle, uid, cache_path, page_cache=page_cache)
        began = time.monotonic()
        duration, interval = float(config['duration']), float(config['interval'])
        if use_monkey:
            monkey=MonkeySession(hdc,config,out/'logs',cancel)
            data['monkey']={'status':'running'}
            save_json(out/'result.json',data)
            emit('phase','随机 Monkey 运行中：次数或时长先到即结束')
            monkey.start()
            began=monkey.started
        with (out / 'resources.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = None
            i = 0
            next_sample=began
            while monkey.running() if use_monkey else time.monotonic() - began < duration:
                if cancel.is_set():
                    raise KeyboardInterrupt
                if use_monkey:
                    event=monkey.step()
                    if event:emit('monkey_event',event)
                    if time.monotonic()<next_sample:continue
                raw = []
                row = monitor.sample(time.monotonic() - began, raw)
                i += 1
                resource_rows.append(row)
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                f.flush()
                save_json(out / 'logs' / ('resource_%04d.json' % i), raw)
                data['resources'] = resource_rows
                save_json(out / 'result.json', data)
                emit('resource', row)
                if network and time.monotonic()-network.last>=15:network.checkpoint('monitor')
                next_sample=time.monotonic()+interval
                if use_monkey:continue
                wait = min(max(0, duration - (time.monotonic() - began)), interval)
                if wait and cancel.wait(wait):
                    raise KeyboardInterrupt
        if use_monkey:
            data['monkey']=monkey.finish()
            save_json(out/'logs'/'monkey_summary.json',data['monkey'])
            emit('log','Monkey：'+data['monkey']['stop_reason'])
        if page_cache:snapshot('end')
        if network:network.checkpoint('end',probe=True)
        data['state'] = 'error' if use_monkey and data['monkey']['status']!='complete' else 'complete'
        if data['state']=='error':data['error']=data['monkey']['stop_reason']
        emit('log', '本组采集结束：'+data['state'])
    except KeyboardInterrupt:
        if data:
            data['state'] = 'interrupted'
        emit('log', '已停止，保留已采集数据。')
    except Exception as exc:
        if data:
            data['state'], data['error'] = 'error', str(exc)
        emit('log', '采集失败：' + str(exc))
    finally:
        if network_session:
            try:data['network_restore']=network_session.finish()
            except Exception as exc:
                data['network_restore']=network_session.state
                data['state'],data['error']='error',data.get('error','')+'；'+str(exc)
        if monkey is not None and data is not None:
            try:
                data['monkey']=monkey.finish(cancelled=cancel.is_set())
                save_json(out/'logs'/'monkey_summary.json',data['monkey'])
                if data['monkey'].get('cleanup_note'):
                    data['state'],data['error']='error',data['monkey']['cleanup_note']
            except Exception as exc:
                data['monkey']={'status':'error','stop_reason':'Monkey 收尾失败：'+str(exc)}
                data['state'],data['error']='error',data['monkey']['stop_reason']
        if out and data:
            data['finished_at'] = core.timestamp()
            try:
                save_json(out / 'result.json', data)
            except OSError as exc:
                emit('log', '保存失败：' + str(exc))
        emit('done', {'out': str(out) if out else None, 'result': data})
    return {'out':str(out) if out else None,'result':data}


def load_result(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('请选择本工具生成的 result.json')
    if not isinstance(data.get('resources'), list) or not isinstance(data.get('startup'), list):
        raise ValueError('结果结构不完整')
    if any(not isinstance(data.get(k), dict) for k in ('environment', 'config', 'artifact', 'cache')) or any(not isinstance(row,dict) for k in ('startup','resources') for row in data[k]) or any(k not in data for k in ('role','label','state')):
        raise ValueError('结果结构不完整，请选择未经修改的采集文件或报告快照')
    return data


def values(rows, key):
    return [r[key] for r in rows if finite(r.get(key))]


def summarize(data):
    result = {}
    for mode in ('cold', 'hot'):
        valid = [r['total_time_ms'] for r in data['startup'] if r.get('requested_mode') == mode and r.get('status') == 'ok' and finite(r.get('total_time_ms'))]
        result[mode + '_mean'] = statistics.mean(valid) if valid else None
        result[mode + '_p95'] = sorted(valid)[math.ceil(len(valid) * .95) - 1] if valid else None
        result[mode + '_n'] = len(valid)
    for metric in ('cpu_pct', 'pss_mib'):
        nums = values(data['resources'], metric)
        result[metric + '_mean'] = statistics.mean(nums) if nums else None
        result[metric + '_peak'] = max(nums) if nums else None
        result[metric + '_n'] = len(nums)
    cache = values(data['resources'], 'cache_mib')
    manual = data.get('cache', {})
    start, end = manual.get('manual_start_mib'), manual.get('manual_end_mib')
    if manual.get('source') == 'system_storage_page':
        checkpoints={p.get('stage'):p for p in manual.get('checkpoints',[]) if p.get('status')=='ok'}
        start=checkpoints.get('start',{}).get('mib');end=checkpoints.get('end',{}).get('mib')
        result.update(cache_start=start,cache_end=end,cache_peak=None,
                      cache_growth=end-start if finite(start) and finite(end) else None,
                      cache_source='系统存储页自动快照')
    elif cache:
        result.update(cache_start=cache[0], cache_end=cache[-1], cache_peak=max(cache),
                      cache_growth=cache[-1] - cache[0] if len(cache) >= 2 else None, cache_source='自动目录监控')
    elif finite(start) and finite(end):
        result.update(cache_start=start, cache_end=end, cache_peak=None, cache_growth=end-start, cache_source='人工补录系统缓存读数')
    else:
        result.update(cache_start=None, cache_end=None, cache_peak=None, cache_growth=None, cache_source='不可用')
    result['package_mib'] = data.get('artifact', {}).get('mib') if data.get('artifact', {}).get('status') == 'ok' else None
    return result


METRICS = [('cold_mean', '冷启动平均', 'ms'), ('cold_p95', '冷启动 P95', 'ms'),
           ('hot_mean', '热启动平均', 'ms'), ('hot_p95', '热启动 P95', 'ms'),
           ('package_mib', '本地包文件大小', 'MiB'), ('cpu_pct_mean', '应用 CPU 平均（整机容量）', '%'),
           ('cpu_pct_peak', '应用 CPU 采样峰值', '%'), ('pss_mib_mean', '应用 PSS 平均', 'MiB'),
           ('pss_mib_peak', '应用 PSS 采样峰值', 'MiB'), ('cache_start', '采集开始缓存', 'MiB'),
           ('cache_end', '采集结束缓存', 'MiB'), ('cache_growth', '采集期间缓存增长', 'MiB')]


def cache_location(data):
    path=data.get('cache',{}).get('path')
    bundle=data.get('environment',{}).get('bundle')
    # Equivalent sandbox directories differ by the intentional package identity.
    return path.replace('/'+bundle+'/', '/{bundle}/') if isinstance(path,str) and bundle else path


def comparison(before, after, threshold=10):
    if not finite(threshold) or threshold < 0:
        raise ValueError('关注阈值必须是非负有限数值')
    a, b = summarize(before), summarize(after)
    warnings, notes = [], []
    warnings.extend(network_env.warnings(before,after))
    ma,mb=test_mode(before['config']),test_mode(after['config'])
    if ma!=mb:warnings.append('测试方式不同，未选择的项目不可直接对比')
    if any(m in ('monkey','both_monkey') for m in (ma,mb)):
        for key in ('monkey_seconds','monkey_events','monkey_interval_ms','monkey_seed'):
            if before['config'].get(key)!=after['config'].get(key):warnings.append('Monkey 参数不同：'+key)
        for label,d in [('改造前',before),('改造后',after)]:
            if test_mode(d['config']) in ('monkey','both_monkey') and d.get('monkey',{}).get('status')!='complete':
                warnings.append(label+' Monkey 未正常完成')
        counts=[d.get('monkey',{}).get('actual_events') for d in (before,after)]
        if None in counts:notes.append('Monkey 实际事件数有缺失，请查看原始日志，不以计划次数替代')
        elif counts[0]!=counts[1]:warnings.append('Monkey 实际执行事件数不同，资源与缓存增量的工作量不一致')
        notes.append('相同种子只使随机事件生成规则一致，不保证两个版本到达相同页面；UITest 检查与资源采样有开销，事件间隔为最小等待时间，时长到达后不再发起新事件')
    ea, eb = before['environment'], after['environment']
    for key, label in [('device', '设备'), ('system', '系统版本'), ('ability', '启动入口'), ('module', '启动模块')]:
        if not ea.get(key) or ea.get(key) != eb.get(key):
            warnings.append(label + '不一致或缺失，比较存在干扰因素')
    if not ea.get('bundle') or not eb.get('bundle'):
        warnings.append('包名缺失，不能核对两组目标')
    elif ea['bundle'] != eb['bundle']:
        notes.append('本次为两个不同包名的对比；需确认它们对应改造前后同一业务，并保持登录态、配置和初始数据可比')
        if ea.get('uid') is not None and ea.get('uid')==eb.get('uid'):
            warnings.append('两个包共享 UID，CPU/内存可能汇总双方进程，不能直接作为各包独立占用')
    for key, label in [('scenario', '操作场景'), ('duration', '监控时长'), ('interval', '采样间隔'), ('count', '启动轮数'), ('settle', '稳定等待'), ('monitor_only', '采集模式')]:
        if before['config'].get(key) != after['config'].get(key):
            warnings.append(label + '不一致')
    if before.get('role') != 'before' or after.get('role') != 'after':
        warnings.append('导入的组别标签与改造前/改造后位置不符')
    if ea.get('installed_hash') and ea.get('installed_hash') == eb.get('installed_hash'):
        warnings.append('设备安装包指纹相同：当前两组不能证明改造前后差异')
    if not ea.get('installed_hash') or not eb.get('installed_hash'):
        warnings.append('安装包指纹缺失，需要人工确认采集的是两个对应构建版本')
    for label, data, sums in [('改造前', before, a), ('改造后', after, b)]:
        if data['state'] != 'complete':
            warnings.append(label + '采集未完整结束')
        if any(sums[m+'_n']<5 for m in startup_modes(data['config'])):
            warnings.append(label + '所选启动方式有效样本不足 5 轮，仅供初步观察')
        if sums['cpu_pct_n'] < 5 or sums['pss_mib_n'] < 5:
            warnings.append(label + 'CPU 或内存有效样本不足 5 个')
        if any(r.get('status') != 'ok' for r in data['startup']):
            warnings.append(label + '存在启动异常轮次，请检查原始日志')
        count = len(data['resources'])
        if count and (sums['cpu_pct_n'] < max(0, count - 1) or sums['pss_mib_n'] < count):
            warnings.append(label + '资源采样有缺失或进程集合变化，均值仅基于有效样本')
    if before.get('artifact', {}).get('format') != after.get('artifact', {}).get('format'):
        warnings.append('包文件格式不同，APP 与 HAP 不可直接比较包体')
    if a['cache_source'] != b['cache_source'] or cache_location(before) != cache_location(after):
        warnings.append('缓存来源或监控目录不同，不宜直接比较')
    cache_incompatible=(before.get('cache',{}).get('unit_base') != after.get('cache',{}).get('unit_base')
                        and ('系统存储页自动快照' in (a['cache_source'],b['cache_source'])))
    if cache_incompatible:warnings.append('系统存储页单位换算基数不同，不宜直接比较')
    if finite(a['cache_start']) and finite(b['cache_start']) and abs(a['cache_start']-b['cache_start']) > max(1, abs(a['cache_start'])*.05):
        warnings.append('监控开始时缓存占用不同，缓存与资源变化可能受初始数据影响')
    rows = []
    for key, label, unit in METRICS:
        x, y = a.get(key), b.get(key)
        delta = y - x if finite(x) and finite(y) else None
        pct = delta / x * 100 if delta is not None and x > 0 else None
        incompatible = (key == 'package_mib' and before.get('artifact', {}).get('format') != after.get('artifact', {}).get('format')) or (key.startswith('cache_') and (a['cache_source'] != b['cache_source'] or cache_location(before) != cache_location(after)))
        if key.startswith('cache_') and cache_incompatible:incompatible=True
        status = '口径不同' if incompatible else ('缺少数据' if delta is None else ('需关注' if (pct is not None and pct > threshold) or (x == 0 and y > 0) else '未超过关注阈值'))
        launch_mode='cold' if key.startswith('cold_') else 'hot' if key.startswith('hot_') else None
        if launch_mode and all(launch_mode not in startup_modes(d['config']) for d in (before,after)):
            status='未选择此测试'
        elif launch_mode and any(launch_mode not in startup_modes(d['config']) for d in (before,after)):
            status='口径不同'
        if key == 'cache_growth' and delta is not None and (x < 0 or y < 0):
            status = '缓存含清理过程，结合曲线判断'
            pct = None
        rows.append(dict(key=key, label=label, unit=unit, before=x, after=y, delta=delta, percent=pct, status=status))
    missing = any(r['status'] in ('缺少数据', '口径不同') for r in rows)
    if missing:
        warnings.append('存在缺失或口径不同的指标，不能给出完整影响结论')
    conclusion = '需补充验证' if warnings else ('存在需关注的指标增幅' if any(r['status'] == '需关注' for r in rows) else '本次监测指标未超过关注阈值')
    return {'rows': rows, 'warnings': warnings, 'notes':notes, 'conclusion': conclusion, 'threshold_pct': threshold,
            'before_summary': a, 'after_summary': b}


def fmt(value, signed=False):
    if not finite(value):
        return '—'
    return ('%+.2f' if signed else '%.2f') % value


def chart(before, after, key, title):
    series = [before.get('resources', []), after.get('resources', [])]
    points = [(r.get('elapsed_s'), r.get(key)) for rows in series for r in rows if finite(r.get('elapsed_s')) and finite(r.get(key))]
    if not points:
        if key=='cache_mib' and any(d.get('cache',{}).get('source')=='system_storage_page' for d in (before,after)):
            return '<p>系统存储页仅在测试前后读取，不绘制连续曲线。原始读数、时间及增长量见下方快照记录。</p>'
        return '<p>无自动采样数据。人工缓存读数不绘制为连续曲线。</p>'
    xmax = max(1, max(p[0] for p in points))
    ymax = max(1, max(p[1] for p in points)) * 1.1
    svg = '<svg viewBox="0 0 700 205" role="img" aria-label="%s"><path d="M50 12V172H680" fill="none" stroke="#c4cfdf"/>' % html.escape(title)
    for fraction in (0, .5, 1):
        y = 172 - fraction * 150
        svg += '<text x="45" y="%.1f" text-anchor="end" font-size="11">%.1f</text>' % (y+4, ymax*fraction)
    svg += '<text x="50" y="193" font-size="11">0 秒</text><text x="620" y="193" font-size="11">%.1f 秒</text>' % xmax
    for rows, color in zip(series, ['#3270da', '#df7a20']):
        segment = []
        def draw(points):
            return '<polyline points="%s" fill="none" stroke="%s" stroke-width="2"/>' % (' '.join(points), color) if points else ''
        for row in rows:
            if not finite(row.get(key)) or not finite(row.get('elapsed_s')):
                svg += draw(segment)
                segment = []
            else:
                x, y = 50 + row['elapsed_s']/xmax*620, 172 - row[key]/ymax*150
                segment.append('%.1f,%.1f' % (x,y))
                svg += '<circle cx="%.1f" cy="%.1f" r="2" fill="%s"/>' % (x,y,color)
        svg += draw(segment)
    return svg + '</svg>'


def report(before, after, output_dir, threshold=10):
    c = comparison(before, after, threshold)
    root = Path(output_dir) / ('comparison_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    root.mkdir(parents=True)
    save_json(root / 'comparison.json', c)
    save_json(root / 'before_snapshot.json', before)
    save_json(root / 'after_snapshot.json', after)
    with (root / 'comparison.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['指标','单位','改造前','改造后','差值（后-前）','变化率 %','提示'])
        for row in c['rows']:
            writer.writerow([row['label'],row['unit'],row['before'],row['after'],row['delta'],row['percent'],row['status']])
    esc = lambda x: html.escape(str(x))
    table = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                    (esc(r['label']),r['unit'],fmt(r['before']),fmt(r['after']),fmt(r['delta'],True),fmt(r['percent'],True),esc(r['status'])) for r in c['rows'])
    warnings = ''.join('<li>'+esc(w)+'</li>' for w in c['warnings']) or '<li>未发现配置不一致。仍需结合业务回归与样本量判断。</li>'
    groups = ''
    for name, data in [('改造前',before),('改造后',after)]:
        env, art = data['environment'], data.get('artifact',{})
        groups += '<section><h2>%s · %s</h2><p>安装版本：%s（%s） · 设备：%s<br>系统：%s<br>场景：%s<br>开始：%s<br>文件：%s · SHA256：<code>%s</code><br>缓存来源：%s<br>有效冷/热样本：%s / %s</p></section>' % (name,esc(data['label']),esc(env.get('version_name')),esc(env.get('version_code')),esc(env.get('device')),esc(env.get('system')),esc(data['config'].get('scenario')),esc(data.get('started_at')),esc(art.get('name','未提供')),esc(art.get('sha256','—')),esc(summarize(data)['cache_source']),summarize(data)['cold_n'],summarize(data)['hot_n'])
    charts = ''.join('<section><h2>%s</h2><p class="legend">蓝：改造前　橙：改造后；横轴为监控阶段经过时间，断点表示数据缺失。</p>%s</section>' % (label,chart(before,after,key,label)) for key,label in [('cpu_pct','CPU · 整机容量 %'),('pss_mib','PSS 内存 · MiB'),('cache_mib','应用磁盘缓存 · MiB')])
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>元服务改造影响对比报告</title><style>body{font:15px/1.65 "Microsoft YaHei",sans-serif;background:#f3f6fa;color:#192a43;margin:0}main{max-width:1120px;margin:auto;padding:32px}h1{margin:0}h2{font-size:18px}section,.panel{background:white;padding:22px;margin:18px 0;border-radius:12px}.notice{border-left:5px solid #cf8b2f}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:11px 8px;border-bottom:1px solid #e3e8ee;text-align:right}td:first-child,th:first-child{text-align:left}th{background:#eaf0f8}code{overflow-wrap:anywhere;font-size:11px}.legend{font-size:12px;color:#61718a}svg{width:100%;max-height:270px}button{padding:8px 16px;float:right}@media print{body{background:white}main{padding:0}button{display:none}section{break-inside:avoid}table{font-size:10px}}</style><main><button onclick="window.print()">打印 / 保存 PDF</button><h1>元服务改造影响对比报告</h1><p>''' + esc(before['environment'].get('bundle','')) + '''</p><div class="panel notice"><h2>''' + esc(c['conclusion']) + '''</h2><p>相对增幅关注阈值：''' + fmt(threshold) + '''%。这是观察提示，不是项目验收标准；缺失值不会当作 0。</p><ul>''' + warnings + '''</ul></div>''' + groups + '''<section><h2>指标对比</h2><p>差值＝改造后−改造前。CPU 差值单位为百分点。前值为 0 或负数时不计算变化率。</p><table><thead><tr><th>指标</th><th>单位</th><th>改造前</th><th>改造后</th><th>差值</th><th>变化率 %</th><th>提示</th></tr></thead><tbody>''' + table + '''</tbody></table></section>''' + charts + '''<section><h2>口径与限制</h2><ul><li>启动计时与资源监控分阶段执行，资源采样不与启动测量并行。冷启动到 UIAbility 首帧，热启动到恢复前台，不表示业务内容加载完成。</li><li>CPU 为同应用 UID 下可见进程的 CPU 时间增量占整机容量比例；进程集合变化或不可读的区间留空。内存为同 UID 可见进程的 PSS Total 合计。不同 UID 的隔离渲染进程可能不在范围内。</li><li>CPU 平均为有效采样区间占比的算术均值；PSS 为有效采样点均值，峰值仅为采样峰值。实际采样周期包含命令开销，不能捕捉所有瞬时峰值。</li><li>包体为所选本地 APP/HAP 的文件字节数，不是安装后占用，也不能单凭文件名证明与已安装版本一致。请选择同格式、同构建配置的对应产物。</li><li>自动缓存仅覆盖指定 cache 目录。权限不足或目录不存在显示不可用；人工读数单独标识，不绘制连续曲线。工具不清理缓存。</li><li>两版应使用同设备、同网络、同操作步骤与可比的初始数据、缓存及温度。工具不能自动验证用户操作、网络、温度和全部缓存初始条件一致。</li><li>本报告不验证埋点，不替代登录、导航、音乐播放、稳定性等业务回归。有限样本不能证明整个元服务无影响。</li></ul><p>计时与诊断依据：<a href="https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/aa-tool.md">aa 工具</a> · <a href="https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/dfx/hidumper.md">HiDumper</a></p></section></main></html>'''
    details=''
    for label,data in [('改造前',before),('改造后',after)]:
        cache=data.get('cache',{})
        if cache.get('source')!='system_storage_page':continue
        rows=''
        for p in cache.get('checkpoints',[]):
            rows+='<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                '测试前' if p.get('stage')=='start' else '测试后',esc(p.get('recorded_at','')),
                esc(p.get('raw_text') or '—'),fmt(p.get('mib')),esc(p.get('error') or p.get('status','')))
        sums=summarize(data)
        details+='<section><h2>%s · 系统存储页缓存快照</h2><p>覆盖整个测试：启动测量和资源监控。增长量：约 %s MiB。此模式不会使用人工补录覆盖失败读数。</p><table><tr><th>时点</th><th>时间</th><th>页面原文</th><th>约 MiB</th><th>状态 / 原因</th></tr>%s</table><p>单位换算假设：页面 KB/MB/GB 按 %s 进制，KiB/MiB/GiB 按 1024；页面读数可能被舍入，不能据此推算精确字节增量或认定不存在微小增长。页面统计口径可能与单个 cache 目录不同。</p></section>' % (label,fmt(sums['cache_growth'],True),rows,esc(cache.get('unit_base',1000)))
    names='<p>改造前包名：'+esc(before['environment'].get('bundle',''))+'<br>改造后包名：'+esc(after['environment'].get('bundle',''))+'</p>'
    document=document.replace('<p>'+esc(before['environment'].get('bundle',''))+'</p>',names,1)
    if c['notes']:
        details='<section><h2>两组目标说明</h2><ul>'+''.join('<li>'+esc(n)+'</li>' for n in c['notes'])+'</ul></section>'+details
    mode_details='<section><h2>测试方式与执行情况</h2>'
    for label,d in [('改造前',before),('改造后',after)]:
        mode_details+='<h3>'+label+' · '+esc(TEST_NAMES[test_mode(d['config'])])+'</h3><p>采集状态：'+esc(d.get('state',''))+'；'+esc(d.get('error',''))+'</p>'
        if test_mode(d['config']) in ('monkey','both_monkey'):
            m=d.get('monkey',{});cfg=d['config']
            mode_details+='<p>种子：%s；最多事件：%s；最长：%s 秒；最小事件间隔：%s ms。<br>实际事件：%s；执行耗时（含前台检查与采样）：%s 秒；结束原因：%s。<br>执行器：UITest 逐条随机点击、滑动、返回。原始日志：各组 logs/monkey_events.jsonl 和 logs/monkey_summary.json。次数为确认注入成功的操作数，不代表业务成功；本工具不提供完整崩溃或 ANR 判定。达到时长或停止后不再发起新事件，正在执行的命令可能稍后返回。</p>' % tuple(map(esc,[cfg.get('monkey_seed'),cfg.get('monkey_events'),cfg.get('monkey_seconds'),cfg.get('monkey_interval_ms'),m.get('actual_events') if m.get('actual_events') is not None else '未取得',m.get('elapsed_s','—'),m.get('stop_reason','未执行')]))
    network_details='<section><h2>网络环境与校验记录</h2>'
    for label,d in [('改造前',before),('改造后',after)]:
        net=d.get('network')
        if not net:
            network_details+='<p>'+label+'：未记录网络环境</p>';continue
        mode_label=next((k for k,v in network_env.MODES.items() if v==net['mode']),net['mode'])
        network_details+='<h3>'+label+' · '+esc(mode_label)+'</h3><p>'+esc(net.get('note',''))+'</p>'
        for p in net.get('points',[]):
            network_details+='<p>'+esc(p['stage'])+' · '+esc(p['at'])+'<br>'+esc(network_env.describe(p))+'<br>Wi-Fi：'+esc(p.get('ssid') or '—')+'；RSSI：'+esc(p.get('rssi_dbm') or '—')+' dBm；校验：'+esc(p.get('check'))+' '+esc(p.get('check_note',''))+'</p>'
            if p.get('ping',{}).get('status')!='not_requested' and p.get('ping'):
                network_details+='<p>ICMP 探测：'+esc(json.dumps(p['ping'],ensure_ascii=False))+'</p>'
        if net['mode']=='reqable':
            points=net.get('points',[]);profile=(points[0].get('reqable',{}).get('profile') or {}) if points else {}
            network_details+='<p>Reqable 配置预设（非实测）：'+esc(json.dumps(profile,ensure_ascii=False))+'。代理覆盖：'+esc(net.get('proxy_path_confirmation','未确认'))+'。仅作用于经过 Reqable 且命中域名规则的请求；模拟 4G/5G 不代表手机真实切换制式。丢包参数是 Reqable 异常模拟，不等于底层 IP 丢包率。</p>'
        if net['mode']=='builtin':
            points=net.get('points',[]);simulation=points[-1].get('simulation') if points else None
            sim=simulation or {};stats=sim.get('stats',{})
            network_details+='<p>内置预设：'+esc(sim.get('profile','未取得'))+'；上传 / 下载上限：'+esc(sim.get('upload_kbps','—'))+' / '+esc(sim.get('download_kbps','—'))+' kbps；新连接延迟：'+esc(sim.get('connection_delay_ms','—'))+' ms；连接失败模拟：'+esc(sim.get('connection_failure_pct','—'))+'%。</p>'
            network_details+='<table><tr><th>代理连接尝试</th><th>连接成功</th><th>模拟失败</th><th>上游/协议错误</th><th>上传字节</th><th>下载字节</th></tr><tr>'+''.join('<td>'+esc(stats.get(key,'—'))+'</td>' for key in ('connections','connected','simulated_failures','upstream_errors','upload_bytes','download_bytes'))+'</tr></table>'
            network_details+='<p>以上是设备经过代理的合计流量，包含其他应用；不能按目标包归因。</p>'
            network_details+='<p>3G/4G/5G 为代理模拟，实际出口为电脑网络；不会改变手机通信制式。失败率按新连接模拟，不是 IP 丢包率。</p>'
        restore=d.get('network_restore')
        if restore:network_details+='<p>环境恢复：'+esc({'restored':'已恢复原设置','restore_failed':'恢复失败，请点击恢复上次环境'}.get(restore.get('state'),restore.get('state')))+' '+esc(restore.get('error',''))+'</p>'
    network_details+='</section>'
    details=mode_details+'</section>'+network_details+details
    document=document.replace('<section><h2>口径与限制</h2>',details+'<section><h2>口径与限制</h2>')
    document=document.replace('自动缓存仅覆盖指定 cache 目录。','目录模式仅覆盖指定 cache 目录；系统页面模式采用系统缓存分类。')
    (root/'report.html').write_text(document,encoding='utf-8')
    return root
