import copy,json,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from test_impact import fixture
import impact_engine as e
import storage_page as s
BUNDLE='com.example.service'

def node(id,text='',rect='[20,400][600,500]'):
    return {'attributes':{'id':id,'text':text,'bounds':rect,'visible':'true','enabled':'true'},'children':[]}
def tree(children):return {'attributes':{'bounds':'[0,0][1276,2848]'},'children':children}
def detail(raw='1.50 MB',bundle=BUNDLE):
    prefix='Setting.AppItem.CacheDataGroup.'+bundle+'_0_cacheSizeData'
    return tree([node('Setting.AppItem.title_id','香港出境服务'),node(prefix+'.title','缓存'),node(prefix+'.result',raw),
                 node('Setting.AppItem.AppDataGroup.'+bundle+'_0_dataSizeData.result','74.5 MB')])
def system_data(role):
    d=fixture(role)
    d['cache']=dict(source='system_storage_page',path=None,unit_base=1000,checkpoints=[dict(stage='start',status='ok',mib=0,raw_text='0 B'),dict(stage='end',status='ok',mib=2,raw_text='2 MiB')])
    return d

class Tests(unittest.TestCase):
    def test_units_and_rejected_placeholders(self):
        self.assertEqual(s.parse_size('0 B'),0)
        self.assertEqual(s.parse_size('1.5 MB'),1500000)
        self.assertEqual(s.parse_size('1.5 MB',1024),1572864)
        self.assertEqual(s.parse_size('1 MiB'),1048576)
        for x in ('计算中','—','-1 B','NaN MB','0 B / 100 MB','1,2 MB'):
            with self.assertRaises(ValueError):s.parse_size(x)
    def test_only_exact_bundle_cache_not_data(self):
        p=s.parse_detail(detail('0 B'),BUNDLE)
        self.assertEqual(p['mib'],0);self.assertEqual(p['raw_text'],'0 B')
        with self.assertRaises(ValueError):s.parse_detail(detail(),BUNDLE+'1')
    def test_missing_or_duplicate_cache_rejected(self):
        d=detail();d['children'].pop(2)
        with self.assertRaises(ValueError):s.parse_detail(d,BUNDLE)
        d=detail();d['children'].append(copy.deepcopy(d['children'][2]))
        with self.assertRaises(ValueError):s.parse_detail(d,BUNDLE)
    def test_snapshots_override_resource_and_manual_values(self):
        d=system_data('before');d['cache']['manual_end_mib']=999
        self.assertEqual(e.summarize(d)['cache_growth'],2)
        self.assertEqual(e.summarize(d)['cache_source'],'系统存储页自动快照')
    def test_partial_snapshot_not_zero_or_manual(self):
        d=system_data('before');d['cache']['checkpoints'][1].update(status='unavailable',mib=None)
        d['cache'].update(manual_start_mib=1,manual_end_mib=10)
        self.assertIsNone(e.summarize(d)['cache_end']);self.assertIsNone(e.summarize(d)['cache_growth'])
    def test_cache_mixed_sources_or_units_not_comparable(self):
        a,b=system_data('before'),system_data('after');b['cache']['unit_base']=1024
        self.assertEqual(next(r for r in e.comparison(a,b)['rows'] if r['key']=='cache_growth')['status'],'口径不同')
        self.assertTrue(any('单位换算' in w for w in e.comparison(a,b)['warnings']))
    def test_report_raw_values_and_timestamps(self):
        a,b=system_data('before'),system_data('after')
        a['cache']['checkpoints'][0]['recorded_at']='2026-11-02T15:00:00+08:00'
        with tempfile.TemporaryDirectory() as out:
            p=e.report(a,b,out);text=(p/'report.html').read_text(encoding='utf-8')
            self.assertIn('0 B',text);self.assertIn('2026-11-02T15:00:00+08:00',text)
            self.assertIn('不会使用人工补录覆盖失败读数',text)
    def test_reader_navigation_has_no_destructive_actions(self):
        layouts=[tree([node('Setting.Storage.title_id','存储','[224,190][364,272]'),
                       node('Setting.Storage.AppGroup.'+BUNDLE+',0.title','香港出境服务')]),detail(),detail()]
        class Fake:
            calls=[]
            def shell(self,*args):
                self.calls.append(args)
                if args[0]=='bm':return json.dumps(dict(hapModuleInfos=[dict(abilityInfos=[dict(name=s.ABILITY,skills=[dict(uris=[dict(scheme='settings',host='storage_manager')])])])]))
                if args[0]=='aa':return 'start ability successfully.'
                if args[0]=='cat':return json.dumps(layouts.pop(0))
                if args[1]=='dumpLayout':return 'DumpLayout saved to:/data/local/tmp/test.json'
                return 'No Error'
        fake=Fake();reader=s.StoragePageReader(fake,BUNDLE,threading.Event())
        with patch.object(reader,'pause'):
            p=reader.read()
        self.assertEqual(p['status'],'ok')
        actions=[x for x in fake.calls if x[:2]==('uitest','uiInput')]
        self.assertEqual(actions,[('uitest','uiInput','click','310','450'),('uitest','uiInput','keyEvent','Home')])
    def test_cancel_before_any_navigation(self):
        cancel=threading.Event();cancel.set()
        reader=s.StoragePageReader(None,BUNDLE,cancel)
        with self.assertRaises(KeyboardInterrupt):reader.check()

if __name__=='__main__':unittest.main(verbosity=2)
