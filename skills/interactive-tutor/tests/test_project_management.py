"""Isolated HTTP management, archive/restore, conflict and activity regression."""
import copy
import json
from pathlib import Path
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from .test_interactive_tutor import workspace, new_project, import_module, ROOT, decision
from utils.scripts import learning_project as lp, learning_project_management as pm
from utils.scripts.structured_io import read_json, write_json, json_digest


def test_all_http_interfaces_and_independent_navigation(workspace, monkeypatch):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    service = import_module('management_service',ROOT/'applications/交互式学习/scripts/workspace_service.py')
    monkeypatch.setattr(service,'ROOT',workspace)
    nav_id = 'nav-' + model['project_id']; nav_path = Path(model['navigation_json'])
    # Exercise actual discovery; a stub previously hid archived-navigation regressions.
    discovery = workspace/'logs/build-curriculum-navigation/runs'/model['project_id']/'run-state.json'
    write_json(discovery, {'status': 'completed', 'navigation_json': str(nav_path)})
    opened = []
    monkeypatch.setattr(pm.os,'startfile',lambda path: opened.append(path),raising=False)
    server = service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    def call(path,method='GET',payload=None):
        body = json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None
        req = Request(f'http://127.0.0.1:{server.server_port}'+path,data=body,method=method,headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=5) as r: return {} if r.status == 204 else json.loads(r.read())
    serial = 0
    def manage(pid,operation,title=None,revision=None,action_id=None):
        nonlocal serial
        serial += 1
        revision = revision or call('/api/projects/'+pid)['revision']
        payload = {'actionId':action_id or f'TEST-MANAGE-{serial}','expectedRevision':revision,'operation':operation}
        if title is not None: payload['title'] = title
        return call('/api/projects/'+pid+'/manage','POST',payload),payload
    prefix = '/api/projects/'+model['project_id']
    try:
        assert call('/health')['status'] == 'ok'
        assert call('/api/projects')[0]['createdAt']
        snapshot = call(prefix); assert snapshot['details']['lastLearningAt'] is None
        assert call(prefix+'/version')['revision'] == snapshot['revision']
        assert call('/api/ui-config')['poll_interval_seconds'] > 0
        for skill, config in call('/api/config').items():
            assert call('/api/config/'+skill,'PUT',{'config':config['config'],'expectedRevision':config['revision']})['revision'] == config['revision']
        assert call(prefix+'/manage','OPTIONS') == {}
        for suffix in ('/manage', '/actions'):
            with pytest.raises(HTTPError) as error: call(prefix+suffix, 'POST', [])
            assert error.value.code == 400
        result, rename = manage(model['project_id'],'rename','导师的新名字')
        assert call(prefix)['title'] == '导师的新名字'
        assert read_json(nav_path)['title'] == '合成测试课程'
        assert call(prefix)['details']['lastLearningAt'] is None
        assert call(prefix+'/manage','POST',rename) == result
        with pytest.raises(HTTPError) as error:
            manage(model['project_id'],'rename','冲突名字',snapshot['revision'])
        assert error.value.code == 409
        manage(model['project_id'],'open-directory'); assert opened == [str(project)]
        before = lp.load(project)
        action = {'actionId':'TEST-SKIP','expectedRevision':call(prefix)['revision'],'targetType':'unit','targetId':'U-0','operation':'skip'}
        first = call(prefix+'/actions','POST',action); assert call(prefix+'/actions','POST',action) == first
        result, archived = manage(model['project_id'],'archive')
        assert not project.exists() and nav_path.is_file()
        assert call(prefix+'/manage','POST',archived) == result
        listed = call('/api/projects'); assert [p['id'] for p in listed] == [nav_id]
        archive = call('/api/archives')[0]; assert archive['id'] == model['project_id']
        with pytest.raises(HTTPError): call(prefix)
        result, restored = manage(model['project_id'],'restore',revision=archive['revision'])
        assert call(prefix+'/manage','POST',restored) == result
        assert project.exists() and lp.load(project)['title'] == before['title']
        assert lp.load(project)['unit_progress'] == before['unit_progress']
        assert [p['id'] for p in call('/api/projects')] == [model['project_id']]
        # Archive tutor once more, then separately rename/archive/restore its navigation entry.
        manage(model['project_id'],'archive')
        nav_bytes = nav_path.read_bytes()
        manage(nav_id,'rename','导航的新名字')
        assert call('/api/projects/'+nav_id)['title'] == '导航的新名字'
        assert nav_path.read_bytes() == nav_bytes
        manage(nav_id,'open-directory'); assert opened[-1] == str(nav_path.parent)
        manage(nav_id,'archive'); assert call('/api/projects') == []
        assert nav_id not in service.navigation_paths()
        with pytest.raises(HTTPError) as error: call('/api/projects/'+nav_id)
        assert error.value.code == 400
        assert call(prefix+'/manage','POST',rename)['status'] == 'completed'
        assert nav_path.read_bytes() == nav_bytes
        a = next(a for a in call('/api/archives') if a['id'] == nav_id)
        manage(nav_id,'restore',revision=a['revision'])
        assert call('/api/projects')[0]['title'] == '导航的新名字'
        assert call('/api/projects/'+nav_id)['details']['currentLesson'] is None
        with pytest.raises(HTTPError): call('/api/projects/../../bad/manage','POST',{})
        with pytest.raises(HTTPError): manage(nav_id,'rename','\n')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_learning_time_updates_only_after_teaching_commit(workspace):
    project = new_project(workspace,1); model, receipt = lp.context(project); cfg = receipt['config']
    assert model['created_at'] and model['last_learning_at'] is None
    d = decision(project,model,cfg)
    assert model['last_learning_at'] is None
    lp.publish(project,model,cfg,d)
    learned = lp.load(project)['last_learning_at']; assert learned
    model['title'] = '仅改展示名'; lp.save(project,model,cfg)
    assert lp.load(project)['last_learning_at'] == learned
    lp.collect_answers(project,model,{'Q-1':'A'}); lp.save(project,model,cfg)
    assert lp.load(project)['last_learning_at']


