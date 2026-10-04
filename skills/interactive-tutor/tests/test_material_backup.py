"""Real-source CLI smoke tests: backup approval, frozen teaching and explicit updates."""
from pathlib import Path
import pytest

from .test_interactive_tutor import workspace, navigation, runner, ROOT, decision
from utils.scripts import learning_project as lp
from utils.scripts.learning_material_backup import backup_plan, ensure_backup, verify_backup, BackupApprovalRequired
from utils.scripts.learning_navigation import render_navigation_markdown
from utils.scripts.learning_navigation_bundle import render_source_fragment_markdown
from utils.scripts.markdown_structure import extract_markdown_structure, sha256_file
from utils.scripts.structured_io import read_json, write_json, json_digest


def source_navigation(root, text=None):
    path = root / 'navigation.json'
    if not path.exists():
        path = navigation(root, 1)
    source = root / 'materials/book.md'
    source.parent.mkdir(exist_ok=True)
    image = source.parent / 'images/figure.png'
    image.parent.mkdir(exist_ok=True)
    if not image.exists():
        image.write_bytes(b'synthetic-image')
    source.write_bytes((text or '# 基础\r\n\r\n原始材料内容\r\n![图](images/figure.png)\r\n').encode('utf-8-sig'))
    structure = extract_markdown_structure(source, root=root, preview_chars=0)
    nav = read_json(path)
    sid = structure['source_id']
    nav['sources'] = [{'source_id': sid, 'path': 'materials/book.md', 'sha256': structure['sha256'],
                       'title': '测试材料', 'size_bytes': source.stat().st_size, 'author': '', 'edition': '',
                       'theme': '测试', 'content_tendency': [], 'scan_confidence': 'high'}]
    unit = nav['units'][0]
    heading = structure['headings'][0]
    locator = {'source_id': sid, 'file': 'materials/book.md', 'source_sha256': structure['sha256'],
               'heading_text': heading['title'], 'heading_level': heading['level'], 'heading_occurrence': heading['occurrence'],
               'parent_heading_chain': heading['parent_heading_chain'], 'start_line_hint': heading['start_line'],
               'end_line_hint': heading['end_line'], 'end_before_heading': None, 'locator_status': 'resolved'}
    unit.update(unit_type='source_reading', source=locator)
    unit['visual_references'] = [{**figure, 'source_id': sid, 'source_path': 'materials/book.md',
                                  'purpose': '理解结构', 'placement_hint': '讲解后', 'locator_status': 'valid'}
                                 for figure in structure['images']]
    from utils.scripts import adaptive_assessment as aa
    nav['planning_profile']['assessment']['graph_revision'] = aa.graph_revision(nav['units'])
    write_json(path, nav)
    path.with_suffix('.md').write_text(render_navigation_markdown(nav), encoding='utf-8')
    fragment = {'schema_version': '2.0', 'source': {k: nav['sources'][0][k] for k in ('source_id', 'path', 'sha256')},
                'learning_points': [{'point_id': unit['unit_id'], 'title': unit['title'], 'difficulty': unit['difficulty'],
                                     'purpose': unit['purpose'], 'source_locator': locator}], 'relationship_records': []}
    write_json(path.with_suffix('.sources') / f'{sid}.json', fragment)
    (path.with_suffix('.sources') / f'{sid}.md').write_text(render_source_fragment_markdown(fragment), encoding='utf-8')
    return path, source, image


def test_cli_backup_and_teaching_survive_source_removal(workspace, capsys):
    nav, source, image = source_navigation(workspace)
    original = source.read_bytes(); image_bytes = image.read_bytes()
    request = workspace / 'request.json'
    write_json(request, {'schema_version': '3.0', 'input_paths': [str(nav)], 'interaction_mode': 'file_driven', 'project_dir': None})
    assert runner.main(['start', '--root', str(workspace), '--request', str(request)]) == 3
    import json
    pending = json.loads(capsys.readouterr().out)
    assert pending['status'] == 'awaiting_name_confirmation'
    assert runner.main(['confirm-project-name', '--creation-dir', pending['creation_dir'], '--name', pending['suggested_name'], '--proposal-sha256', pending['proposal_sha256']]) == 3
    pending = json.loads(capsys.readouterr().out)
    project = Path(pending['project_dir'])
    assert not (project / '学习材料').exists()
    target = ['--project-dir', str(project)]
    assert runner.main(['resume', *target, '--backup-plan-sha256', pending['backup_plan_sha256']]) == 0
    capsys.readouterr()
    nav_data = read_json(nav)
    paths = verify_backup(workspace, project, nav_data)
    assert paths['materials/book.md'].read_bytes() == original == source.read_bytes()
    assert (paths['materials/book.md'].parent / 'images/figure.png').read_bytes() == image_bytes
    source.unlink(); image.unlink()
    assert runner.main(['verify', *target]) == 0; capsys.readouterr()
    assert runner.main(['prepare-lesson', *target]) == 0; capsys.readouterr()
    model = lp.load(project)
    evidence = read_json(Path(model['run_dir']) / 'evidence.json')['evidence'][0]
    assert '原始材料内容' in evidence['text']
    assert '/artifacts/material-originals/' in evidence['source_path']
    paths['materials/book.md'].write_bytes(b'corrupt')
    assert runner.main(['verify', *target]) == 4; capsys.readouterr()


