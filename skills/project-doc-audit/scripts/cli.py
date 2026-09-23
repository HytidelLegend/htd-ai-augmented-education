"""CLI for project-doc-audit."""
from __future__ import annotations
import argparse, json, sys, uuid
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'utils' / 'scripts'))
from document_audit import audit
WORKFLOW='project-doc-audit'
STAGES=['prepared','discovering','loading_cache','comparing_snapshots','checking_structure','checking_documents','checking_skill_catalog','checking_skill_scenarios','checking_dependencies','checking_environment','validating_findings','rendering_report','verifying_report','completed']
def dirs(root, run): return root/'logs'/WORKFLOW/'runs'/run, root/'outputs'/WORKFLOW/'runs'/run
def now(): return datetime.now().astimezone().isoformat(timespec='seconds')
def save(p,obj): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def load(root,run): return json.loads((dirs(root,run)[0]/'state.json').read_text(encoding='utf-8'))
def transition(path,state,target):
    state['status']=target; state['current_stage']=target; state['completed_steps'].append(target); state['event_sequence']+=1; state['updated_at']=now(); state['last_heartbeat_at']=state['updated_at']; save(path,state)
def render(report):
    s=report['summary']; lines=['# 项目文档审计报告','',f"检查文档：{s['checked_documents']} 个",f"跳过未变化文档：{s.get('skipped_documents',0)} 个",f"发现差异：{s['findings']} 条",'']
    if not report['findings']: lines.append('未发现确定性差异。')
    for i,f in enumerate(report['findings'],1):
        lines += [f"## {i}. `{f['kind']}`",'',f"- 路径：`{f['path']}`",f"- 当前：`{f.get('current')}`",f"- 期望：`{f.get('expected')}`",f"- 建议：{f['suggestion']}",f"- 证据：{', '.join(f.get('evidence',[]))}",f"- 置信度：`{f['confidence']}`",'']
    return '\n'.join(lines)+'\n'
def start(root):
    run=uuid.uuid4().hex[:12]; log,out=dirs(root,run); cache_path=root/'logs/project-doc-audit/cache.json'; previous=json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.is_file() else None
    stamp=now(); state={'schema_version':'1.0','workflow':WORKFLOW,'run_id':run,'status':'prepared','current_stage':'prepared','resume_stage':None,'current_object_id':None,'current_batch_id':None,'completed_steps':['prepared'],'pending_decisions':[],'error':None,'created_at':stamp,'updated_at':stamp,'last_heartbeat_at':stamp,'event_sequence':1}; save(log/'state.json',state)
    for stage in STAGES[1:STAGES.index('checking_environment')+1]: transition(log/'state.json',state,stage)
    report,cache=audit(root,previous); olddocs=(previous or {}).get('documents',{}); cache['skipped_documents']=sum(1 for k,v in cache['documents'].items() if olddocs.get(k)==v); report['summary']['skipped_documents']=cache['skipped_documents']; cache_path.parent.mkdir(parents=True,exist_ok=True); save(cache_path,cache)
    transition(log/'state.json',state,'validating_findings'); save(log/'findings.json',report); transition(log/'state.json',state,'rendering_report'); (out/'report.md').parent.mkdir(parents=True,exist_ok=True); (out/'report.md').write_text(render(report),encoding='utf-8'); save(out/'findings.json',report); transition(log/'state.json',state,'verifying_report'); transition(log/'state.json',state,'completed')
    print(json.dumps({'run_id':run,'status':'completed','findings':report['summary']['findings'],'report':str(out/'report.md')},ensure_ascii=False))
def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    for cmd in ('start','status','verify','deliver'):
        p=sub.add_parser(cmd); p.add_argument('--root',default='.'); p.add_argument('--run-id')
    a=ap.parse_args(); root=Path(a.root).resolve()
    if a.cmd=='start': return start(root)
    if not a.run_id: ap.error('--run-id required')
    log,out=dirs(root,a.run_id); state=load(root,a.run_id)
    if a.cmd=='status': print(json.dumps(state,ensure_ascii=False)); return
    if a.cmd=='verify':
        ok=state.get('status')=='completed' and (out/'report.md').is_file() and (log/'findings.json').is_file(); print(json.dumps({'ok':ok},ensure_ascii=False)); raise SystemExit(0 if ok else 4)
    if state.get('status')!='completed': raise SystemExit(4)
    payload = (out/'report.md').read_text(encoding='utf-8').encode('utf-8')
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.write(payload.decode('utf-8'))
if __name__=='__main__': main()
