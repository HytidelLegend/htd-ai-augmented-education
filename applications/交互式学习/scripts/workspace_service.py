"""Loopback API: public dual-graph snapshots, queued actions and skill settings."""
from __future__ import annotations
import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from utils.scripts import learning_project as lp
from utils.scripts import learning_project_management as management
from utils.scripts.learning_config import SKILLS, load_config, save_config
from utils.scripts.structured_io import read_json, validate_json_schema, json_digest
from utils.scripts.learning_navigation import validate_navigation
from utils.scripts.timestamp import iso_timestamp

APP = ROOT/'applications/交互式学习'
SCHEMAS = lp.ROOT/'utils/references'

def project_paths():
    paths = {}
    directory = ROOT/'outputs/interactive-tutor/runs'
    if directory.is_dir():
        for path in directory.glob('*/项目.json'):
            try:
                lp.safe_path(ROOT,path)
                meta = read_json(path)
                validate_json_schema(meta,SCHEMAS/'learning-project-v3.schema.json')
                if not management.entry(ROOT, meta['project_id']).get('archived'): paths[meta['project_id']] = path.parent
            except (ValueError,OSError): continue
    directory = ROOT/'logs/interactive-tutor/runs'
    if directory.is_dir():
        for path in directory.glob('*/state.json'):
            try:
                project = lp.safe_path(ROOT,read_json(path)['project_dir'])
                meta = read_json(project/'项目.json')
                validate_json_schema(meta,SCHEMAS/'learning-project-v3.schema.json')
                if not management.entry(ROOT, meta['project_id']).get('archived'): paths[meta['project_id']] = project
            except (ValueError,OSError,KeyError): continue
    return paths

def navigation_paths():
    paths = {}
    parent = ROOT/'outputs/build-curriculum-navigation/runs'
    if parent.is_dir():
        for path in parent.glob('*/navigation.json'): paths['nav-'+path.parent.name] = path
    parent = ROOT/'logs/build-curriculum-navigation/runs'
    if parent.is_dir():
        for path in parent.glob('*/run-state.json'):
            try:
                state = read_json(path)
                if state['status']=='completed': paths['nav-'+path.parent.name] = lp.safe_path(ROOT,state['navigation_json'])
            except (ValueError,OSError,KeyError): continue
    return {pid: path for pid, path in paths.items() if not management.entry(ROOT, pid).get('archived')}

def public_snapshot(model, pending, can_edit=True):
    active = [l for l in model['lessons'] if not l['archived']]
    units = []
    for u in model['units']:
        p = model['unit_progress'][u['unit_id']]
        units.append({'id':u['unit_id'],'title':u['title'],'status':p['status'],'mastery':p['mastery'],
                      'skip':p['skip'],'group':u['module'],'number':str(u['sequence']),
                      'track': 'main' if any(l['track']=='main' and u['unit_id'] in l['teaches_unit_ids'] for l in active)
                      else u.get('track', 'main' if u['importance'] in ('required','critical_reading') else 'branch')})
    lessons = [{'id':l['lesson_id'],'title':l['title'],'status':l['status'],'skip':l['skip'], 'number':l['number'],
                'group':'主线' if l['track']=='main' else '支线', 'track':l['track'], 'teaches':l['teaches_unit_ids'],'prerequisites':l['prerequisite_unit_ids']} for l in active]
    queued = {}
    for action in pending:
        if action['action_id'] not in model['applied_actions']:
            queued[action['target_type']+':'+action['target_id']] = action['operation']=='skip'
    snapshot = {'id':model['project_id'],'title':model['title'],'status':model['project_status'],
            'state':model['state'],'canEdit':can_edit,
            'units':units,'lessons':lessons,'unitEdges':model['unit_edges'],'lessonEdges':model['lesson_edges'],'pending':queued}
    snapshot['revision'] = json_digest([snapshot, model['revision'], pending])
    return snapshot


def tutor_snapshot(model, path, actions):
    snapshot = public_snapshot(model,actions)
    model = {**model, 'sources': read_json(lp.safe_path(ROOT,model['navigation_json'])).get('sources', [])}
    snapshot['details'] = management.detail(model,path,'tutor')
    snapshot['revision'] = json_digest([snapshot['revision'], snapshot['details'], management.entry(ROOT,model['project_id'])])
    return snapshot

