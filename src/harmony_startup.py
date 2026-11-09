#!/usr/bin/env python3
"""HarmonyOS UIAbility startup measurement using aa start -W (API 20+).

Python 3.9+, standard library only. Run --help for batch arguments.
TotalTime is device-reported; host/USB command duration is never a substitute.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time

DEFAULT_BUNDLE = ''
DOC_URL = 'https://github.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/aa-tool.md'


class MeasurementError(RuntimeError):
    pass


def timestamp():
    return dt.datetime.now().astimezone().isoformat(timespec='milliseconds')


def pause(args, seconds):
    cancel = getattr(args, 'cancel', None)
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise KeyboardInterrupt


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:$-]*', value):
        raise argparse.ArgumentTypeError('名称只允许字母、数字、下划线、点、冒号、美元符号和连字符')
    return value


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('必须是正整数')
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('必须是大于 0 的有限数字')
    return number


def find_hdc(explicit=None):
    if explicit:
        candidate = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if candidate:
            return str(candidate)
        raise MeasurementError('找不到指定的 hdc：' + explicit)
    found = shutil.which('hdc')
    if found:
        return found
    roots = [os.environ.get(k) for k in ('DEVECO_SDK_HOME', 'OHOS_SDK_HOME', 'HOS_SDK_HOME')]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(str(Path(local) / 'OpenHarmony' / 'Sdk'))
    candidates = []
    for root in filter(None, roots):
        base = Path(root)
        for pattern in ('toolchains/hdc.exe', '*/toolchains/hdc.exe', '*/openharmony/toolchains/hdc.exe'):
            candidates.extend(base.glob(pattern))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise MeasurementError('未找到 hdc。请将 SDK toolchains 加入 PATH，或使用 --hdc 指定 hdc.exe。')


class Hdc:
    def __init__(self, executable, target=None, timeout=30):
        self.executable, self.target, self.timeout = executable, target, timeout

    def run(self, args, log=None, allow_nonzero=False):
        command = [self.executable]
        if self.target:
            command += ['-t', self.target]
        command += list(args)
        started = timestamp()
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=self.timeout, shell=False)
            output = proc.stdout.decode('utf-8', errors='replace')
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b'').decode('utf-8', errors='replace')
            if log is not None:
                log.append({'at': started, 'command': command, 'timeout': True, 'output': output})
            raise MeasurementError('命令超时，未将等待时间记为启动耗时：' + ' '.join(args)) from exc
        if log is not None:
            log.append({'at': started, 'command': command, 'returncode': proc.returncode, 'output': output})
        if proc.returncode and not allow_nonzero:
            raise MeasurementError('hdc 命令失败：' + output.strip()[:600])
        if re.search(r'\[Fail\]|device not found|No connected devices|Connect server failed|Not match target', output, re.I):
            raise MeasurementError('设备连接失败：' + output.strip()[:600])
        return output

    def shell(self, *args, log=None, allow_nonzero=False):
        # Quote for the device POSIX shell; do not invoke a host shell.
        return self.run(['shell', ' '.join(shlex.quote(str(a)) for a in args)], log, allow_nonzero)

    def select_device(self):
        requested = self.target
        self.target = None
        try:
            output = self.run(['list', 'targets'])
        finally:
            self.target = requested
        devices = [line.strip() for line in output.splitlines()
                   if line.strip() and not line.strip().startswith('[')]
        if not devices:
            raise MeasurementError('没有连接设备。请开启 USB 调试并在手机上授权。')
        if requested and requested not in devices:
            raise MeasurementError('指定设备未连接：' + requested)
        if not requested and len(devices) != 1:
            raise MeasurementError('连接了多台设备，请用 --target 选择：' + ', '.join(devices))
        self.target = requested or devices[0]


def bundle_json(text):
    start = text.find('{')
    if start < 0:
        raise MeasurementError('无法读取包信息，请确认包名及当前用户下的安装状态：' + text.strip()[:300])
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value
    except (ValueError, TypeError) as exc:
        raise MeasurementError('包信息不是可识别的 JSON。') from exc


def resolve_entry(info, ability=None, module=None):
    modules = info.get('hapModuleInfos', [])
    entry_module = info.get('entryModuleName')
    if module:
        modules = [m for m in modules if m.get('moduleName', m.get('name')) == module]
    elif not ability:
        entries = [m for m in modules if m.get('moduleName', m.get('name')) == entry_module]
        if entries:
            modules = entries
    candidates = []
    for mod in modules:
        mod_name = mod.get('moduleName') or mod.get('name')
        name = ability or mod.get('mainAbility') or mod.get('mainElementName') or mod.get('mainElement')
        for item in mod.get('abilityInfos', []):
            if item.get('name') != name:
                continue
            if item.get('exported') is False or item.get('visible') is False:
                continue
            candidates.append((mod_name, name, item.get('process') or mod.get('process') or
                               info.get('applicationInfo', {}).get('process') or info.get('name')))
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise MeasurementError('无法唯一确定启动 UIAbility。请用 --ability 和 --module 指定入口，'
                               '并确认该入口允许外部启动。候选：' + repr(unique))
    return unique[0]


def parse_start(output):
    if re.search(r'\berror\s*:|failed to|unknown option|invalid option|not support', output, re.I):
        raise MeasurementError('启动失败：' + output.strip()[:700])
    fields = {}
    for key in ('StartMode', 'BundleName', 'AbilityName', 'ModuleName', 'TotalTime', 'WaitTime'):
        values = re.findall(r'^\s*' + key + r'\s*:\s*(.*?)\s*$', output, re.M)
        if len(values) > 1:
            raise MeasurementError('启动返回包含重复字段：' + key)
        if values:
            fields[key] = values[0]
    if fields.get('StartMode', '').lower() not in ('cold', 'hot'):
        raise MeasurementError('缺少有效 StartMode；设备可能不支持 aa start -W。')
    for key in ('TotalTime', 'WaitTime'):
        raw = fields.get(key, '')
        if key == 'WaitTime' and not raw:
            fields[key] = None
            continue
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(?:ms)?', raw, re.I)
        if not match:
            raise MeasurementError('缺少有效的系统计时 ' + key + '，本轮不计入统计。')
        value = float(match.group(1))
        if not math.isfinite(value):
            raise MeasurementError('系统计时超出有效范围：' + key)
        fields[key] = value
    fields['StartMode'] = fields['StartMode'].lower()
    return fields


def pids(hdc, process_name, log):
    text = hdc.shell('pidof', process_name, log=log, allow_nonzero=True).strip()
    if not text:
        return set()
    if not re.fullmatch(r'\d+(?:\s+\d+)*', text):
        raise MeasurementError('无法校验目标进程：' + text[:300])
    return set(text.split())


def start(hdc, args, log):
    text = hdc.shell('aa', 'start', '-b', args.bundle, '-a', args.ability,
                     '-m', args.module, '-W', log=log)
    result = parse_start(text)
    if result.get('BundleName') != args.bundle or result.get('AbilityName') != args.ability:
        raise MeasurementError('系统返回的启动目标与指定包名/Ability 不一致。')
    if result.get('ModuleName') and result['ModuleName'] != args.module:
        raise MeasurementError('系统返回的启动模块不一致。')
    return result


def home(hdc, log):
    text = hdc.shell('uitest', 'uiInput', 'keyEvent', 'Home', log=log)
    diagnostic = re.sub(r'\bNo Error\b', '', text, flags=re.I)
    if re.search(r'error|failed|usage|not found|missing parameter', diagnostic, re.I):
        raise MeasurementError('返回桌面失败，不能确认应用已进入后台：' + text[:300])


FIELDS = ['recorded_at', 'requested_mode', 'actual_mode', 'round', 'status', 'total_time_ms',
          'wait_time_ms', 'pid_before', 'pid_after', 'bundle', 'module', 'ability', 'error', 'raw_log']


def measure(hdc, args, mode, index, out):
    log = []
    row = {key: '' for key in FIELDS}
    row.update(recorded_at=timestamp(), requested_mode=mode, round=index, status='error',
               bundle=args.bundle, module=args.module, ability=args.ability)
    path = out / 'logs' / ('%s_%03d.json' % (mode, index))
    row['raw_log'] = str(path.relative_to(out))
    interrupted = False
    try:
        # Both modes begin from the desktop, never Back (which may destroy an Ability).
        home(hdc, log)
        if mode == 'cold':
            text = hdc.shell('aa', 'force-stop', args.bundle, log=log)
            if re.search(r'error|failed', text, re.I):
                raise MeasurementError('强制停止失败：' + text[:300])
            pause(args, args.settle)
            before = pids(hdc, args.process, log)
            if before:
                raise MeasurementError('强制停止后目标进程仍存在，不作为冷启动。')
        else:
            # The preparatory launch is explicitly excluded from statistics.
            prepared = start(hdc, args, log)
            log.append({'note': '热启动预备启动，不计入统计', 'result': prepared})
            pause(args, args.settle)
            alive = pids(hdc, args.process, log)
            if len(alive) != 1:
                raise MeasurementError('热启动准备后无法唯一确定目标主进程。')
            home(hdc, log)
            pause(args, args.background)
            before = pids(hdc, args.process, log)
            if before != alive:
                raise MeasurementError('应用退后台后进程消失或变化，本轮不作为热启动。')
        row['pid_before'] = ','.join(sorted(before))
        row['recorded_at'] = timestamp()
        result = start(hdc, args, log)
        after = pids(hdc, args.process, log)
        row.update(actual_mode=result['StartMode'], total_time_ms=result['TotalTime'],
                   wait_time_ms=result['WaitTime'], pid_after=','.join(sorted(after)))
        if result['StartMode'] != mode:
            row['status'] = 'type_mismatch'
            row['error'] = '系统实际启动类型与计划类型不一致，不计入汇总。'
        elif len(after) != 1 or (mode == 'hot' and after != before):
            row['status'] = 'process_mismatch'
            row['error'] = '启动后进程无法确认或热启动期间进程变化，不计入汇总。'
        else:
            row['status'] = 'ok'
        pause(args, args.settle)
    except KeyboardInterrupt:
        interrupted = True
        row['status'], row['error'] = 'interrupted', '用户中断，本轮不计入汇总。'
    except (MeasurementError, OSError) as exc:
        row['error'] = str(exc)
    finally:
        path.write_text(json.dumps({'sample': row, 'commands': log}, ensure_ascii=False, indent=2), encoding='utf-8')
    return row, interrupted


def summarize(rows):
    result = {}
    for mode in ('cold', 'hot'):
        selected = [r for r in rows if r['requested_mode'] == mode]
        if not selected:
            continue
        values = sorted(float(r['total_time_ms']) for r in selected if r['status'] == 'ok')
        summary = {'attempted': len(selected), 'valid': len(values), 'invalid': len(selected) - len(values)}
        for key, value in {
            'mean_ms': statistics.mean(values) if values else None,
            'median_ms': statistics.median(values) if values else None,
            'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None,
            'min_ms': min(values) if values else None,
            'max_ms': max(values) if values else None,
        }.items():
            summary[key] = round(value, 3) if value is not None else None
        result[mode] = summary
    return result


def save_summary(out, rows, args, interrupted=False):
    summaries = summarize(rows)
    data = {'bundle': args.bundle, 'device': args.target, 'requested_count_per_mode': args.count,
            'interrupted': interrupted, 'metric': 'aa start -W TotalTime',
            'cold_endpoint': 'UIAbility first frame drawn', 'hot_endpoint': 'UIAbility foreground state',
            'p95_method': 'nearest rank', 'results': summaries, 'source': DOC_URL}
    (out / 'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['HarmonyOS 冷热启动测量', '包名：' + args.bundle,
             '口径：系统 aa start -W 的 TotalTime，单位 ms。',
             '冷启动终点为 UIAbility 首帧绘制完成；热启动终点为 UIAbility 恢复前台。',
             '不代表网页或业务内容加载完成；WaitTime 仅作诊断。',
             'P95 使用最近秩法。小样本仅供排查，不代表稳定性能基线。', '']
    for mode, stat in summaries.items():
        label = '冷启动' if mode == 'cold' else '热启动'
        lines.append('%s：有效 %d/%d，异常 %d' % (label, stat['valid'], stat['attempted'], stat['invalid']))
        if stat['valid']:
            lines.append('平均 {mean_ms} ms，中位数 {median_ms} ms，P95 {p95_ms} ms，最小 {min_ms} ms，最大 {max_ms} ms'.format(**stat))
        else:
            lines.append('无有效样本，未计算耗时统计。')
    if interrupted:
        lines.append('本次运行已中断，已保留完成的记录。')
    text = '\n'.join(lines)
    (out / 'summary.txt').write_text(text, encoding='utf-8-sig')
    return text


def parser():
    p = argparse.ArgumentParser(description='HarmonyOS 冷热启动计时（hdc / aa start -W，API 20+）')
    p.add_argument('--bundle', type=identifier, default=DEFAULT_BUNDLE, help='待测应用包名，必须提供')
    p.add_argument('--ability', type=identifier, help='默认从已安装包信息识别')
    p.add_argument('--module', type=identifier, help='启动模块，默认从包信息识别')
    p.add_argument('--process', type=identifier, help='主进程名；默认从包信息识别')
    p.add_argument('--count', type=positive_int, default=10, help='每种启动的测量次数，默认 10')
    p.add_argument('--mode', choices=['both', 'cold', 'hot'], default='both')
    p.add_argument('--settle', type=positive_float, default=3, help='停止后及启动后稳定等待秒数，默认 3')
    p.add_argument('--background', type=positive_float, default=2, help='热启动的后台停留秒数，默认 2')
    p.add_argument('--timeout', type=positive_float, default=30, help='单条命令超时秒数，默认 30')
    p.add_argument('--target', help='hdc 设备标识；多设备时必填')
    p.add_argument('--hdc', help='hdc.exe 完整路径，未提供时自动寻找')
    p.add_argument('--out', type=Path, default=Path(__file__).resolve().parent / 'results', help='结果父目录')
    p.add_argument('--inspect', action='store_true', help='只检查连接、入口和 -W 支持，不启动或停止应用')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    if not argv and sys.stdin.isatty():
        print('HarmonyOS 冷热启动测量；请保持设备解锁，测试期间不要操作手机。')
        bundle = input('应用包名（必填）：').strip()
        count = input('每种启动测试次数（回车 10）：').strip() or '10'
        argv = ['--bundle', bundle, '--count', count]
    args = p.parse_args(argv)
    if not args.bundle:
        p.error('请通过 --bundle 提供应用包名。')
    try:
        hdc = Hdc(find_hdc(args.hdc), args.target, args.timeout)
        hdc.select_device()
        args.target = hdc.target
        if not re.search(r'(?<!\w)-W(?!\w)', hdc.shell('aa', 'start', '-h')):
            raise MeasurementError('设备不支持 aa start -W，需要 API 20+；不会用命令耗时替代。')
        info = bundle_json(hdc.shell('bm', 'dump', '-n', args.bundle))
        module, ability, process = resolve_entry(info, args.ability, args.module)
        args.module, args.ability = identifier(module), identifier(ability)
        args.process = identifier(args.process or process or args.bundle)
        print('设备：%s\n包名：%s\n入口：%s / %s\n主进程：%s' %
              (args.target, args.bundle, args.module, args.ability, args.process), flush=True)
        if args.inspect:
            print('预检通过；未启动或停止应用。')
            return 0
        out = args.out.resolve() / (dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + args.bundle)
        (out / 'logs').mkdir(parents=True, exist_ok=False)
        metadata = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
        metadata.update(hdc=hdc.executable, app_version=info.get('versionName'),
                        started_at=timestamp(), python=sys.version, timing_source=DOC_URL)
        (out / 'run_config.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        print('结果目录：' + str(out), flush=True)
        print('冷启动将停止目标进程；保留应用数据和缓存。热启动使用 Home 退后台后恢复。', flush=True)
        modes = ['cold', 'hot'] if args.mode == 'both' else [args.mode]
        rows, interrupted = [], False
        with (out / 'samples.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            try:
                for mode in modes:
                    for index in range(1, args.count + 1):
                        row, interrupted = measure(hdc, args, mode, index, out)
                        rows.append(row)
                        writer.writerow(row)
                        f.flush()
                        save_summary(out, rows, args, interrupted)
                        label = '冷启动' if mode == 'cold' else '热启动'
                        message = ('%s ms' % row['total_time_ms']) if row['status'] == 'ok' else row['error']
                        print('[%s %d/%d] %s: %s' % (label, index, args.count, row['status'], message), flush=True)
                        if interrupted:
                            break
                    if interrupted:
                        break
            except KeyboardInterrupt:
                interrupted = True
            finally:
                summary_text = save_summary(out, rows, args, interrupted)
        print('\n' + summary_text + '\n\n结果已保存：' + str(out), flush=True)
        if interrupted:
            return 130
        return 0 if rows and all(r['status'] == 'ok' for r in rows) else 2
    except (MeasurementError, OSError, argparse.ArgumentTypeError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已取消。', file=sys.stderr)
        sys.exit(130)
