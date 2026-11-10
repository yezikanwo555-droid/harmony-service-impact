import tempfile,unittest,copy
from pathlib import Path
from unittest.mock import patch
import impact_app as ui
import pair_charts as p
from test_monitor_charts import sample
from test_impact import fixture
from test_storage_page import system_data

class ModelTests(unittest.TestCase):
    def test_shared_axis_and_short_series_has_no_padding(self):
        data={'before':{'resources':[sample(0,5),sample(10,10)]},'after':{'resources':[sample(0,3),sample(20,8)]}}
        rows,start,end,top=p.model(data,'cpu_pct')
        self.assertEqual((start,end,top),(0,20,100));self.assertEqual(rows['before'][-1]['elapsed_s'],10)
    def test_snapshot_cache_not_mistaken_for_continuous_values(self):
        data=system_data('before');data['resources']=[sample(0),sample(10)]
        self.assertEqual(p.rows_for(data,'cache_mib'),[])
        self.assertIn('增长 2 MiB',p.snapshot_text(data))
        data['cache']['checkpoints'][1]['status']='error';self.assertIn('增长 —',p.snapshot_text(data))
    def test_report_legend_gaps_and_escaped_identity(self):
        a,b=fixture('before'),fixture('after');a['environment']['bundle']='<script>bad</script>'
        a['resources']=[sample(0,5),sample(2,None),sample(4,8)];b['resources']=[sample(0,9),sample(4,10)]
        html=p.svg_chart(a,b,'cpu_pct','CPU %')
        self.assertIn('#2563eb',html);self.assertIn('#c45a0a',html);self.assertIn('stroke-dasharray="6 3"',html)
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertEqual(html.count('<polyline'),1)
        self.assertIn('原始采样值',html)

class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=patch.object(ui,'BASE',Path(self.temp.name));self.base.start()
        self.app=ui.App();self.app.withdraw()
    def tearDown(self):self.app.destroy();self.base.stop();self.temp.cleanup()
    def test_before_survives_while_after_streams_and_old_after_is_removed(self):
        a=fixture('before');a['resources']=[sample(0,7),sample(10,9)]
        self.app.datasets={'before':a,'after':fixture('after')}
        self.app.prepare_live(dict(role='after',bundle='com.example.after',cache_mode='directory'))
        self.app.sample_rows.append(sample(0,10));data=self.app.chart_datasets()
        self.assertEqual(data['before']['resources'],a['resources']);self.assertEqual(len(data['after']['resources']),1)
        self.assertNotIn('after',self.app.datasets)
    def test_imported_pair_draws_both_colors(self):
        self.app.datasets={role:dict(resources=[sample(0,5),sample(10,9)],cache={}) for role in ('before','after')}
        chart=self.app.pair_charts
        if chart.pending is not None:chart.after_cancel(chart.pending)
        chart.draw();c=chart.canvases['cpu_pct']
        self.assertTrue(c.find_withtag('before'));self.assertTrue(c.find_withtag('after'))
        chart.at_time(8,'cpu_pct');self.assertIn('实际 10s',chart.info.get())
