import json,random,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import monkey_runner as m

CONFIG=dict(bundle='com.example.test',monkey_seconds=60,monkey_events=1,monkey_interval_ms=50,monkey_seed=42)
class Device:
    def __init__(self):self.calls=[];self.change_focus=False;self.focus_calls=0;self.response='No Error'
    def shell(self,*args,**kwargs):
        self.calls.append(args)
        if args[:2]==('uitest','dumpLayout'):return 'DumpLayout saved to: /tmp/file'
        if args[0]=='cat':return json.dumps({'attributes':dict(hostWindowId='3',visible='true',bounds='[0,0][1000,2000]')})
        if args[:2]==('uitest','uiInput'):return self.response
        return ''

class Tests(unittest.TestCase):
    def session(self,out,device=None):
        s=m.MonkeySession(device or Device(),CONFIG,out,threading.Event());s.started=time.monotonic();s.uid=100;s.foreground=lambda:(True,3)
        return s
    def test_successful_count_and_event_log(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out);row=s.step();self.assertEqual(row['index'],1);self.assertIsNone(s.step())
            result=s.finish();self.assertEqual(result['status'],'complete')
            rows=[json.loads(x) for x in (Path(out)/'monkey_events.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(x['kind']=='event' for x in rows),1)
    def test_focus_switch_prevents_injection(self):
        with tempfile.TemporaryDirectory() as out:
            s=self.session(out)
            with patch.object(s,'foreground',side_effect=[(True,3),(False,4)]):
                with self.assertRaises(ValueError):s.step()
            self.assertFalse(any(c[:2]==('uitest','uiInput') for c in s.hdc.calls));self.assertEqual(s.finish()['status'],'error')
    def test_cancel_and_deadline_send_no_more_commands(self):
        for cancelled in (True,False):
            with tempfile.TemporaryDirectory() as out:
                s=self.session(out)
                if cancelled:s.cancel.set()
                else:s.started-=100
                self.assertIsNone(s.step());self.assertEqual(s.hdc.calls,[]);s.finish()
    def test_injection_failure_not_counted(self):
        with tempfile.TemporaryDirectory() as out:
            d=Device();d.response='Error: failed';s=self.session(out,d)
            with self.assertRaises(ValueError):s.step()
            self.assertEqual(s.finish()['actual_events'],0)
    def test_seed_and_bounds(self):
        a,b=random.Random(42),random.Random(42)
        for _ in range(100):
            action=m.event_for(a,(0,0,1000,2000));self.assertEqual(action,m.event_for(b,(0,0,1000,2000)))
            if action[0]=='keyEvent':continue
            coords=list(map(int,action[1:-1] if action[0]=='swipe' else action[1:]))
            for x,y in zip(coords[::2],coords[1::2]):self.assertTrue(120<=x<=880 and 360<=y<=1640)
    def test_focus_parser_rejects_ambiguous_window(self):
        text='App 0 500 3 1 abc\nOther 0 600 4 1 def\nFocus window: 3\n'
        self.assertEqual(m.focus_pid(text),(500,3))
        with self.assertRaises(ValueError):m.focus_pid(text+'Fake 0 700 3 1 xyz\n')

if __name__=='__main__':unittest.main(verbosity=2)