def test_busy_catalog_does_not_report_project_as_deleted(workspace, monkeypatch):
    project = new_project(workspace,1)
    service = import_module('busy_management_service',ROOT/'applications/交互式学习/scripts/workspace_service.py')
    monkeypatch.setattr(service,'ROOT',workspace)
    with lp.lock(project):
        with pytest.raises(ValueError,match='正在保存'): service.projects()
    assert service.projects()[0]['id'] == lp.load(project)['project_id']


def test_archive_failure_replays_bound_checkpoint(workspace, monkeypatch):
    project = new_project(workspace,1); model = lp.load(project)
    payload = {'operation':'archive','actionId':'TEST-RECOVER','expectedRevision':'rev'}
    original = pm.write_json
    def fail_registry(path,value):
        if path == pm.registry_path(workspace): raise OSError('合成事务中断')
        original(path,value)
    monkeypatch.setattr(pm,'write_json',fail_registry)
    with pytest.raises(OSError): pm.operate(workspace,model['project_id'],project,'tutor',payload,'rev')
    state = pm.operation_checkpoint(workspace,payload['actionId'])
    assert read_json(state)['state'] == 'paused_error'
    monkeypatch.setattr(pm,'write_json',original)
    assert pm.operate(workspace,model['project_id'],project,'tutor',payload,'changed')['status'] == 'completed'
    assert pm.entry(workspace,model['project_id'])['archived']
    bad = {**payload,'operation':'rename','title':'其他操作'}
    with pytest.raises(ValueError,match='操作 ID 冲突'): pm.operate(workspace,model['project_id'],project,'tutor',bad,'rev')


def test_restore_failure_replays_without_overwriting_and_archived_calls_are_blocked(workspace, monkeypatch):
    project = new_project(workspace,1); model = lp.load(project)
    archive = {'operation':'archive','actionId':'TEST-ARCHIVE-RESTORE','expectedRevision':'rev'}
    pm.operate(workspace,model['project_id'],project,'tutor',archive,'rev')
    with pytest.raises(FileNotFoundError):
        with lp.lock(project): pytest.fail('归档后不能重新创建原目录')
    with pytest.raises(ValueError,match='已归档'): lp.save(project,model,model['render_config'])
    assert not project.exists()
    revision = json_digest(pm.entry(workspace,model['project_id']))
    restore = {'operation':'restore','actionId':'TEST-RESTORE-RECOVER','expectedRevision':revision}
    original = pm.write_json
    def fail_registry(path,value):
        if path == pm.registry_path(workspace): raise OSError('合成恢复中断')
        original(path,value)
    monkeypatch.setattr(pm,'write_json',fail_registry)
    with pytest.raises(OSError): pm.operate(workspace,model['project_id'],project,'tutor',restore,revision)
    monkeypatch.setattr(pm,'write_json',original)
    assert pm.operate(workspace,model['project_id'],project,'tutor',restore,revision)['status'] == 'completed'
    assert not pm.entry(workspace,model['project_id'])['archived']
    assert lp.load(project)['unit_progress'] == model['unit_progress']
    assert not (Path(model['run_dir'])/'.project.lock').exists()