def test_explicit_update_retains_versions(workspace, capsys):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'
    plan = backup_plan(workspace, project, read_json(nav))
    receipt = lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    assert receipt['status'] == 'ready'
    old_path = verify_backup(workspace, project, read_json(nav))['materials/book.md']
    nav, source, image = source_navigation(workspace, '# 基础\n新版本材料\n![图](images/figure.png)\n')
    target = ['--project-dir', str(project), '--navigation', str(nav)]
    assert runner.main(['supply-navigation', *target]) == 3
    import json
    pending = json.loads(capsys.readouterr().out)
    assert old_path.is_file()
    assert runner.main(['supply-navigation', *target, '--backup-plan-sha256', pending['backup_plan_sha256']]) == 0
    capsys.readouterr()
    new_path = verify_backup(workspace, project, read_json(nav))['materials/book.md']
    assert new_path != old_path and old_path.is_file()
    assert new_path.read_bytes() == source.read_bytes()
    assert list((project / 'artifacts/material-backup-history').glob('*.json'))


def test_resources_conflicts_and_recovery(workspace, monkeypatch):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'
    run = workspace / 'logs/backup-test'
    data = read_json(nav); plan = backup_plan(workspace, project, data)
    with pytest.raises(BackupApprovalRequired): ensure_backup(workspace, project, data, run)
    target = project / plan['materials'][0]['backup_path']
    target.parent.mkdir(parents=True); target.write_bytes(b'existing unrelated data')
    with pytest.raises(ValueError, match='冲突'):
        ensure_backup(workspace, project, data, run, json_digest(plan))
    assert source.read_bytes() != target.read_bytes()
    assert read_json(run / 'material-backup-state.json')['state'] == 'paused_backup_conflict'
    target.unlink()
    ensure_backup(workspace, project, data, run)
    assert read_json(run / 'material-backup-state.json')['state'] == 'backup_ready'
    source.write_text('# 基础\n[附件](../../outside.pdf)\n', encoding='utf-8')
    data['sources'][0]['sha256'] = sha256_file(source)
    with pytest.raises(ValueError): backup_plan(workspace, project, data)


def test_local_reference_and_html_resources(workspace):
    source = workspace / 'book.md'
    for name in ('one.png', 'two.pdf', 'three.png', 'four.png'):
        (workspace / name).write_bytes(b'synthetic')
    source.write_text('# 资源\n![一][image]\n[附件](two.pdf)\n[image]: one.png\n<img src="three.png">\n![[four.png]]\n![远程](https://example.invalid/a.png)\n', encoding='utf-8')
    from utils.scripts.learning_material_backup import collect_files
    assert len(collect_files(workspace, source)) == 5
    source.write_text('# 绝对引用\n![图片](/one.png)\n', encoding='utf-8')
    with pytest.raises(ValueError, match='绝对本地引用'):
        collect_files(workspace, source)


def test_legacy_project_gets_backup_on_resume(workspace, capsys):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'
    plan = backup_plan(workspace, project, read_json(nav))
    lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    model = lp.load(project)
    model.pop('material_project')
    write_json(lp.art(project, '学习路线'), model)
    (project / 'artifacts/学习材料备份.json').unlink()
    (Path(model['run_dir']) / 'material-backup-approval.json').unlink()
    target = ['--project-dir', str(project)]
    assert runner.main(['resume', *target]) == 3
    import json
    pending = json.loads(capsys.readouterr().out)
    assert runner.main(['resume', *target, '--backup-plan-sha256', pending['backup_plan_sha256']]) == 0
    capsys.readouterr()
    assert lp.load(project)['material_project'] == str(project)
    verify_backup(workspace, project, read_json(nav))


