import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_storage_page import system_data
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import impact_app as ui
import impact_engine as engine

class Tests(unittest.TestCase):
    def setUp(self):
        self.app=ui.App();self.app.withdraw()
    def tearDown(self):
        self.app.destroy()
    def test_snapshot_events_update_display(self):
        self.app.page_cache_active=True
        self.app.events.put(('cache_snapshot',dict(stage='start',status='ok',raw_text='0 B',mib=0)))
        self.app.events.put(('cache_snapshot',dict(stage='end',status='ok',raw_text='1 MiB',mib=1)))
        self.app.poll()
        self.assertEqual(self.app.cache.get(),'1 MiB')
        self.assertEqual(len(self.app.cache_snapshots),2)
    def test_import_compare_export_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            for role in ('before','after'):
                p=Path(folder)/(role+'.json');engine.save_json(p,system_data(role))
                with patch.object(ui.filedialog,'askopenfilename',return_value=str(p)):
                    self.app.import_result(role)
            self.assertEqual(len(self.app.compare_table.get_children()),len(engine.METRICS))
            self.app.v['out'].set(folder)
            with patch.object(ui.webbrowser,'open') as opened:
                self.app.make_report()
                self.assertEqual(opened.call_count,1)
            report=Path(self.app.last_report).read_text(encoding='utf-8')
            self.assertIn('系统存储页缓存快照',report)
    def test_invalid_manual_cache_number(self):
        for value in ('NaN','-1','inf'):
            with self.assertRaises(ValueError):ui.App.cache_number(value)

if __name__=='__main__':unittest.main(verbosity=2)