def test_stale_recovery_cannot_undo_later_management(workspace, monkeypatch):
    project = new_project(workspace, 1); model = lp.load(project); pid = model['project_id']
    old = {'operation': 'rename', 'actionId': 'OLD-RENAME', 'expectedRevision': 'rev', 'title': '旧操作'}
    original = pm.write_json
    def fail_registry(path, value):
        if path == pm.registry_path(workspace): raise OSError('合成写入中断')
        original(path, value)
    monkeypatch.setattr(pm, 'write_json', fail_registry)
    with pytest.raises(OSError): pm.operate(workspace, pid, project, 'tutor', old, 'rev')
    monkeypatch.setattr(pm, 'write_json', original)
    new = {**old, 'actionId': 'NEW-RENAME', 'title': '后续操作'}
    pm.operate(workspace, pid, project, 'tutor', new, 'rev')
    with pytest.raises(ValueError, match='版本冲突'):
        pm.operate(workspace, pid, project, 'tutor', old, 'rev')
    assert lp.load(project)['title'] == '后续操作'


def test_pending_podcast_and_republication_do_not_mark_learning(workspace):
    project = new_project(workspace, 1); model = lp.load(project)
    lesson = model['lessons'][0]
    lesson['content'] = {'synthetic': '旧讲解'}
    model['last_learning_at'] = '2000-01-01T00:00:00'
    lp.store(lp.art(project, '学习路线'), model)
    lesson['content'] = {'synthetic': '新讲解'}
    lesson['podcast'] = {'requested': True, 'status': 'pending'}
    pm.mark_learning_activity(project, model)
    assert model['last_learning_at'] == '2000-01-01T00:00:00'
    historical = {'project_id': model['project_id'], 'events': [
        {'at': '2000-01-01T00:00:00', 'event': 'publish_lesson', 'to': 'podcast_required'}]}
    assert pm.timestamps(historical)[1] is None
    historical['events'].append({'at': '2000-01-02T00:00:00', 'event': 'podcast_verified_and_lesson_published'})
    assert pm.timestamps(historical)[1] == '2000-01-02T00:00:00'
    # ID allocation and project creation can fall on different seconds.
    # Historical records without created_at deliberately recover from the ID.
    from utils.scripts.timestamp import parse_filename_timestamp
    assert pm.timestamps(historical)[0] == parse_filename_timestamp(model['project_id'])
    assert pm.timestamps(model)[0] == model['created_at']


def test_navigation_material_coverage_without_planned_lessons(workspace):
    project = new_project(workspace, 1); model = lp.load(project)
    model['lessons'] = []
    model['material_points'] = [{'point_id': 'P-1', 'summary': '合成要点', 'track': 'main', 'unit_ids': ['U-0']}]
    details = pm.detail(model, project, 'navigation')
    assert details['coverage'][0]['summary'] == '合成要点'
    assert details['coverage'][0]['lessons'] == []


def test_archive_rejects_mismatched_identity(workspace):
    project = new_project(workspace, 1)
    request = {'operation': 'archive', 'actionId': 'WRONG-ID', 'expectedRevision': 'rev'}
    with pytest.raises(ValueError, match='ID 不一致'):
        pm.operate(workspace, '19990101T000000', project, 'tutor', request, 'rev')
    assert project.is_dir()


def test_navigation_recovery_rejects_changed_shared_content(workspace, monkeypatch):
    project = new_project(workspace, 1); model = lp.load(project)
    path = Path(model['navigation_json']); pid = 'nav-' + model['project_id']
    payload = {'operation': 'rename', 'actionId': 'NAV-STALE', 'expectedRevision': 'rev', 'title': '旧名称'}
    original = pm.write_json
    def fail_registry(target, value):
        if target == pm.registry_path(workspace): raise OSError('合成导航登记中断')
        original(target, value)
    monkeypatch.setattr(pm, 'write_json', fail_registry)
    with pytest.raises(OSError): pm.operate(workspace, pid, path, 'navigation', payload, 'rev')
    monkeypatch.setattr(pm, 'write_json', original)
    navigation = read_json(path); navigation['title'] = '后续更新'; write_json(path, navigation)
    with pytest.raises(ValueError, match='版本冲突'):
        pm.operate(workspace, pid, path, 'navigation', payload, 'rev')
    assert pm.entry(workspace, pid) == {}
