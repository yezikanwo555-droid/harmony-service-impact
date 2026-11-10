import unittest
from unittest.mock import Mock
import traffic_monitor as t
from test_impact import fixture
import impact_engine as e

class Tests(unittest.TestCase):
    def tracker(self,values):
        tracker=t.Tracker(None,200001)
        tracker.counters=Mock(side_effect=[dict(rx=rx,tx=tx,at=at,recorded_at=str(at),uid=200001,pid=10) if not isinstance(rx,Exception) else rx for rx,tx,at in values])
        return tracker
    def test_parse_requires_one_application_counter_pair(self):
        self.assertEqual(t.parse('Received Bytes:123\nSent Bytes:456\n'),dict(rx=123,tx=456))
        for text in ('Received Bytes:0','Received Bytes:0\nSent Bytes:0\nerror','Received Bytes:0\nSent Bytes:0\nSent Bytes:1'):
            with self.assertRaises(ValueError):t.parse(text)
    def test_consumption_subtracts_history_and_rate_uses_actual_interval(self):
        tracker=self.tracker([(100000,50000,10),(102048,51024,12),(104096,52048,14)])
        first=tracker.sample('before_tests',[]);second=tracker.sample('resource',[]);tracker.sample('end',[])
        self.assertEqual(first['traffic_total_bytes'],0);self.assertIsNone(first['upload_kib_s'])
        self.assertEqual(second['upload_kib_s'],.5);self.assertEqual(second['download_kib_s'],1)
        summary=tracker.summary();self.assertEqual(summary['total_bytes'],6144);self.assertEqual(summary['status'],'complete')
    def test_missing_initial_baseline_is_partial(self):
        tracker=self.tracker([(ValueError('not running'),0,0),(1000,2000,2),(1200,2100,4)])
        tracker.sample('before_tests',[]);tracker.sample('resources_start',[]);tracker.sample('end',[])
        self.assertEqual(tracker.summary()['status'],'partial');self.assertEqual(tracker.summary()['total_bytes'],300)
    def test_reset_invalidates_total_instead_of_negative_or_zero(self):
        tracker=self.tracker([(100,200,0),(5,10,1),(15,30,2)])
        tracker.sample('before_tests',[]);tracker.sample('resource',[]);tracker.sample('end',[])
        self.assertIsNone(tracker.summary()['total_bytes']);self.assertTrue(tracker.summary()['counter_reset'])
    def test_missing_end_keeps_partial_lower_bound(self):
        tracker=self.tracker([(100,200,0),(110,220,1),(ValueError('disconnected'),0,2)])
        tracker.sample('before_tests',[]);tracker.sample('resource',[]);tracker.sample('end',[])
        self.assertEqual(tracker.summary()['status'],'partial');self.assertEqual(tracker.summary()['total_bytes'],30)
    def test_shared_uid_read_once_not_sum_per_pid(self):
        class Hdc:
            calls=[]
            def shell(self,*args,**kwargs):
                self.calls.append(args)
                if args[0]=='ps':return '10 200001 com.example\n11 200001 com.example:worker'
                if args[0]=='cat':return 'Uid:\t200001 200001 200001 200001'
                return 'Received Bytes:100\nSent Bytes:200'
        h=Hdc();tracker=t.Tracker(h,200001);tracker.sample('before_tests',[])
        self.assertEqual([x for x in h.calls if x[0]=='hidumper'],[('hidumper','--net','10')])
    def test_incomplete_coverage_is_not_reported_as_comparable(self):
        a,b=fixture('before'),fixture('after')
        for d,status in ((a,'complete'),(b,'partial')):d['traffic']=dict(status=status,total_bytes=1048576,upload_bytes=100,download_bytes=1048476)
        rows=e.comparison(a,b)['rows'];row=next(r for r in rows if r['key']=='traffic_total_mib')
        self.assertEqual(row['status'],'口径不同')