def test_interrupted_copy_resumes_with_previous_approval(workspace, monkeypatch):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'; run = workspace / 'logs/backup-test'
    data = read_json(nav); plan = backup_plan(workspace, project, data)
    from utils.scripts import learning_material_backup as backup
    original_copy = backup.shutil.copyfile
    calls = []
    def interrupted(src, target):
        calls.append(src)
        if len(calls) == 2:
            Path(target).write_bytes(b'partial')
            raise OSError('synthetic interrupted copy')
        return original_copy(src, target)
    monkeypatch.setattr(backup.shutil, 'copyfile', interrupted)
    with pytest.raises(OSError): ensure_backup(workspace, project, data, run, json_digest(plan))
    assert not (project / 'artifacts/学习材料备份.json').exists()
    assert read_json(run / 'material-backup-state.json')['state'] == 'paused_backup_error'
    monkeypatch.setattr(backup.shutil, 'copyfile', original_copy)
    ensure_backup(workspace, project, data, run)
    verify_backup(workspace, project, data)
    assert not list((project / 'artifacts/material-backup-staging').iterdir())


def test_resource_only_update_requires_explicit_sync(workspace, capsys):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'
    data = read_json(nav); plan = backup_plan(workspace, project, data)
    lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    old_path = verify_backup(workspace, project, data)['materials/book.md']
    image.write_bytes(b'updated local image')
    lp.context(project)  # Normal teaching retains the previous resource version.
    assert (old_path.parent / 'images/figure.png').read_bytes() == b'synthetic-image'
    target = ['--project-dir', str(project), '--navigation', str(nav)]
    assert runner.main(['supply-navigation', *target]) == 3
    import json
    pending = json.loads(capsys.readouterr().out)
    assert runner.main(['supply-navigation', *target, '--backup-plan-sha256', pending['backup_plan_sha256']]) == 0
    capsys.readouterr()
    new_path = verify_backup(workspace, project, data)['materials/book.md']
    assert new_path != old_path
    assert (new_path.parent / 'images/figure.png').read_bytes() == image.read_bytes()


def test_used_resources_ignore_code_and_unused_definitions(workspace):
    from utils.scripts.learning_material_backup import collect_files
    source = workspace / 'book.md'
    for name in ('image(1).png', 'ref.png', 'nested.md', 'nested.png'):
        (workspace / name).write_bytes(b'synthetic')
    (workspace / 'nested.md').write_text('# 嵌套\n![图](nested.png)\n', encoding='utf-8')
    source.write_text('# 正文\n[![图](image(1).png)](nested.md)\n![引用][]\n[引用]: ref.png "说明"\n[unused]: missing.pdf\n`[例子](missing-inline.pdf)`\n````md\n```\n[例子](missing-fenced.pdf)\n````\n<!-- [例子](missing-comment.pdf) -->\n', encoding='utf-8')
    assert {f['original_path'] for f in collect_files(workspace, source)} == {'book.md', 'image(1).png', 'ref.png', 'nested.md', 'nested.png'}
    source.write_text('# 正文\n\n    [缩进代码](missing.pdf)\n\n- 正文列表\n    [附件](nested.md)\n\n> ```md\n> [引用代码](missing.pdf)\n> ```\n', encoding='utf-8')
    assert {f['original_path'] for f in collect_files(workspace, source)} == {'book.md', 'nested.md', 'nested.png'}


def test_navigation_publish_failure_rolls_back_manifest_and_views(workspace, monkeypatch, capsys):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'; original = read_json(nav)
    plan = backup_plan(workspace, project, original)
    lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    updated = dict(original, title='新版导航测试')
    new_path = workspace / 'new-navigation.json'
    write_json(new_path, updated)
    new_path.with_suffix('.md').write_text(render_navigation_markdown(updated), encoding='utf-8')
    import shutil
    shutil.copytree(nav.with_suffix('.sources'), new_path.with_suffix('.sources'))
    new_plan = backup_plan(workspace, project, updated)
    from utils.scripts import structured_io
    replace = structured_io.os.replace; failed = []
    def fail_view(src, target):
        if Path(target) == project / '总结.md' and not failed:
            failed.append(True); raise OSError('synthetic view publication failure')
        return replace(src, target)
    monkeypatch.setattr(structured_io.os, 'replace', fail_view)
    target = ['--project-dir', str(project), '--navigation', str(new_path), '--backup-plan-sha256', json_digest(new_plan)]
    assert runner.main(['supply-navigation', *target]) == 5
    capsys.readouterr()
    for path, content in before.items():
        assert path.read_bytes() == content
    lp.navigation_preflight(workspace, nav, material_project=project)
    monkeypatch.setattr(structured_io.os, 'replace', replace)
    assert runner.main(['supply-navigation', *target]) == 0
    capsys.readouterr()
    assert lp.load(project)['navigation_hash'] == json_digest(updated)


