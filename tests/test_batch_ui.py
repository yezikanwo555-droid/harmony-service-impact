import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import impact_app as ui

class Tests(unittest.TestCase):
    def test_batch_configuration_and_completion(self):
        captured=[]
        class Worker:
            def __init__(self,**kwargs):captured.append(kwargs)
            def is_alive(self):return False
            def start(self):pass
        with tempfile.TemporaryDirectory() as out,patch.object(ui,'BASE',Path(out)):
            app=ui.App();app.withdraw()
            try:
                app.v['out'].set(out);app.v['test_mode'].set('冷热启动 + Monkey')
                app.v['monkey_seconds'].set('30');app.v['monkey_events'].set('5')
                app.group['before']['bundle'].set('com.example.old');app.group['after']['bundle'].set('com.example.new')
                app.datasets={'stale':{}}
                with patch.object(ui.threading,'Thread',Worker):app.start_batch()
                configs=captured[0]['args'][0]
                self.assertEqual([c['bundle'] for c in configs],['com.example.old','com.example.new'])
                self.assertTrue(all(c['test_mode']=='both_monkey' and c['monkey_events']==5 for c in configs))
                self.assertEqual(app.datasets,{})
                self.assertEqual(str(app.stop_button['state']),'normal')
                report=Path(out)/'report.html';report.write_text('test',encoding='utf-8')
                app.events.put(('batch_done',dict(state='complete',error='',results={'before':fixture('before'),'after':fixture('after')},out=out,report=str(report))))
                with patch.object(ui.webbrowser,'open') as opened:
                    app.poll();opened.assert_called_once_with(report.as_uri())
                self.assertFalse(app.batch_running);self.assertEqual(str(app.stop_button['state']),'disabled')
                self.assertEqual(len(app.compare_table.get_children()),len(ui.engine.METRICS))
            finally:app.destroy()

if __name__=='__main__':unittest.main(verbosity=2)
