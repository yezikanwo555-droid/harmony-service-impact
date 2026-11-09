import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_impact import fixture
import impact_app as ui
import impact_engine as e

class Tests(unittest.TestCase):
    def test_distinct_targets_are_supported_and_reported(self):
        a,b=fixture('before'),fixture('after')
        a['environment']['bundle']='com.example.old';b['environment']['bundle']='com.example.new'
        c=e.comparison(a,b)
        self.assertTrue(c['notes']);self.assertFalse(any('包名不一致' in x for x in c['warnings']))
        with tempfile.TemporaryDirectory() as out:
            text=(e.report(a,b,out)/'report.html').read_text(encoding='utf-8')
            self.assertIn('改造前包名：com.example.old',text)
            self.assertIn('改造后包名：com.example.new',text)
    def test_equivalent_sandbox_paths_and_custom_mismatch(self):
        a,b=fixture('before'),fixture('after')
        for d,name in ((a,'com.example.old'),(b,'com.example.new')):
            d['environment']['bundle']=name;d['cache']['path']='/data/app/el2/100/base/'+name+'/cache'
        c=e.comparison(a,b)
        self.assertNotEqual(next(r for r in c['rows'] if r['key']=='cache_end')['status'],'口径不同')
        b['cache']['path']+='/subfolder'
        self.assertEqual(next(r for r in e.comparison(a,b)['rows'] if r['key']=='cache_end')['status'],'口径不同')
    def test_shared_uid_warning(self):
        a,b=fixture('before'),fixture('after');b['environment']['bundle']='com.other'
        a['environment']['uid']=b['environment']['uid']=123
        self.assertTrue(any('共享 UID' in x for x in e.comparison(a,b)['warnings']))
    def test_old_settings_migrate_to_both_groups(self):
        with tempfile.TemporaryDirectory() as out,patch.object(ui,'BASE',Path(out)):
            (Path(out)/'impact_settings.json').write_text(json.dumps({'bundle':'com.legacy','ability':'LegacyAbility'}),encoding='utf-8')
            app=ui.App();app.withdraw()
            try:
                for role in ('before','after'):
                    self.assertEqual(app.group[role]['bundle'].get(),'com.legacy')
                    self.assertEqual(app.group[role]['ability'].get(),'LegacyAbility')
                self.assertNotIn('bundle',app.v)
            finally:app.destroy()
    def test_collection_uses_only_selected_group(self):
        captured=[]
        class Worker:
            def __init__(self,**kwargs):captured.append(kwargs['args'][0])
            def is_alive(self):return False
            def start(self):pass
        with tempfile.TemporaryDirectory() as out,patch.object(ui,'BASE',Path(out)):
            app=ui.App();app.withdraw()
            try:
                app.v['out'].set(out)
                for role,suffix in (('before','old'),('after','new')):
                    app.group[role]['bundle'].set('com.example.'+suffix)
                    app.group[role]['ability'].set(suffix+'Ability')
                    app.group[role]['cache_path'].set('/cache/'+suffix)
                with patch.object(ui.threading,'Thread',Worker):
                    app.start('before');app.start('after')
                self.assertEqual([c['bundle'] for c in captured],['com.example.old','com.example.new'])
                self.assertEqual([c['ability'] for c in captured],['oldAbility','newAbility'])
                self.assertEqual([c['cache_path'] for c in captured],['/cache/old','/cache/new'])
                saved=json.loads((Path(out)/'impact_settings.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['after']['bundle'],'com.example.new')
            finally:app.destroy()

if __name__=='__main__':unittest.main(verbosity=2)
