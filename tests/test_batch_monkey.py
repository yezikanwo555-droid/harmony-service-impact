import copy,json,queue,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import comparison_job as job
import impact_engine as e
import monkey_runner as m

def cfg(role,out):
    return dict(fixture(role)['config'],role=role,label=role,bundle='com.example.'+role,out=out,hdc='',target='',test_mode='both')

class FakeHdc:
    calls=[]
    def __init__(self,*args,**kw):self.target='test_device'
    def select_device(self):pass
    def shell(self,*args):
        self.calls.append(args)
        return json.dumps({'versionName':'1','versionCode':1}) if args[0]=='bm' else 'ok'

class Tests(unittest.TestCase):
    def test_monkey_args_are_bounded_and_single_bundle(self):
        c=dict(bundle='com.example.test',monkey_seconds=10,monkey_events=30,monkey_interval_ms=100,monkey_seed=42)
        self.assertEqual(m.validate(c)['monkey_events'],30)
        for key in ('monkey_seconds','monkey_events','monkey_interval_ms'):
            with self.assertRaises(ValueError):m.validate(dict(c,**{key:0}))
    def test_actual_count_not_planned_count_and_false_success(self):
        c=dict(bundle='com.example.test',monkey_seconds=10,monkey_events=20,monkey_interval_ms=100,monkey_seed=42)
        for actual,elapsed,cancelled,expected in [(0,11,False,'error'),(20,8,False,'complete'),(2,8,False,'error'),(2,11,False,'complete'),(1,2,True,'interrupted')]:
            with tempfile.TemporaryDirectory() as out:
                s=m.MonkeySession(FakeHdc(),c,out,threading.Event());s.actual=actual;s.started=time.monotonic()-elapsed
                result=s.finish(cancelled);self.assertEqual(result['status'],expected);self.assertEqual(result['actual_events'],actual)
    def test_unselected_startup_not_missing(self):
        a,b=fixture('before'),fixture('after')
        for d in (a,b):
            d['config'].update(test_mode='monkey',monkey_seconds=10,monkey_events=20,monkey_seed=42,monkey_interval_ms=100)
            d['startup']=[];d['monkey']=dict(status='complete',actual_events=20)
        result=e.comparison(a,b)
        self.assertTrue(all(r['status']=='未选择此测试' for r in result['rows'][:4]))
        self.assertFalse(any('启动方式有效样本' in w for w in result['warnings']))
    def test_cold_only_keeps_hot_unselected(self):
        a,b=fixture('before'),fixture('after')
        for d in (a,b):d['config']['test_mode']='cold';d['startup']=[r for r in d['startup'] if r['requested_mode']=='cold']
        self.assertEqual(e.comparison(a,b)['rows'][2]['status'],'未选择此测试')
    def run_fake_pair(self,out,fail=False,cancelled=False):
        q=queue.Queue();cancel=threading.Event();calls=[]
        def collect(c,events,stop):
            calls.append(c['role']);d=fixture(c['role']);d['config']=c;d['environment']['bundle']=c['bundle'];d['error']=''
            if fail:d['state']='error';d['error']='simulated failure'
            if cancelled:d['state']='interrupted';cancel.set()
            payload={'out':out+'/'+c['role'],'result':d};events.put(('done',payload));return payload
        with patch.object(job.core,'find_hdc',return_value='fake'),patch.object(job,'DesktopHdc',FakeHdc),patch.object(job.core,'resolve_entry',return_value=('entry','EntryAbility','proc')),patch.object(job.e,'collect',side_effect=collect):
            job.run_pair([cfg('before',out),cfg('after',out)],q,cancel)
        all_events=[]
        while not q.empty():all_events.append(q.get())
        return calls,all_events[-1][1]
    def test_batch_runs_in_order_and_generates_fresh_report(self):
        with tempfile.TemporaryDirectory() as out:
            calls,result=self.run_fake_pair(out)
            self.assertEqual(calls,['before','after']);self.assertEqual(result['state'],'complete')
            self.assertTrue(Path(result['report']).is_file())
            self.assertEqual(json.loads((Path(result['out'])/'batch.json').read_text(encoding='utf-8'))['state'],'complete')
    def test_failed_group_stops_queue_and_marks_skipped(self):
        with tempfile.TemporaryDirectory() as out:
            calls,result=self.run_fake_pair(out,fail=True)
            self.assertEqual(calls,['before']);self.assertEqual(result['state'],'error')
            self.assertEqual(result['results']['after']['state'],'skipped');self.assertTrue(Path(result['report']).exists())
    def test_cancel_stops_queue_and_preserves_partial_report(self):
        with tempfile.TemporaryDirectory() as out:
            calls,result=self.run_fake_pair(out,cancelled=True)
            self.assertEqual(calls,['before']);self.assertEqual(result['state'],'interrupted')
            self.assertEqual(result['results']['after']['state'],'skipped')
    def test_same_package_does_not_run_fake_comparison(self):
        with tempfile.TemporaryDirectory() as out:
            a,b=cfg('before',out),cfg('after',out);b['bundle']=a['bundle'];q=queue.Queue()
            with patch.object(job.e,'collect') as collect:job.run_pair([a,b],q,threading.Event());collect.assert_not_called()
            result=q.get()[1];self.assertEqual(result['state'],'error');self.assertIsNone(result['report'])
    def test_auto_network_applied_once_restored_before_report(self):
        import network_session
        order=[]
        class Session:
            id='test-session'
            def __init__(self,h,c,*args):self.config=c
            def begin(self):order.append('apply');self.config.update(_network_session_id=self.id,_network_applied=True)
            def finish(self):order.append('restore');return {'state':'restored'}
        def collect(c,events,stop):
            order.append(c['role']);self.assertEqual(c['_network_session_id'],'test-session')
            d=fixture(c['role']);d['config']=c;d['error']='';path=Path(c['out'])/c['role'];path.mkdir()
            return dict(result=d,out=str(path))
        real_report=e.report
        def report(*args):
            order.append('report');self.assertEqual(args[0]['network_restore']['state'],'restored');return real_report(*args)
        with tempfile.TemporaryDirectory() as out:
            configs=[dict(cfg(role,out),network_auto=True,network_mode='builtin',network_reqable_preset=5) for role in ('before','after')]
            with patch.object(job.core,'find_hdc',return_value='fake'),patch.object(job,'DesktopHdc',FakeHdc),patch.object(job.core,'resolve_entry',return_value=('entry','EntryAbility','proc')),patch.object(network_session,'Session',Session),patch.object(e,'collect',side_effect=collect),patch.object(e,'report',side_effect=report):
                job.run_pair(configs,queue.Queue(),threading.Event())
            self.assertEqual(order,['apply','before','after','restore','report'])

if __name__=='__main__':unittest.main(verbosity=2)