def test_input_cannot_be_overwritten_by_backup_controls(workspace):
    nav, source, image = source_navigation(workspace)
    run = workspace / 'logs/protected-backup'; run.mkdir(parents=True)
    state = run / 'material-backup-state.json'; state.write_bytes(b'synthetic source attachment')
    source.write_text('# 基础\n[附件](../logs/protected-backup/material-backup-state.json)\n', encoding='utf-8')
    data = read_json(nav); data['sources'][0]['sha256'] = sha256_file(source)
    original = state.read_bytes()
    with pytest.raises(ValueError, match='禁止修改输入'):
        ensure_backup(workspace, workspace / 'project', data, run)
    assert state.read_bytes() == original


def test_backup_state_rejects_illegal_transition(workspace):
    from utils.scripts.learning_material_backup import backup_stage
    run = workspace / 'logs/state-test'
    with pytest.raises(ValueError, match='非法备份状态迁移'):
        backup_stage(run, 'backup_ready')
    backup_stage(run, 'planning_backup')
    with pytest.raises(ValueError, match='非法备份状态迁移'):
        backup_stage(run, 'copying_materials')


def test_same_names_use_distinct_sources_and_complete_approval_list(workspace):
    nav, source, image = source_navigation(workspace)
    other = workspace / 'other/book.md'; other.parent.mkdir()
    other.write_text('# 其他材料\n内容\n', encoding='utf-8')
    data = read_json(nav)
    data['sources'].append({'source_id': 'SRC-other', 'path': 'other/book.md', 'sha256': sha256_file(other)})
    project = workspace / 'project'; run = workspace / 'logs/two-sources'
    plan = backup_plan(workspace, project, data)
    with pytest.raises(BackupApprovalRequired) as pending:
        ensure_backup(workspace, project, data, run)
    assert 'artifacts/学习材料备份.json' in pending.value.receipt['planned_paths']
    ensure_backup(workspace, project, data, run, json_digest(plan))
    paths = verify_backup(workspace, project, data)
    assert paths['other/book.md'].read_bytes() == other.read_bytes()
    assert paths['materials/book.md'] != paths['other/book.md']


def test_initial_publish_failure_has_recoverable_project(workspace, monkeypatch, capsys):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'; raw = source.read_bytes()
    plan = backup_plan(workspace, project, read_json(nav))
    original_write = lp.write_text_transaction
    def fail_manifest(updates):
        if project / 'artifacts/学习材料备份.json' in updates:
            raise OSError('synthetic initial publication failure')
        return original_write(updates)
    monkeypatch.setattr(lp, 'write_text_transaction', fail_manifest)
    with pytest.raises(OSError):
        lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    assert lp.load(project)['state'] == 'backup_required'
    assert source.read_bytes() == raw
    monkeypatch.setattr(lp, 'write_text_transaction', original_write)
    assert runner.main(['resume', '--project-dir', str(project)]) == 0
    capsys.readouterr()
    assert lp.load(project)['state'] == 'ready'


def test_course_image_pointer_uses_backup_and_rejects_wrong_source(workspace, monkeypatch):
    nav, source, image = source_navigation(workspace)
    project = workspace / 'project'; data = read_json(nav)
    plan = backup_plan(workspace, project, data)
    lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name="合成测试课程")
    source.unlink(); image.unlink()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == source or original_is_symlink(path))
    model, receipt = lp.context(project)
    content = decision(project, model, receipt['config'])
    result = lp.publish(project, model, receipt['config'], content)
    assert '学习材料/' in Path(result['lesson']).read_text(encoding='utf-8')
    data['units'][0]['visual_references'][0]['source_path'] = 'unregistered.md'
    from utils.scripts.learning_navigation import validate_navigation
    paths = verify_backup(workspace, project, read_json(nav))
    with pytest.raises(ValueError, match='图片来源'):
        validate_navigation(data, schema_path=ROOT/'utils/references/learning-navigation-v2.schema.json', root=workspace, material_paths=paths)