def project(project_id):
    if not re.fullmatch(r'(?:nav-)?\d{8}T\d{6}(?:_\d+)?',project_id): raise ValueError('项目 ID 无效')
    management.ensure_active(ROOT, project_id)
    paths = project_paths()
    if project_id in paths:
        path = paths[project_id]
        with lp.lock(path):
            model = lp.load(path)
            actions = read_json(lp.art(path,'调整指令'))['actions'] if lp.art(path,'调整指令').is_file() else []
            return tutor_snapshot(model,path,actions)
    path = navigation_paths().get(project_id)
    if path is None: raise FileNotFoundError('项目不存在')
    nav = read_json(lp.safe_path(ROOT,path))
    validate_navigation(nav,schema_path=SCHEMAS/'learning-navigation-v2.schema.json',root=ROOT,verify_sources=False)
    model = {'project_id':project_id,'title':nav['title'],'project_status':'in_progress','state':'navigation_ready',
             'revision':json_digest(nav),'units':nav['units'],'lessons':[],'applied_actions':[],
             'unit_edges':lp.edges_from_units(nav['units']),'lesson_edges':[],
             'unit_progress':{u['unit_id']:{'status':'pending','skip':False,'mastery':None} for u in nav['units']}}
    model['material_points'] = nav.get('material_points', [])
    model['created_at'] = nav.get('created_at'); model['sources'] = nav.get('sources', [])
    model['title'] = management.entry(ROOT,project_id).get('title', nav['title'])
    snapshot = public_snapshot(model,[],False)
    snapshot['details'] = management.detail(model,path.parent,'navigation')
    snapshot['revision'] = json_digest([snapshot['revision'], nav, management.entry(ROOT,project_id)])
    return snapshot

def projects():
    result=[]; paths=project_paths()
    used=set()
    for pid,path in paths.items():
        try:
            meta=read_json(path/'项目.json'); used.add(str(Path(meta['navigation_json']).resolve()))
            snapshot=project(pid)
            result.append({'id':pid,'title':snapshot['title'],'status':snapshot['status'],'unitCount':len(snapshot['units']),'lessonCount':len(snapshot['lessons']),'updatedAt':meta['updated_at'],'createdAt':snapshot['details']['createdAt'],'lastLearningAt':snapshot['details']['lastLearningAt']})
        except (ValueError,OSError,KeyError) as error:
            if '已有事务正在执行' in str(error): raise ValueError('项目正在保存，请稍后刷新') from error
            continue
    for pid,path in sorted(navigation_paths().items(),reverse=True):
        if str(path.resolve()) in used: continue
        try:
            snapshot=project(pid); nav=read_json(path)
            result.append({'id':pid,'title':snapshot['title'],'status':'in_progress','unitCount':len(snapshot['units']),'lessonCount':0,'updatedAt':nav['updated_at'],'createdAt':snapshot['details']['createdAt'],'lastLearningAt':None})
            used.add(str(path.resolve()))
        except (ValueError,OSError,KeyError): continue
    return sorted(result,key=lambda x:x['updatedAt'],reverse=True)

def queue_action(pid,payload):
    if not isinstance(payload, dict) or set(payload) != {'actionId','expectedRevision','targetType','targetId','operation'}: raise ValueError('调整指令字段无效')
    if any(not isinstance(value, str) or not value for value in payload.values()): raise ValueError('调整指令字段必须为非空字符串')
    if payload['targetType'] not in ('unit','lesson') or payload['operation'] not in ('skip','restore') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',payload['actionId']): raise ValueError('调整指令无效')
    path=project_paths().get(pid)
    if path is None: raise ValueError('请先建立交互式学习项目')
    with lp.lock(path):
        model=lp.load(path); file=lp.art(path,'调整指令')
        actions=read_json(file) if file.is_file() else {'actions':[]}
        existing=next((a for a in actions['actions'] if a['action_id']==payload['actionId']),None)
        if existing:
            if any(existing[k]!=payload[v] for k,v in [('target_type','targetType'),('target_id','targetId'),('operation','operation')]): raise ValueError('操作 ID 冲突')
            return tutor_snapshot(model,path,actions['actions'])
        snapshot=tutor_snapshot(model,path,actions['actions'])
        if snapshot['revision'] != payload['expectedRevision']: raise ValueError('项目版本冲突，请刷新后重试')
        known={u['unit_id'] for u in model['units']} if payload['targetType']=='unit' else {l['lesson_id'] for l in model['lessons'] if not l['archived']}
        if payload['targetId'] not in known: raise ValueError('节点不存在')
        actions['actions'].append({'action_id':payload['actionId'],'target_type':payload['targetType'],'target_id':payload['targetId'],
            'operation':payload['operation'],'base_revision':model['revision'],'created_at':iso_timestamp()})
        lp.store(file,actions)
        return tutor_snapshot(model,path,actions['actions'])


