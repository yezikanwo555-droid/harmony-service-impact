import datetime as dt
import io,json,tempfile,time,unittest,queue,threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch,Mock
from test_impact import fixture
import monkey_diagnostics as d
import impact_engine as engine

BUNDLE='com.example.target';UID=200001
ZONE=dt.timezone(dt.timedelta(hours=-5))
WHEN=dt.datetime(2026,11,10,10,0,1,tzinfo=ZONE).timestamp()
NAME='jscrash-com.example.target-200001-20261110100001000.log'
TEXT='Timestamp:2026-11-10 10:00:01.000\nModule name:com.example.target\nPid:123\nUid:200001\nReason:Error: failure <script>\n'

class Tests(unittest.TestCase):
    def test_device_clock_and_timezone_not_host_timezone(self):
        epoch,zone=d.parse_clock('1794322801.125 -0500\n')
        self.assertEqual(zone,ZONE);self.assertEqual(epoch,1794322801.125)
        self.assertEqual(d.stamp_time('20261110100001000',zone),WHEN)
        self.assertEqual(d.stamp_time(str(int(WHEN*1000)),zone),WHEN)
    def test_fault_listing_supports_empty_but_denial_is_not_empty(self):
        self.assertEqual(d.parse_listing('Fault log list:\n******\n******'),set())
        for text in ('Permission denied','[Fail] bad','unexpected'):
            with self.assertRaises(ValueError):d.parse_listing(text)
        self.assertEqual(d.parse_listing('Fault log list:\n'+NAME),{NAME})
    def assess(self,name=NAME,text=TEXT,start=WHEN-1,end=WHEN+1):
        return d.assess_fault(name,text,BUNDLE,UID,BUNDLE,start,end,ZONE)
    def test_confirmed_requires_identity_time_and_typed_record(self):
        value=self.assess();self.assertTrue(value['confirmed']);self.assertEqual(value['kind'],'jscrash')
        for kind in ('cppcrash','appfreeze'):
            self.assertEqual(self.assess(name=NAME.replace('jscrash',kind))['kind'],kind)
    def test_old_other_uid_and_similarly_named_package_are_excluded(self):
        self.assertIsNone(self.assess(start=WHEN+2,end=WHEN+4))
        self.assertIsNone(self.assess(name=NAME.replace('200001','200002')))
        self.assertIsNone(self.assess(name=NAME.replace('target','target2')))
        self.assertIsNone(self.assess(name='../'+NAME))
    def test_inconsistent_header_or_unreadable_body_is_not_confirmed(self):
        for text in (TEXT.replace('Uid:200001','Uid:200002'),TEXT.replace('target','other'),TEXT.replace('10:00:01','10:00:09'),'Permission denied'):
            with self.assertRaises(ValueError):self.assess(text=text)
    def test_child_process_matches_exact_family(self):
        value=self.assess(name=NAME.replace('target-','target:worker-'),text=TEXT.replace('target','target:worker'))
        self.assertTrue(value['confirmed'])
    def session(self,out):
        s=d.Session(Mock(executable='hdc',target='device'),BUNDLE,UID,BUNDLE,Path(out))
        s.begin=WHEN-1;s.end=WHEN+1;s.zone=ZONE;s.began_mono=time.monotonic()-2;s.baseline_ok=True;s.lines=2
        s.clock=Mock(return_value=(WHEN+1,ZONE));return s
    def test_finish_only_exports_new_target_fault_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);old=NAME.replace('01000','00000');s.baseline={old}
            s.listing=Mock(return_value={NAME,old,NAME.replace('target','other')})
            s._read_fault=Mock(return_value=(s.out/'fault_001.log',TEXT))
            with patch.object(d.time,'sleep'):result=s.finish({'stop_reason':'达到事件次数上限'})
            self.assertEqual(result['crash_count'],1);self.assertEqual(result['status'],'complete')
            self.assertEqual(s._read_fault.call_count,1);self.assertIs(s.finish(),result)
            self.assertTrue((s.out/'summary.json').exists())
    def test_time_jump_prevents_confirmation_even_with_new_record(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);s.clock=Mock(return_value=(WHEN+600,ZONE));s.listing=Mock(return_value={NAME})
            s._read_fault=Mock(return_value=(s.out/'fault_001.log',TEXT))
            with patch.object(d.time,'sleep'):result=s.finish()
            self.assertEqual(result['crash_count'],0);self.assertEqual(result['status'],'partial')
    def test_timeout_is_suspected_not_confirmed_and_restore_is_not_crash(self):
        for reason,expected in [('命令超时',1),('拉回后前台仍不是目标应用',0),('用户停止',0)]:
            with tempfile.TemporaryDirectory() as out:
                s=self.session(out);s.listing=Mock(return_value=set())
                with patch.object(d.time,'sleep'):result=s.finish({'stop_reason':reason,'actual_events':3})
                self.assertEqual(result['suspected_count'],expected);self.assertEqual(result['crash_count'],0)
    def test_permission_failure_preserves_log_status_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);s.listing=Mock(side_effect=ValueError('Permission denied'))
            with patch.object(d.time,'sleep'):result=s.finish()
            self.assertEqual(result['status'],'partial');self.assertIn('Permission denied',' '.join(result['errors']))
    def test_stream_filters_historical_logs_and_obeys_size_limit(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);s.capture_begin=WHEN
            payload=b'EST 2026-11-10 09:59:59.000 123 old\nEST 2026-11-10 10:00:01.000 123 current\ntrace continuation\nEST 2026-11-10 10:00:02.000 123 next\n'
            s.proc=Mock(stdout=io.BytesIO(payload));s.stop.set();s._capture()
            text=(s.out/'system_hilog.log').read_text();self.assertNotIn('old',text);self.assertIn('continuation',text)
            s.proc=Mock(stdout=io.BytesIO(payload));s.bytes=0;s.lines=0
            with patch.object(d,'LOG_LIMIT',70):s._capture()
            self.assertTrue(s.truncated);self.assertLessEqual((s.out/'system_hilog.log').stat().st_size,70)
    def test_hilog_display_time_respects_device_zone(self):
        self.assertEqual(d.log_epoch(b'EST 2026-11-10 10:00:01.000 123 message',ZONE),WHEN)
        self.assertIsNone(d.log_epoch(b'trace continuation',ZONE))
    def test_early_stream_exit_and_no_data_are_not_complete(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);s.lines=0;s.proc=Mock(stdout=io.BytesIO(b''));s._capture()
            self.assertIn('结束前退出',s.stream_error)
    def test_report_highlights_evidence_and_escapes_content(self):
        a,b=fixture('before'),fixture('after');a['config']['test_mode']='monkey'
        a['diagnostics']={'status':'complete','incidents':[dict(self.assess(),evidence='logs/diagnostics/fault_001.log')]}
        self.assertEqual(engine.comparison(a,b)['conclusion'],'发现已确认的崩溃或应用无响应')
        with tempfile.TemporaryDirectory() as out:
            html=(engine.report(a,b,out)/'report.html').read_text(encoding='utf8')
            self.assertIn('#b91c1c',html);self.assertIn('fault_001.log',html);self.assertNotIn('<script>',html)
            self.assertIn('&lt;script&gt;',html)
    def test_import_rejects_bad_incident_shape(self):
        a=fixture('before');a['diagnostics']={'incidents':['bad']}
        with tempfile.TemporaryDirectory() as out:
            p=Path(out)/'result.json';p.write_text(json.dumps(a),encoding='utf8')
            with self.assertRaises(ValueError):engine.load_result(p)
    def test_collection_always_finishes_diagnostics_on_stop_or_monkey_log_failure(self):
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled),tempfile.TemporaryDirectory() as out,ExitStack() as stack:
                cancel=threading.Event();hdc=Mock(executable='fake',target='test');hdc.shell.return_value='system'
                monkey=Mock(started=time.monotonic(),actual=1,error='命令超时',result=None)
                monkey.running.return_value=True;monkey.finish.side_effect=OSError('Monkey log write failed')
                def fail_step():
                    if cancelled:cancel.set();raise KeyboardInterrupt
                    raise engine.core.MeasurementError('命令超时')
                monkey.step.side_effect=fail_step
                diagnostic=Mock(incidents=[]);diagnostic.finish.return_value={'status':'partial','incidents':[],'errors':[]}
                stack.enter_context(patch.object(engine,'DesktopHdc',return_value=hdc))
                stack.enter_context(patch.object(engine.core,'find_hdc',return_value='fake'))
                stack.enter_context(patch.object(engine.core,'bundle_json',return_value={'uid':UID,'versionName':'1'}))
                stack.enter_context(patch.object(engine.core,'resolve_entry',return_value=('entry','EntryAbility',BUNDLE)))
                stack.enter_context(patch.object(engine.core,'start'));stack.enter_context(patch.object(engine.core,'pause'))
                stack.enter_context(patch.object(engine,'MonkeySession',return_value=monkey))
                stack.enter_context(patch.object(d,'Session',return_value=diagnostic))
                import traffic_monitor
                traffic=Mock();traffic.summary.return_value={}
                stack.enter_context(patch.object(traffic_monitor,'Tracker',return_value=traffic))
                config=dict(bundle=BUNDLE,out=out,role='before',label='test',test_mode='monkey',count=1,settle=1,duration=5,interval=1,scenario='same')
                result=engine.collect(config,queue.Queue(),cancel)['result']
                diagnostic.start.assert_called_once();diagnostic.finish.assert_called_once()
                self.assertEqual(result['diagnostics']['status'],'partial')
