"""Minimal checks for shared diagrams, tutor publication and public CLI entrypoints."""
from __future__ import annotations

import copy
import re
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts import learning_project as lp
from utils.scripts.mermaid_flowchart import render_flowchart, verify_flowchart, node_id
from utils.scripts.structured_io import read_json

spec = importlib.util.spec_from_file_location('tutor_cli', ROOT / 'skills/interactive-tutor/scripts/run_interactive_tutor.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def model(tmp_path):
    units = [{'unit_id': key, 'title': title, 'prerequisites': prereqs,
              'sequence': i, 'stage': '基础', 'module': '基础', 'importance': 'required', 'learning_objectives': ['说明对应性质']}
             for i, (key, title, prereqs) in enumerate([
                 ('A', '等式的性质', []), ('B', '合并同类项', []),
                 ('C', '解一元一次方程', ['A', 'B']), ('D', '独立知识点', [])])]
    chapters, lessons = lp.initial_plan({'units': units})
    return {'schema_version': '3.0', 'project_id': 'SYNTHETIC-tutor', 'title': '合成课程',
            'navigation_json': str(tmp_path / 'navigation.json'), 'navigation_hash': 'synthetic',
            'workspace_root': str(tmp_path), 'run_dir': str(tmp_path / 'logs/run'),
            'revision': 0, 'state': 'ready', 'current_lesson_id': None,
            'units': units, 'chapters': chapters, 'lessons': lessons,
            'unit_progress': {u['unit_id']: {'status': 'pending', 'skip': False, 'mastery': None, 'evidence_refs': []} for u in units},
            'applied_actions': [], 'candidate_orders': {}, 'notes': [], 'errors': [],
            'events': [], 'question_discussions': []}


@pytest.fixture
def config():
    return {'lesson': {'max_new_units': 3, 'max_multiple_choice_questions': 3, 'max_open_ended_questions': 2},
            'mastery': {'good_min': 80, 'medium_min': 60}, 'podcast': {'enabled': False}}


def test_multiple_prerequisites_isolated_nodes_and_determinism(model, config):
    lp.rebuild(model, config)
    text = lp.roadmap_md(model)
    lp.verify_roadmap_flowcharts(model, text)
    assert text.count('```mermaid') == 2
    for graph in lp.roadmap_graphs(model):
        diagram = render_flowchart(graph)
        shuffled = {'nodes': list(reversed(graph['nodes'])), 'edges': list(reversed(graph['edges']))}
        assert render_flowchart(shuffled) == diagram
        assert diagram.count(' --> ') == 2
        assert len(graph['nodes']) == 4


def test_successful_save_preserves_existing_model_references(tmp_path, model, config):
    lesson, unit, progress = model['lessons'][0], model['units'][0], model['unit_progress']['A']
    lesson_list = model['lessons']
    lp.save(tmp_path / 'outputs/project', model, config)
    assert model['lessons'] is lesson_list and model['lessons'][0] is lesson
    assert model['units'][0] is unit and model['unit_progress']['A'] is progress
    assert lesson['number'] == '1.1'


def test_label_escaping_and_stable_ids():
    graph = {'nodes': [{'id': 'end unsafe ID', 'label': '中文 "[]{}<> & # `\\\n```mermaid', 'status': '待学习'}], 'edges': []}
    diagram = render_flowchart(graph)
    assert '#34;' in diagram and '#96;' in diagram and '#60;' in diagram
    assert len(diagram.splitlines()) == 4
    assert node_id(graph['nodes'][0]['id']) in diagram
    verify_flowchart(graph, diagram)


@pytest.mark.parametrize('graph', [
    {'nodes': [{'id': 'A', 'label': 'A', 'status': '待学习'}] * 2, 'edges': []},
    {'nodes': [{'id': 'A', 'label': 'A', 'status': '待学习'}], 'edges': [{'from': 'A', 'to': 'missing'}]},
    {'nodes': [{'id': 'A', 'label': 'A', 'status': '待学习'}], 'edges': [{'from': 'A', 'to': 'A'}]},
    {'nodes': [{'id': x, 'label': x, 'status': '待学习'} for x in ('A', 'B')],
     'edges': [{'from': 'A', 'to': 'B'}, {'from': 'B', 'to': 'A'}]},
])
def test_invalid_graphs_rejected(graph):
    with pytest.raises(ValueError):
        render_flowchart(graph)


def test_empty_graph_and_parallel_edge_deduplication():
    empty = {'nodes': [], 'edges': []}
    verify_flowchart(empty, render_flowchart(empty))
    graph = {'nodes': [{'id': x, 'label': x, 'status': '待学习'} for x in ('A', 'B')],
             'edges': [{'from': 'A', 'to': 'B'}] * 2}
    diagram = render_flowchart(graph)
    assert diagram.count(' --> ') == 1
    verify_flowchart(graph, diagram)


def test_dependency_status_and_plan_updates_detect_stale_diagrams(model, config):
    lp.rebuild(model, config)
    old = lp.roadmap_md(model)
    model['units'][2]['prerequisites'] = ['B']
    model['unit_progress']['A']['status'] = 'skipped'
    model['unit_progress']['A']['skip'] = True
    model['lessons'][0]['status'] = 'skipped'
    lp.plan_patch(model, [{'operation': 'update', 'lesson_id': model['lessons'][3]['lesson_id'],
                           'patch': {'title': '新版独立知识点', 'track': 'branch'}}])
    lp.rebuild(model, config)
    with pytest.raises(ValueError):
        lp.verify_roadmap_flowcharts(model, old)
    new = lp.roadmap_md(model)
    lp.verify_roadmap_flowcharts(model, new)
    assert '已跳过' in new and '支线' in new and '新版独立知识点' in new
    assert new.count(' --> ') == 2  # One edge in each independent graph.


def test_archived_course_excluded_but_skipped_nodes_retained(model, config):
    old_lesson = copy.deepcopy(model['lessons'][3])
    old_lesson.update(lesson_id='HISTORY', section_number=5, archived=True, title='合成旧课程')
    model['lessons'].append(old_lesson)
    model['unit_progress']['D'].update(status='skipped', skip=True)
    lp.rebuild(model, config)
    text = lp.roadmap_md(model)
    assert '合成旧课程' not in text
    assert '独立知识点｜已跳过' in text
    lp.verify_roadmap_flowcharts(model, text)


def test_publish_verify_rollback_and_resume(tmp_path, model, config, monkeypatch):
    project = tmp_path / 'outputs/project'
    monkeypatch.setattr(lp, 'verify_backup', lambda *a, **k: None)
    lp.store(Path(model['navigation_json']), {})
    lp.save(project, model, config)
    assert lp.verify(project, lp.load(project), config)['status'] == 'verified'
    checkpoint = Path(model['run_dir']) / 'document-render/state.json'
    assert read_json(checkpoint)['stateHistory'] == [
        'prepared', 'validating_graphs', 'rendering_documents', 'verifying_documents', 'publishing', 'completed']
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    old_revision = model['revision']
    old_state = (Path(model['run_dir']) / 'state.json').read_bytes()
    import utils.scripts.structured_io as io
    replace = io.os.replace
    replacements = 0

    def fail_second(source, target):
        nonlocal replacements
        if Path(target).is_relative_to(project):
            replacements += 1
            if replacements == 2:
                raise OSError('SYNTHETIC publication failure')
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(io.os, 'replace', fail_second)
        model['units'][2]['prerequisites'] = ['A']
        with pytest.raises(OSError):
            lp.save(project, model, config)
    assert all(p.read_bytes() == content for p, content in before.items())
    assert model['revision'] == old_revision
    assert (Path(model['run_dir']) / 'state.json').read_bytes() == old_state
    assert read_json(checkpoint)['state'] == 'paused_error'
    failed_history = read_json(checkpoint)['stateHistory']
    recovered = lp.load(project)
    recovered['units'][2]['prerequisites'] = ['A']
    lp.save(project, recovered, config)
    assert lp.verify(project, lp.load(project), config)['status'] == 'verified'
    assert read_json(checkpoint)['state'] == 'completed'
    assert read_json(checkpoint)['stateHistory'][:len(failed_history)] == failed_history
    assert read_json(checkpoint)['error'] is None
    route = project / '学习路线.md'
    route.write_text(route.read_text(encoding='utf-8').replace(' --> ', ' -.-> ', 1), encoding='utf-8')
    with pytest.raises(ValueError):
        lp.verify(project, lp.load(project), config)


def test_public_commands_help():
    commands = cli.parser()._subparsers._group_actions[0].choices
    for command in commands:
        result = subprocess.run([sys.executable, str(ROOT / 'skills/interactive-tutor/scripts/cli.py'), command, '--help'],
                                capture_output=True, text=True, encoding='utf-8')
        assert result.returncode == 0, (command, result.stderr)
        assert 'usage:' in result.stdout


@pytest.mark.parametrize('command', ['status', 'resume', 'report', 'verify', 'plan', 'skip', 'restore', 'questions', 'supply-navigation'])
def test_public_project_commands_publish_synced_diagrams(command, tmp_path, model, config, monkeypatch):
    project = tmp_path / 'outputs/project'
    lp.store(Path(model['navigation_json']), {})
    lp.save(project, model, config)
    monkeypatch.setattr(cli, 'project_path', lambda args: project)
    receipt = {'config': config, 'message': ''}
    monkeypatch.setattr(cli, 'freeze_config', lambda *a: receipt)
    monkeypatch.setattr(cli, 'acknowledge_config', lambda *a: None)
    monkeypatch.setattr(lp, 'context', lambda *a, **k: (lp.load(project), receipt))
    monkeypatch.setattr(lp, 'navigation_preflight', lambda *a, **k: {})
    monkeypatch.setattr(lp, 'verify_input_snapshots', lambda *a: None)
    monkeypatch.setattr(lp, 'verify_backup', lambda *a, **k: None)
    # Only external navigation/material gates are stubbed; tutor commands and save are real.
    args = [command, '--project-dir', str(project)]
    if command == 'plan':
        patch = tmp_path / 'patch.json'
        lp.store(patch, {'base_revision': model['revision'], 'operations': [
            {'operation': 'update', 'lesson_id': model['lessons'][3]['lesson_id'], 'patch': {'title': '新课程标题'}}]})
        args += ['--patch', str(patch)]
    elif command in ('skip', 'restore'):
        args += ['--target-type', 'unit', '--target-id', 'D']
    elif command == 'questions':
        args += ['--question', '什么是等式？', '--answer', '表示相等关系。']
    elif command == 'supply-navigation':
        nav = {'units': copy.deepcopy(model['units']), 'planning_profile': {'ordering': {}}}
        nav['units'][2]['prerequisites'] = ['A']
        lp.store(Path(model['navigation_json']), nav)
        monkeypatch.setattr(lp, 'navigation_preflight', lambda *a, **k: nav)
        monkeypatch.setattr(lp, 'ensure_backup', lambda *a, **k: None)
        args += ['--navigation', model['navigation_json']]
    result = cli.execute(cli.parser().parse_args(args))
    assert result['status'] in {'ready', 'verified'}
    current = lp.load(project)
    lp.verify_roadmap_flowcharts(current, (project / '学习路线.md').read_text(encoding='utf-8'))
    assert lp.verify(project, current, config)['status'] == 'verified'


@pytest.mark.parametrize('answer_mode', ['chat', 'file'])
def test_public_lesson_review_notes_and_question_gate(answer_mode, tmp_path, model, config, monkeypatch):
    project = tmp_path / 'outputs/project'
    nav = {'planning_profile': {}, 'lesson_generation_policy': {}}
    lp.store(Path(model['navigation_json']), nav)
    lp.save(project, model, config)
    receipt = {'config': config, 'message': ''}
    monkeypatch.setattr(cli, 'project_path', lambda args: project)
    monkeypatch.setattr(cli, 'acknowledge_config', lambda *a: None)
    monkeypatch.setattr(lp, 'context', lambda *a, **k: (lp.load(project), receipt))
    monkeypatch.setattr(lp, 'verify_backup', lambda *a, **k: None)
    monkeypatch.setattr(lp, 'evidence_for', lambda model, lesson, root: [
        {'evidence_id': 'E1', 'unit_id': lesson['teaches_unit_ids'][0]}])

    def call(command, *options):
        result = cli.execute(cli.parser().parse_args([command, '--project-dir', str(project), *options]))
        current = lp.load(project)
        lp.verify_roadmap_flowcharts(current, (project / '学习路线.md').read_text(encoding='utf-8'))
        return result

    assert call('prepare-lesson')['status'] == 'lesson_decision_required'
    decision = read_json(Path(model['run_dir']) / 'lesson-decision.template.json')
    decision['questions'] = decision['questions'][:1]
    decision.update(overview='认识等式的性质。', key_points=['等式两边同加一个数仍相等。'])
    decision['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '等式两边同加一个数仍相等。'
    decision['teaching_points'][0]['blocks'][0]['coverage'] = 'explained'
    decision['questions'][0].update(prompt='等式两边同加一个数会怎样？',
                                  options=['仍相等', '左边较大', '右边较大', '必为零'],
                                  reference_answer='仍相等', expected_points={'A': ['两边仍相等']})
    decision_path = tmp_path / 'decision.json'
    lp.store(decision_path, decision)
    pending = call('publish-lesson', '--decision', str(decision_path))
    assert pending['status'] == 'teaching_review_required'
    quality = read_json(Path(pending['quality_template']))
    quality['short_complete_reason'] = '正文完整给出等式性质的条件、操作与结果，合成目标仅要求说明这条性质。'
    for item in quality['items']: item.update(judgment='sufficient', reason='本块说明两边同加的条件及相等结论。')
    quality_path = tmp_path / 'teaching-review.json'; lp.store(quality_path, quality)
    assert call('publish-lesson', '--decision', str(decision_path), '--quality-review', str(quality_path))['status'] == 'awaiting_answer'
    assert '正在学习' in (project / '学习路线.md').read_text(encoding='utf-8')
    if answer_mode == 'chat':
        answers = tmp_path / 'answers.json'
        lp.store(answers, {'Q-1': 'A'})
        assert call('submit-chat-answer', '--answers', str(answers))['status'] == 'review_decision_required'
    else:
        lesson = lp.load(project)['lessons'][0]
        path = project / '课程' / lesson['filename']
        text = path.read_text(encoding='utf-8').replace('⟦请将这行替换为你的回答，可分段填写⟧', 'A')
        path.write_text(text, encoding='utf-8')
        assert call('check-answers')['status'] == 'review_decision_required'
    review_path = tmp_path / 'review.json'
    review = read_json(Path(model['run_dir']) / 'review.template.json')
    review.update(strengths='能正确应用等式性质。', weaknesses='本题未发现不足。')
    review['scores'][0]['feedback'] = '回答正确。'
    lp.store(review_path, review)
    assert call('review-answers', '--review', str(review_path))['status'] == 'ready'
    assert '已学习' in (project / '学习路线.md').read_text(encoding='utf-8')
    assert call('questions', '--question', '可以同减吗？', '--answer', '可以。')['status'] == 'ready'
    draft_path = tmp_path / 'draft.json'
    lp.store(draft_path, {'lesson_id': decision['lesson_id'], 'title': '等式性质笔记',
                         'location': '核心要点', 'background': '保持两边相等。', 'text': '同加或同减均保持相等。'})
    prepared = call('note-prepare', '--draft', str(draft_path))
    assert prepared['status'] == 'awaiting_note_confirmation'
    assert call('note-confirm', '--draft-hash', prepared['draft_hash'], '--confirmed-by', 'SYNTHETIC-user')['status'] == 'note_saved'
    assert lp.verify(project, lp.load(project), config)['status'] == 'verified'


@pytest.mark.parametrize('interrupted_state', ['validating_graphs', 'rendering_documents', 'verifying_documents', 'publishing'])
def test_interrupted_document_checkpoint_preserves_history(interrupted_state, tmp_path, model, config):
    from utils.scripts.workflow_checkpoint import WorkflowCheckpoint
    directory = Path(model['run_dir']) / 'document-render'
    flow = WorkflowCheckpoint(lp.DOCUMENT_TRANSITIONS, directory)
    for state in ('validating_graphs', 'rendering_documents', 'verifying_documents', 'publishing'):
        flow.move(state)
        if state == interrupted_state:
            break
    history = read_json(directory / 'state.json')['stateHistory']
    lp.save(tmp_path / 'outputs/project', model, config)
    saved = read_json(directory / 'state.json')
    assert saved['stateHistory'][:len(history)] == history
    assert saved['stateHistory'][len(history):len(history)+2] == ['paused_error', 'validating_graphs']
    assert saved['state'] == 'completed'


def test_invalid_checkpoint_does_not_overwrite_evidence(tmp_path, model, config):
    checkpoint = Path(model['run_dir']) / 'document-render/state.json'
    lp.store(checkpoint, {'state': 'publishing', 'stateHistory': ['prepared', 'publishing']})
    before = checkpoint.read_bytes()
    with pytest.raises(ValueError, match='检查点'):
        lp.save(tmp_path / 'outputs/project', model, config)
    assert checkpoint.read_bytes() == before
    assert model['revision'] == 0


def test_resume_upgrades_legacy_roadmap_and_real_skip_restore(tmp_path, model, config, monkeypatch):
    project = tmp_path / 'outputs/project'
    lp.store(Path(model['navigation_json']), {})
    lp.save(project, model, config)
    route = project / '学习路线.md'
    text = route.read_text(encoding='utf-8')
    route.write_text(re.sub(r'```mermaid\n.*?```\n', '', text, flags=re.S), encoding='utf-8')
    receipt = {'config': config, 'message': ''}
    monkeypatch.setattr(cli, 'project_path', lambda args: project)
    monkeypatch.setattr(cli, 'acknowledge_config', lambda *a: None)
    monkeypatch.setattr(lp, 'context', lambda *a, **k: (lp.load(project), receipt))
    monkeypatch.setattr(lp, 'verify_backup', lambda *a, **k: None)
    with pytest.raises(ValueError, match='双思维导图'):
        lp.verify(project, lp.load(project), config)
    assert cli.execute(cli.parser().parse_args(['resume', '--project-dir', str(project)]))['status'] == 'ready'
    assert lp.verify(project, lp.load(project), config)['status'] == 'verified'
    for command, expected in [('skip', '已跳过'), ('restore', '待学习')]:
        cli.execute(cli.parser().parse_args([command, '--project-dir', str(project),
                    '--target-type', 'unit', '--target-id', 'D']))
        lp.verify_roadmap_flowcharts(lp.load(project), route.read_text(encoding='utf-8'))
        assert f'独立知识点｜{expected}' in route.read_text(encoding='utf-8')


def test_public_creation_and_selection_interfaces(tmp_path, model, config, monkeypatch):
    from utils.scripts.timestamp import iso_timestamp
    from utils.scripts import learning_project_selection as selection
    nav = {'title': '合成课程', 'sources': [], 'units': model['units'], 'planning_profile': {'ordering': {}}}
    nav_path = Path(model['navigation_json'])
    lp.store(nav_path, nav)
    receipt = {'config': config, 'message': ''}
    monkeypatch.setattr(lp, 'navigation_preflight', lambda *a, **k: nav)
    monkeypatch.setattr(lp, 'freeze_config', lambda *a: receipt)
    monkeypatch.setattr(lp, 'ensure_backup', lambda *a, **k: None)
    request = tmp_path / 'request.json'
    assert cli.execute(cli.parser().parse_args(['init-request', '--file', str(request), '--input', str(nav_path)]))['status'] == 'prepared'
    glossary = tmp_path / 'glossary.md'
    assert cli.execute(cli.parser().parse_args(['init-bilingual-glossary', '--file', str(glossary)]))['status'] == 'prepared'
    created = cli.execute(cli.parser().parse_args(['start', '--root', str(tmp_path), '--request', str(request)]))
    assert created['status'] == 'awaiting_name_confirmation'
    created = cli.execute(cli.parser().parse_args(['confirm-project-name', '--creation-dir', created['creation_dir'], '--name', created['suggested_name'], '--proposal-sha256', created['proposal_sha256']]))
    assert created['status'] == 'ready'
    project = Path(created['project_dir'])
    current = lp.load(project)
    lp.verify_roadmap_flowcharts(current, (project / '学习路线.md').read_text(encoding='utf-8'))
    entry = {'candidate_id': 'NAV-' + lp.json_digest('fixture')[:16], 'run_id': current['project_id'],
             'title': nav['title'], 'updated_at': iso_timestamp(), 'navigation_path': nav_path.relative_to(tmp_path).as_posix(),
             'navigation_status': 'completed', 'availability': 'available', 'reason': '', 'tutor_projects': [
                 {'project_id': current['project_id'], 'title': current['title'], 'state': current['state'],
                  'revision': current['revision'], 'project_dir': project.relative_to(tmp_path).as_posix()}]}
    catalog = {'schema_version': '1.0', 'catalog_sha256': lp.json_digest(entry), 'entries': [entry]}
    monkeypatch.setattr(selection, 'discover', lambda *a: catalog)
    monkeypatch.setattr(selection, 'valid_navigation', lambda *a: nav)
    monkeypatch.setattr(cli, 'project_path', lambda args: project)
    monkeypatch.setattr(lp, 'context', lambda *a, **k: (lp.load(project), receipt))
    monkeypatch.setattr(cli, 'acknowledge_config', lambda *a: None)
    for entry_command in ('list-projects', 'start'):
        listed = cli.execute(cli.parser().parse_args([entry_command, '--root', str(tmp_path)]))
        assert listed['status'] == 'awaiting_project_selection'
        directory = listed['selection_dir']
        selected = cli.execute(cli.parser().parse_args(['select-project', '--selection-dir', directory,
                              '--choice', '1', '--catalog-sha256', listed['catalog_sha256']]))
        assert selected['status'] == 'awaiting_tutor_selection'
        assert cli.execute(cli.parser().parse_args(['resume-selection', '--selection-dir', directory]))['status'] == 'awaiting_tutor_selection'
        resolved = cli.execute(cli.parser().parse_args(['select-tutor', '--selection-dir', directory,
                              '--choice', '1', '--catalog-sha256', listed['catalog_sha256']]))
        assert resolved['status'] == 'project_resolved' and resolved['result']['status'] == 'ready'
        lp.verify_roadmap_flowcharts(lp.load(project), (project / '学习路线.md').read_text(encoding='utf-8'))
