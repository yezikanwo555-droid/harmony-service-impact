import json,random,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_controlled_monkey import Device,CONFIG
from test_impact import fixture
import monkey_runner as m
import impact_engine as engine
import impact_app as ui

class RecoverDevice(Device):
    def shell(self,*args,**kwargs):
        if args[:2]==('aa','start'):
            self.calls.append(args);return 'start ability successfully'
        return super().shell(*args,**kwargs)

class Tests(unittest.TestCase):
    def session(self,out,events=1):
        s=m.MonkeySession(RecoverDevice(),dict(CONFIG,monkey_in_app=True,monkey_events=events),out,threading.Event())
        s.started=time.monotonic();s.uid=100;s.module='entry';s.ability='EntryAbility'
        return s
    def logs(self,out):return [json.loads(line) for line in (Path(out)/'monkey_events.jsonl').read_text(encoding='utf8').splitlines()]
    def test_never_generates_back_and_seed_repeats(self):
        a,b=random.Random(42),random.Random(42);kinds=set()
        for _ in range(1000):
            event=m.event_for(a,(0,0,1000,2000),True);kinds.add(event[0])
            self.assertEqual(event,m.event_for(b,(0,0,1000,2000),True))
        self.assertEqual(kinds,{'click','swipe'})
    def test_last_event_is_checked_and_recovered_without_extra_count(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out)
            with patch.object(s,'foreground',side_effect=[(True,3),(True,3),(False,4),(True,3)]),patch.object(s.cancel,'wait',return_value=False):s.step()
            result=s.finish();self.assertEqual(result['status'],'complete');self.assertEqual(result['actual_events'],1)
            self.assertEqual(result['foreground_recovery_successes'],1)
            self.assertTrue(any(r['kind']=='restore_result' and r['success'] and r['phase']=='after_event' for r in self.logs(out)))
    def test_recovered_event_continues_to_next_event(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out,2)
            with patch.object(s,'foreground',side_effect=[(True,3),(True,3),(False,4),(True,3),(True,3),(True,3),(True,3)]),patch.object(s.cancel,'wait',return_value=False):
                s.step();self.assertTrue(s.running());s.step()
            self.assertEqual(s.finish()['actual_events'],2)
    def test_failed_recovery_stops_and_records_last_successful_event(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out)
            with patch.object(s,'foreground',side_effect=[(True,3),(True,3)]+[(False,4)]*4),patch.object(s.cancel,'wait',return_value=False):
                with self.assertRaisesRegex(ValueError,'拉回后'):s.step()
            self.assertIsNone(s.step());result=s.finish();self.assertEqual(result['status'],'error');self.assertEqual(result['actual_events'],1)
            self.assertTrue(any(r['kind']=='restore_result' and not r['success'] for r in self.logs(out)))
    def test_focus_changes_before_injection_recovers_without_stale_click(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out)
            with patch.object(s,'foreground',side_effect=[(True,3),(False,4),(False,4),(True,3)]),patch.object(s.cancel,'wait',return_value=False):self.assertIsNone(s.step())
            self.assertFalse(any(c[:2]==('uitest','uiInput') for c in s.hdc.calls));s.finish()
    def test_cancel_during_recovery_does_not_send_another_event(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out,2)
            def cancel_wait(seconds):s.cancel.set();return True
            with patch.object(s,'foreground',side_effect=[(True,3),(True,3),(False,4)]),patch.object(s.cancel,'wait',side_effect=cancel_wait):
                with self.assertRaises(KeyboardInterrupt):s.step()
            self.assertIsNone(s.step());self.assertEqual(s.finish()['status'],'interrupted')
    def test_recovery_launch_failure_and_unknown_foreground_stop(self):
        for unknown in (False,True):
            with tempfile.TemporaryDirectory() as out:
                s=self.session(out)
                readings=[(True,3),(True,3),ValueError('unreadable') if unknown else (False,4)]
                original=s.hdc.shell
                def shell(*args,**kwargs):return 'failed' if args[:2]==('aa','start') else original(*args,**kwargs)
                with patch.object(s,'foreground',side_effect=readings),patch.object(s.hdc,'shell',side_effect=shell):
                    with self.assertRaises(ValueError):s.step()
                self.assertEqual(s.finish()['status'],'error');self.assertTrue(any(r['kind']=='error' for r in self.logs(out)))
    def test_disabled_mode_retains_old_event_distribution(self):
        rng=random.Random(42)
        self.assertIn('keyEvent',{m.event_for(rng,(0,0,1000,2000))[0] for _ in range(100)})
        self.assertFalse(m.in_app_mode({}));self.assertFalse(m.in_app_mode({'monkey_in_app':'0'}))
    def test_report_and_comparison_include_mode(self):
        a,b=fixture('before'),fixture('after')
        for d in (a,b):d['config'].update(test_mode='monkey',monkey_in_app=False);d['monkey']=dict(status='complete',actual_events=1)
        b['config']['monkey_in_app']=True
        self.assertTrue(any('应用内模式不同' in w for w in engine.comparison(a,b)['warnings']))
        with tempfile.TemporaryDirectory() as out:
            text=(engine.report(a,b,out)/'report.html').read_text(encoding='utf8')
            self.assertIn('应用内模式：开启',text);self.assertIn('拉回成功',text)
    def test_ui_default_and_persistence(self):
        with tempfile.TemporaryDirectory() as out,patch.object(ui,'BASE',Path(out)):
            app=ui.App();app.withdraw()
            try:
                self.assertEqual(app.v['monkey_in_app'].get(),'1')
                app.monkey_in_app_check.invoke();self.assertEqual(app.settings()['monkey_in_app'],'0')
                engine.save_json(app.settings_path,app.settings())
            finally:app.destroy()
            app=ui.App();app.withdraw()
            try:self.assertEqual(app.v['monkey_in_app'].get(),'0')
            finally:app.destroy()