def manage_project(pid, payload):
    management.validate_id(pid)
    if not isinstance(payload, dict): raise ValueError('项目操作必须为 JSON 对象')
    operation = payload.get('operation')
    checkpoint = management.operation_checkpoint(ROOT,payload.get('actionId'))
    # Failed archive may already have moved its directory. Replay from bound checkpoint.
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(payload.get('actionId',''))) and checkpoint and checkpoint.is_file():
        saved = read_json(checkpoint); record = saved.get('record')
        if saved.get('target'):
            target = saved['target']
            return management.operate(ROOT,pid,target['path'],target['kind'],payload,payload.get('expectedRevision'))
        if record and operation in ('archive','restore'):
            return management.operate(ROOT,pid,record['original_path'],record['kind'],payload,payload.get('expectedRevision'))
        if saved.get('state') == 'completed':
            record = management.entry(ROOT,pid)
            if record.get('original_path'):
                return management.operate(ROOT,pid,record['original_path'],record['kind'],payload,payload.get('expectedRevision'))
    if operation == 'restore':
        record = management.entry(ROOT,pid)
        if not record.get('archived'): raise ValueError('项目未归档')
        return management.operate(ROOT,pid,record['original_path'],record['kind'],payload,json_digest(record))
    path = project_paths().get(pid)
    kind = 'tutor' if path else 'navigation'
    path = path or navigation_paths().get(pid)
    if path is None: raise FileNotFoundError('项目不存在')
    def revision():
        if kind == 'navigation': return project(pid)['revision']
        model = lp.load(path)
        actions = read_json(lp.art(path,'调整指令'))['actions'] if lp.art(path,'调整指令').is_file() else []
        return tutor_snapshot(model,path,actions)['revision']
    result = management.operate(ROOT,pid,path,kind,payload,revision)
    return result

class Handler(BaseHTTPRequestHandler):
    def trusted(self):
        origin=self.headers.get('Origin','')
        host=self.headers.get('Host','')
        if not re.fullmatch(r'127\.0\.0\.1(?::\d+)?',host) or (origin and not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}',origin)):
            raise ValueError('请求来源无效')
    def body(self):
        self.trusted()
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': raise ValueError('仅接受 JSON')
        count=int(self.headers.get('Content-Length','0'))
        if not 0<count<=100_000: raise ValueError('请求大小无效')
        payload = json.loads(self.rfile.read(count))
        if not isinstance(payload, dict): raise ValueError('请求必须为 JSON 对象')
        return payload
    def do_OPTIONS(self):
        try: self.trusted(); self.send_payload({},204)
        except ValueError as e: self.send_payload({'error':str(e)},403)
    def do_GET(self): self.dispatch('GET')
    def do_POST(self): self.dispatch('POST')
    def do_PUT(self): self.dispatch('PUT')
    def dispatch(self,method):
        try:
            self.trusted(); path=urlparse(self.path).path
            if method=='GET' and path=='/health': payload={'status':'ok','protocolVersion':3}
            elif method=='GET' and path=='/api/projects': payload=projects()
            elif method=='GET' and path=='/api/archives': payload=[{**item,'revision':json_digest(management.entry(ROOT,item['id']))} for item in management.archived_projects(ROOT)]
            elif method=='GET' and path=='/api/ui-config': payload=yaml.safe_load((APP/'config.yaml').read_text(encoding='utf-8'))
            elif method=='GET' and path=='/api/config': payload={skill:load_config(skill,ROOT) for skill in SKILLS}
            elif method=='PUT' and path.startswith('/api/config/'):
                skill=unquote(path.removeprefix('/api/config/')); body=self.body()
                payload=save_config(skill,body['config'],body['expectedRevision'],ROOT)
            elif path.startswith('/api/projects/'):
                parts=path.removeprefix('/api/projects/').split('/'); pid=unquote(parts[0])
                if method=='GET' and len(parts)==1: payload=project(pid)
                elif method=='GET' and parts[1:]==['version']: payload={'revision':project(pid)['revision']}
                elif method=='POST' and parts[1:]==['actions']: payload=queue_action(pid,self.body())
                elif method=='POST' and parts[1:]==['manage']: payload=manage_project(pid,self.body())
                else: self.send_payload({'error':'未找到接口'},404); return
            else: self.send_payload({'error':'未找到接口'},404); return
            self.send_payload(payload)
        except FileNotFoundError as e: self.send_payload({'error':str(e)},404)
        except (ValueError,KeyError,StopIteration) as e: self.send_payload({'error':str(e)},409 if '版本冲突' in str(e) else 400)
        except Exception: self.send_payload({'error':'读取或保存失败，请检查本地服务日志'},500)
    def send_payload(self,payload,code=200):
        body=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8')
        origin=self.headers.get('Origin','')
        if re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}',origin): self.send_header('Access-Control-Allow-Origin',origin)
        self.send_header('Access-Control-Allow-Methods','GET,POST,PUT,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        if code!=204: self.wfile.write(body)

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=5178);args=p.parse_args()
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
if __name__=='__main__':main()
