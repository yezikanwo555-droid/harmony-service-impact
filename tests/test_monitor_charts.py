import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import monitor_charts as charts
import impact_app as ui

def sample(t,cpu=5,memory=20,cache=1):
    return dict(elapsed_s=t,cpu_pct=cpu,pss_mib=memory,cache_mib=cache,cpu_note='',memory_note='',cache_note='')

class ModelTests(unittest.TestCase):
    def test_gaps_and_zero_are_distinct(self):
        rows=[sample(0,0),sample(1,5),sample(2,None),sample(3,7),sample(4,float('nan'))]
        self.assertEqual(charts.segments(rows,'cpu_pct',0,4,100),[[(0,0),(1,5)],[(3,7)]])
    def test_time_window_uses_elapsed_time(self):
        rows=[sample(0),sample(30),sample(125),sample(180)]
        visible,start,end=charts.visible_rows(rows,60)
        self.assertEqual([r['elapsed_s'] for r in visible],[125,180]);self.assertEqual((start,end),(120,180))
        self.assertEqual(len(charts.visible_rows(rows,None)[0]),4)
    def test_downsampling_preserves_spike_and_dip(self):
        rows=[sample(i/1000,5) for i in range(10000)]
        rows[301]['cpu_pct']=99;rows[302]['cpu_pct']=0
        result=charts.segments(rows,'cpu_pct',0,10,20)[0]
        self.assertIn((.301,99),result);self.assertIn((.302,0),result);self.assertLessEqual(len(result),84)
    def test_invalid_time_and_invalid_value_not_zero(self):
        rows=[sample(float('inf')),sample(-1),sample(2,-1),sample(3,True)]
        visible,_,_=charts.visible_rows(rows,None)
        self.assertEqual(len(visible),2);self.assertEqual(charts.segments(visible,'cpu_pct',0,3,30),[])

class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=patch.object(ui,'BASE',Path(self.temp.name));self.base.start()
        self.app=ui.App();self.app.withdraw();self.view=self.app.monitor_charts
    def tearDown(self):
        self.app.destroy();self.base.stop();self.temp.cleanup()
    def render(self):
        if self.view.pending is not None:self.view.after_cancel(self.view.pending)
        self.view.draw()
    def test_pause_keeps_collection_running_and_resume_catches_up(self):
        self.app.events.put(('resource',sample(1)));self.app.poll();self.view.toggle_pause()
        self.app.events.put(('resource',sample(2)));self.app.poll();self.render()
        self.assertEqual(len(self.app.sample_rows),2);self.assertEqual(len(self.view.visible),1)
        self.assertEqual(len(self.app.resource_table.get_children()),2)
        self.assertFalse(self.app.cancel.is_set())
        self.view.toggle_pause();self.render();self.assertEqual(len(self.view.visible),2)
    def test_snapshots_never_draw_continuous_cache_line(self):
        self.app.page_cache_active=True
        self.app.cache_snapshots=[dict(stage='start',status='ok',mib=0,raw_text='0 B'),dict(stage='end',status='error',error='locked')]
        self.app.sample_rows=[sample(0),sample(1)];self.render()
        c=self.view.canvases['cache_mib']
        self.assertFalse(c.find_withtag('series'));self.assertNotIn('cache_mib',self.view.bounds)
        self.assertIn('读取失败',[c.itemcget(i,'text') for i in c.find_all() if c.type(i)=='text'])
    def test_keyboard_reveals_missing_value_and_group_reset(self):
        self.app.sample_rows=[sample(0),sample(1,None)];self.render();self.view.step('last')
        self.assertIn('CPU 不可用',self.view.detail.get())
        self.view.toggle_pause();self.app.prepare_live(dict(role='after',bundle='com.example.new',cache_mode='system_ui'))
        self.assertIsNone(self.view.frozen);self.assertIsNone(self.view.selected)
        self.assertEqual(self.app.sample_rows,[]);self.assertIn('改造后',self.view.context.get())
