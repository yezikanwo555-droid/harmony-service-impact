"""One click, one immutable pair of configurations and one fresh report."""
import copy
import datetime as dt
from pathlib import Path
import impact_engine as e
import harmony_startup as core
from startup_app import DesktopHdc


def skipped(config,environment,reason):
    return dict(schema=e.SCHEMA,role=config['role'],label=config['label'],config=config,state='skipped',error=reason,
                started_at=None,startup=[],resources=[],artifact={'status':'missing'},cache={},
                environment=environment,monkey={'status':'not_run'})


def run_pair(configs,events,cancel,threshold=10):
    configs=copy.deepcopy(configs)
    results={};paths={};environments={};report_dir=None;root=None;error='';status='complete'
    network_session=None
    class Forward:
        def put(self,item):
            kind,value=item
            events.put(('group_done' if kind=='done' else kind,value))
    try:
        if [c.get('role') for c in configs]!=['before','after']:raise ValueError('需要改造前、改造后两组配置')
        if configs[0]['bundle']==configs[1]['bundle']:raise ValueError('一键对比需要两个不同包名；同包名更换版本请分别采集')
        root=Path(configs[0]['out']).resolve()/('batch_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        root.mkdir(parents=True)
        h=DesktopHdc(core.find_hdc(configs[0].get('hdc') or None),configs[0].get('target') or None,30,cancel)
        h.select_device()
        events.put(('phase','一键对比：检查两个包是否已安装'))
        for c in configs:
            core.identifier(c['bundle'])
            c['target']=h.target;c['out']=str(root)
            info=core.bundle_json(h.shell('bm','dump','-n',c['bundle']))
            module,ability,_=core.resolve_entry(info,c.get('ability') or None,c.get('module') or None)
            environments[c['role']]={'bundle':c['bundle'],'device':h.target,'version_name':info.get('versionName'),
                                      'version_code':info.get('versionCode'),'module':module,'ability':ability}
        if configs[0].get('network_auto') and configs[0].get('network_mode','current')!='current':
            import network_session as ns
            network_session=ns.Session(h,configs[0],root,cancel,lambda *a:events.put(a));network_session.begin()
            configs[1].update(_network_session_id=network_session.id,_network_applied=True)
        e.save_json(root/'batch.json',dict(state='running',configs=configs,results={}))
        for index,c in enumerate(configs):
            if cancel.is_set():raise KeyboardInterrupt
            events.put(('group_start',c))
            payload=e.collect(c,Forward(),cancel)
            results[c['role']]=payload['result'];paths[c['role']]=payload['out']
            e.save_json(root/'batch.json',dict(state='running',configs=configs,results=paths))
            if not payload['result'] or payload['result']['state']!='complete':
                status='interrupted' if cancel.is_set() else 'error'
                error=(payload['result'] or {}).get('error') or '本组未完成，后续组不再执行'
                break
            if index==0:
                # Avoid the first tested package continuing to compete for resources.
                h.shell('aa','force-stop',c['bundle'])
        if cancel.is_set():status='interrupted';error=error or '用户停止'
    except KeyboardInterrupt:status,error='interrupted','用户停止，后续组不再执行'
    except Exception as exc:status,error='error',str(exc)
    finally:
        if network_session:
            try:restore=network_session.finish()
            except Exception as exc:
                restore=network_session.state;status='error';error+='；'+str(exc)
            for role,data in results.items():
                if data:
                    data['network_restore']=restore
                    if status=='error' and restore and restore.get('state')=='restore_failed':
                        data['state']='error';data['error']+='；网络恢复失败'
                    if paths.get(role):e.save_json(Path(paths[role])/'result.json',data)
        # Only create a partial comparison when actual data was collected in this batch.
        if results and root:
            for c in configs:
                if c['role'] not in results or results[c['role']] is None:
                    results[c['role']]=skipped(c,environments.get(c['role'],{'bundle':c['bundle']}),error or '未执行')
            try:report_dir=e.report(results['before'],results['after'],root,threshold)
            except Exception as exc:status='error';error+='；报告生成失败：'+str(exc)
        if root:
            try:e.save_json(root/'batch.json',dict(state=status,error=error,configs=configs,results=paths,
                                                 report=str(report_dir/'report.html') if report_dir else None))
            except OSError as exc:status='error';error+='；批次清单保存失败：'+str(exc)
        events.put(('batch_done',dict(state=status,error=error,results=results,out=str(root) if root else None,
                                      report=str(report_dir/'report.html') if report_dir else None)))
