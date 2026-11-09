import copy
import importlib.util
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import impact_engine as e
import harmony_startup as core


def fixture(role):
    data={'schema':e.SCHEMA,'role':role,'label':role,'state':'complete','started_at':'test',
          'config':dict(scenario='same',duration=60,interval=5,count=5,settle=3,monitor_only=False),
          'environment':dict(device='D',system='S',bundle='com.example',module='entry',ability='EntryAbility',installed_hash=role,version_name='1',version_code=1),
          'artifact':dict(status='ok',mib=10,format='.app',name='test.app',sha256=role),
          'cache':dict(path='/cache',manual_start_mib=None,manual_end_mib=None),
          'startup':[dict(requested_mode=mode,status='ok',total_time_ms=100+i) for mode in ('cold','hot') for i in range(5)],
          'resources':[dict(elapsed_s=i*5,cpu_pct=1+i/10,pss_mib=100+i,cache_mib=10+i) for i in range(6)]}
    return data


class Tests(unittest.TestCase):
    def test_uid_aggregation_and_partial_memory_is_missing(self):
        class Fake:
            tick=10; total=100; broken=False
            def shell(self,*args,**kwargs):
                if args[0]=='ps':return 'PID UID NAME\n1 100 app\n2 100 app:worker\n3 200 unrelated'
                if args[0]=='du':return '1024 /cache'
                if args[:2]==('cat','/proc/stat'):return 'cpu %d 0 0 0'%self.total
                if args[0]=='hidumper':
                    if self.broken and args[-1]=='2':return 'Permission denied'
                    return 'Total 2048 0 0'
                if args[1].endswith('/status'):return 'VmRSS: 4096 kB'
                fields=['S']+['0']*25
                fields[11]=str(self.tick);fields[19]='900'
                return '1 (app) '+' '.join(fields)
        fake=Fake();m=e.ResourceMonitor(fake,'app',100,'/cache')
        first=m.sample(0,[])
        self.assertEqual(first['pids'],'1,2');self.assertEqual(first['pss_mib'],4)
        self.assertIsNone(first['cpu_pct'])
        fake.tick=12;fake.total=200;fake.broken=True
        second=m.sample(5,[])
        self.assertEqual(second['cpu_pct'],4);self.assertIsNone(second['pss_mib'])
    def test_malformed_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.json'
            for content in ([],{'schema':e.SCHEMA,'startup':[],'resources':[]}):
                p.write_text(json.dumps(content),encoding='utf-8')
                with self.assertRaises(ValueError):e.load_result(p)
    def test_proc_stat_with_spaces_in_name(self):
        fields=['S']+['0']*25
        fields[11]='10';fields[12]='7';fields[19]='900'
        self.assertEqual(e.parse_stat('123 (name with ) space) '+' '.join(fields)),(17,900))
    def test_guest_not_double_counted(self):
        self.assertEqual(e.parse_total_cpu('cpu  10 20 30 40 5 6 7 8 100 200\ncpu0 1 2'),126)
    def test_cpu_process_identity_and_rollback(self):
        a=dict(total=100,ticks=10,identities=[(1,100)])
        b=dict(total=200,ticks=12,identities=[(1,100)])
        self.assertEqual(e.cpu_percent(a,b),2)
        b['identities']=[(1,200)]
        self.assertIsNone(e.cpu_percent(a,b))
        self.assertIsNone(e.cpu_percent(None,b))
    def test_memory_and_cache_denied_not_zero(self):
        self.assertEqual(e.parse_pss('Total 2048 0 0 0'),2)
        self.assertEqual(e.parse_du('1024 /cache\n'),1)
        for value in ['du: /cache: Permission denied','0 /cache\ndu: Permission denied','No such file','']:
            with self.assertRaises(ValueError):e.parse_du(value)
    def test_missing_remains_missing(self):
        a,b=fixture('before'),fixture('after');b['artifact']={'status':'missing'}
        b['resources']=[dict(elapsed_s=0,cpu_pct=None,pss_mib=None,cache_mib=None)]
        c=e.comparison(a,b)
        self.assertTrue(any(r['status']=='缺少数据' for r in c['rows']))
        self.assertEqual(c['conclusion'],'需补充验证')
        self.assertIsNone(c['after_summary']['cache_growth'])
    def test_same_binary_is_warning(self):
        a,b=fixture('before'),fixture('after');b['environment']['installed_hash']='before'
        self.assertTrue(any('指纹相同' in w for w in e.comparison(a,b)['warnings']))
    def test_format_mismatch_not_pass(self):
        a,b=fixture('before'),fixture('after');b['artifact']['format']='.hap'
        row=next(r for r in e.comparison(a,b)['rows'] if r['key']=='package_mib')
        self.assertEqual(row['status'],'口径不同')
    def test_zero_baseline_no_division(self):
        a,b=fixture('before'),fixture('after');a['artifact']['mib']=0
        row=next(r for r in e.comparison(a,b)['rows'] if r['key']=='package_mib')
        self.assertIsNone(row['percent']);self.assertEqual(row['status'],'需关注')
    def test_manual_cache_is_distinct(self):
        a,b=fixture('before'),fixture('after');b['resources']=[dict(r,cache_mib=None) for r in b['resources']]
        b['cache'].update(manual_start_mib=20,manual_end_mib=23)
        self.assertEqual(e.summarize(b)['cache_growth'],3)
        self.assertEqual(next(r for r in e.comparison(a,b)['rows'] if r['key']=='cache_growth')['status'],'口径不同')
    def test_single_cache_sample_not_growth_zero(self):
        d=fixture('before');d['resources']=d['resources'][:1]
        self.assertIsNone(e.summarize(d)['cache_growth'])
    def test_attention_threshold(self):
        a,b=fixture('before'),fixture('after');b['artifact']['mib']=12
        self.assertEqual(e.comparison(a,b)['conclusion'],'存在需关注的指标增幅')
    def test_report_escapes_text_and_preserves_snapshots(self):
        a,b=fixture('before'),fixture('after');a['label']='<script>alert(1)</script>'
        with tempfile.TemporaryDirectory() as folder:
            path=e.report(a,b,folder)
            text=(path/'report.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)</script>',text)
            self.assertIn('&lt;script&gt;',text)
            self.assertEqual(json.loads((path/'before_snapshot.json').read_text())['role'],'before')
            self.assertTrue((path/'comparison.csv').exists())
    def test_artifact_bytes_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'中文 文件.app';p.write_bytes(b'x'*2048)
            info=e.artifact_info(str(p),threading.Event())
            self.assertEqual(info['bytes'],2048);self.assertEqual(len(info['sha256']),64)
    def test_cancel_preserves_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            config=dict(bundle='com.example.service',out=folder,role='before',label='test',hdc='',target='')
            cancel=threading.Event();cancel.set();events=queue.Queue()
            with patch.object(core,'find_hdc',return_value='fake'):
                e.collect(config,events,cancel)
            results=list(Path(folder).rglob('result.json'))
            self.assertEqual(len(results),1)
            self.assertEqual(json.loads(results[0].read_text(encoding='utf-8'))['state'],'interrupted')


if __name__=='__main__':unittest.main(verbosity=2)
