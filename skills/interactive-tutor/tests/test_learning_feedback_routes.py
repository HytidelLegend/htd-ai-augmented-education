"""New lesson protocol, forks and coverage use synthetic examples only."""
import copy
from pathlib import Path
import pytest
from .test_interactive_tutor import workspace, new_project, runner, import_module, ROOT, acknowledge_feedback
from utils.scripts import learning_project as lp, learning_route as route
from utils.scripts.learning_content import fragments, validate_division, coverage_rows
from utils.scripts.learning_question_quality import compare
from utils.scripts.structured_io import read_json, write_json, json_digest
from .test_material_backup import source_navigation
from utils.scripts.learning_navigation import render_navigation_markdown


def modern_decision(project, model, cfg, reviewed=True):
    lp.prepare(project, model, cfg, Path(model['workspace_root']))
    d = read_json(Path(model['run_dir']) / 'lesson-decision.template.json')
    d.update(overview='合成课程说明', key_points=['联系具体条件解释'])
    for p in d['teaching_points']:
        unit = next(u for u in model['units'] if u['unit_id'] == p['unit_id'])
        p['blocks'] = [{'block_id': 'BLOCK-' + p['unit_id'], 'kind': 'explanation',
                        'text': '先核对具体条件，再解释观察到的变化。',
                        'parts': [{'type': 'paragraph', 'text': '先核对具体条件，再解释观察到的变化。'}],
                        'objective_indices': list(range(len(unit['learning_objectives']))),
                        'point_ids': [x['point_id'] for x in model.get('material_points', []) if p['unit_id'] in x['unit_ids']],
                        'coverage': 'explained'}]
    d['questions'] = d['questions'][:1]
    for q in d['questions']:
        q.update(prompt='条件发生变化时，首先应该核对哪项依据？', options=['具体条件', '旧标签', '任意印象', '固定口号'],
                 reference_answer='具体条件', expected_points={u: ['核对具体条件'] for u in q['unit_ids']})
    if 'adaptation_applied' in d: d['adaptation_applied']['changes'] = '依据前课反馈调整示范深度。'
    if reviewed:
        from utils.scripts import learning_teaching_quality as quality
        from utils.scripts.learning_content import validate_teaching
        lesson = next(l for l in model['lessons'] if l['lesson_id'] == d['lesson_id'])
        validate_teaching(d, lesson, model)
        gate, _ = quality.evaluate(d, lesson, model, cfg)
        if gate:
            review = read_json(Path(gate['quality_template']))
            review['short_complete_reason'] = '合成测试目标仅要求核对具体条件，正文明确给出核对动作和解释对象，无其他待讲步骤。'
            for item in review['items']: item.update(judgment='sufficient', reason='本块明确讲解所引用目标的核对动作及对象。')
            assert quality.evaluate(d, lesson, model, cfg, review)[0] is None
    return d


def finish(project, model, cfg, d, feedback=None):
    lp.publish(project, model, cfg, d)
    q = model['lessons'][0]['content']['questions'][0]
    review = lp.collect_answers(project, model, {'answers': {q['question_id']: chr(65 + q['correct_index'])}, 'learner_feedback': feedback or {}})
    review.update(strengths='能核对具体条件', weaknesses='新情境迁移尚未验证')
    for item in review['scores']: item['feedback'] = '已核对本题的具体条件。'
    result = lp.review(project, model, cfg, review)
    acknowledge_feedback(project, model, cfg, result)
    return result


@pytest.mark.parametrize('mode', ['chat', 'file'])
def test_optional_feedback_defaults_without_extra_question_gate(workspace, mode):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg)
    lp.publish(project, model, cfg, d)
    lesson = model['lessons'][0]
    assert '## 学习反馈' in lesson['markdown_template']
    if mode == 'file':
        f = project / '课程' / lesson['filename']
        f.write_text(f.read_text(encoding='utf-8').replace('⟦请将这行替换为你的回答，可分段填写⟧', 'A'), encoding='utf-8')
        review = lp.collect_answers(project, model)
    else: review = lp.collect_answers(project, model, {'Q-1': 'A'})
    assert lesson['learner_feedback'] == {'difficulty': 'B', 'comment': '', 'difficulty_filled': False, 'comment_filled': False}
    review.update(strengths='能核对条件', weaknesses='其他情境未验证')
    for item in review['scores']: item['feedback'] = '正确识别条件。'
    result = lp.review(project, model, cfg, review)
    acknowledge_feedback(project, model, cfg, result)
    assert result['status'] == 'ready' and '你还有疑问吗' not in result['feedback_markdown']
    assert len(lesson['content']['questions']) == 1
    assert lp.prepare(project, model, cfg, workspace)['status'] == 'completed'


def test_feedback_question_answer_then_adapt_cli(workspace, capsys):
    project = new_project(workspace, 2); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg)
    result = finish(project, model, cfg, d, {'difficulty': 'C', 'comment': '为什么要先核对条件？'})
    assert result['status'] == 'adaptation_decision_required'
    packet = copy.deepcopy(model['pending_adaptation'])
    packet['decision'].update(feedback_kind='question', rationale='先答疑，再增加操作示范。')
    f = Path(model['run_dir'])/'adaptation-input.json'; write_json(f, packet)
    args = ['--project-dir', str(project)]
    assert runner.main(['apply-adaptation', *args, '--decision', str(f)]) == 3; capsys.readouterr()
    assert lp.load(project)['state'] == 'awaiting_feedback_questions'
    assert runner.main(['questions', *args, '--question', '为什么要先核对条件？', '--answer', '条件决定结论是否适用。']) == 3; capsys.readouterr()
    assert runner.main(['apply-adaptation', *args, '--decision', str(f)]) == 0; capsys.readouterr()
    model = lp.load(project)
    assert model['next_adaptation']['learner_feedback']['difficulty'] == 'C'
    lp.prepare(project, model, cfg, workspace)
    template = read_json(Path(model['run_dir'])/'lesson-decision.template.json')
    assert template['adaptation_applied']['feedback_id'] == packet['feedback_id']


def route_model(workspace):
    project = new_project(workspace, 4); model, receipt = lp.context(project); cfg = receipt['config']
    for u in model['units']: u['prerequisites'] = [] if u['unit_id'] == 'U-0' else ['U-0']
    for l in model['lessons']:
        l['track'] = 'main' if l['teaches_unit_ids'][0] in ('U-0', 'U-1') else 'branch'
    first = model['lessons'][0]; first['status'] = 'completed'
    model['unit_progress']['U-0']['status'] = 'completed'; model['current_lesson_id'] = first['lesson_id']
    model['routing_protocol'] = 1; lp.save(project, model, cfg)
    return project, model, cfg


def test_fork_default_main_branch_defer_resume_and_interfaces(workspace, capsys):
    project, model, cfg = route_model(workspace)
    result = lp.prepare(project, model, cfg, workspace)
    assert result['status'] == 'awaiting_route_choice' and '图谱页' in result['markdown']
    args = ['--project-dir', str(project)]
    assert runner.main(['choose-route', *args, '--choice', '继续', '--choice-sha256', result['choice_sha256']]) == 0; capsys.readouterr()
    model = lp.load(project)
    assert lp.prepare(project, model, cfg, workspace)['status'] == 'lesson_decision_required'
    assert model['current_lesson_id'] == model['lessons'][1]['lesson_id']
    model['state'] = 'ready'; model['lessons'][1]['status'] = 'completed'; model['unit_progress']['U-1']['status'] = 'completed'
    result = lp.prepare(project, model, cfg, workspace)
    assert result['status'] == 'awaiting_branch_continuation'
    result = route.choose(model, 'later', result['choice_sha256'], lp.transition); lp.save(project, model, cfg)
    assert result['status'] == 'main_completed'
    assert read_json(project/'项目.json')['status'] == 'main_completed'
    assert all(l['status'] == 'pending' for l in model['lessons'][2:])
    assert runner.main(['choose-route', *args, '--choice', model['lessons'][2]['number'], '--choice-sha256', result['choice_sha256']]) == 0; capsys.readouterr()
    assert lp.load(project)['state'] == 'ready'
    service = import_module('track_snapshot_test', ROOT/'applications/交互式学习/scripts/workspace_service.py')
    snapshot = service.public_snapshot(lp.load(project), [])
    assert {n['track'] for n in snapshot['lessons']} == {'main', 'branch'}
    assert {n['track'] for n in snapshot['units']} == {'main', 'branch'}


def test_stale_fork_rejected_and_branch_session_returns_to_main(workspace):
    project, model, cfg = route_model(workspace)
    result = lp.prepare(project, model, cfg, workspace)
    stale = route.choose(model, '继续', 'outdated', lp.transition)
    assert stale['status'] == 'awaiting_route_choice' and '重选' in stale['message']
    branch = model['lessons'][2]
    assert route.choose(model, branch['number'], result['choice_sha256'], lp.transition)['status'] == 'ready'
    lp.prepare(project, model, cfg, workspace)
    assert model['current_lesson_id'] == branch['lesson_id']
    branch = next(l for l in model['lessons'] if l['lesson_id'] == branch['lesson_id'])
    branch['status'] = 'completed'; model['unit_progress']['U-2']['status'] = 'completed'; model['state'] = 'ready'
    result = lp.prepare(project, model, cfg, workspace)
    assert result['status'] == 'lesson_decision_required' and model['current_lesson_id'] == model['lessons'][1]['lesson_id']


def test_short_complete_block_allowed_missing_objective_rejected(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg); bad = copy.deepcopy(d)
    bad['teaching_points'][0]['blocks'][0]['objective_indices'] = []
    with pytest.raises(ValueError, match='学习目标'): lp.publish(project, model, cfg, bad)
    mentioned = copy.deepcopy(d)
    mentioned['teaching_points'][0]['blocks'][0]['coverage'] = 'mentioned'
    with pytest.raises(ValueError, match='不能仅提及'): lp.publish(project, model, cfg, mentioned)
    assert lp.publish(project, model, cfg, d)['status'] == 'awaiting_answer'
    assert len(d['teaching_points'][0]['blocks']) == 1


def test_duplicate_question_needs_retest_purpose(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg)
    nav = read_json(Path(model['navigation_json']))
    old = nav['planning_profile']['assessment']['answers'][0]
    d['questions'][0].update(prompt=old['prompt'], options=old['options'][:4], correct_index=old['correct_index'])
    with pytest.raises(ValueError, match='重复'): lp.publish(project, model, cfg, d)
    d['questions'][0]['reuse_review'] = [{'history_id': 'diagnostic-1', 'purpose': 'retest', 'reason': '显式复测同一结论，限制掌握证据。'}]
    blank = copy.deepcopy(d); blank['questions'][0]['reuse_review'][0]['reason'] = ' '
    with pytest.raises(ValueError, match='重复'): lp.publish(project, model, cfg, blank)
    assert lp.publish(project, model, cfg, d)['status'] == 'awaiting_answer'
    assert compare({'question_id': 'Q', 'prompt': '不同题干', 'options': old['options'][:4][::-1]}, [{**old, 'history_id': 'D'}])[0]['kind'] == 'duplicate'
    quality = read_json(Path(model['run_dir'])/'question-quality'/'state.json')
    assert quality['state'] == 'completed'
    assert 'quality_review_required' in quality['stateHistory']


def test_body_point_plan_confirmation_and_teaching_coverage(workspace, capsys):
    path, source, _ = source_navigation(workspace)
    nav = read_json(path); sid = nav['sources'][0]['source_id']
    fs = fragments(source, sid, nav['sources'][0]['sha256'])
    division = {'source_id': sid, 'source_sha256': nav['sources'][0]['sha256'],
                'points': [{'fragment_ids': [f['fragment_id'] for f in fs[1:]], 'summary': '原始材料内容', 'track': 'main', 'reason': '核心要点'}],
                'ignored_fragments': [{'fragment_id': fs[0]['fragment_id'], 'reason': '标题'}]}
    point = validate_division(nav['sources'][0], {'fragments': fs}, division)[0]
    point['unit_ids'] = ['U-0']; nav['material_points'] = [point]
    nav['units'][0].update(track='main', point_ids=[point['point_id']])
    nav['units'][0]['source']['content_span'] = {'start_line': point['start_line'], 'end_line': point['end_line']}
    from utils.scripts.adaptive_assessment import graph_revision
    nav['planning_profile']['assessment']['graph_revision'] = graph_revision(nav['units'])
    fragment = read_json(path.with_suffix('.sources')/f'{sid}.json')
    fragment['learning_points'][0]['source_locator'] = nav['units'][0]['source']
    write_json(path.with_suffix('.sources')/f'{sid}.json', fragment)
    from utils.scripts.learning_navigation_bundle import render_source_fragment_markdown
    (path.with_suffix('.sources')/f'{sid}.md').write_text(render_source_fragment_markdown(fragment), encoding='utf-8')
    write_json(path.with_suffix('.sources')/sid/'point-division.json', division)
    write_json(path, nav); path.with_suffix('.md').write_text(render_navigation_markdown(nav), encoding='utf-8')
    receipt = lp.create(workspace, path, confirmed_name='正文覆盖合成测试')
    project = Path(receipt['project_dir']); args = ['--project-dir', str(project)]
    assert runner.main(['resume', *args, '--backup-plan-sha256', receipt['backup_plan_sha256']]) == 3
    capsys.readouterr(); model = lp.load(project)
    assert model['state'] == 'lesson_plan_required'
    assert runner.main(['confirm-plan', *args, '--revision', str(model['revision'])]) == 0
    capsys.readouterr(); model, receipt = lp.context(project); cfg = receipt['config']
    empty = copy.deepcopy(model); empty['lessons'][0]['archived'] = True
    with pytest.raises(ValueError, match='原文要点'): coverage_rows(empty)
    d = modern_decision(project, model, cfg); bad = copy.deepcopy(d)
    bad['teaching_points'][0]['blocks'][0]['point_ids'] = []
    with pytest.raises(ValueError, match='原文要点'): lp.publish(project, model, cfg, bad)
    assert lp.publish(project, model, cfg, d)['status'] == 'awaiting_answer'
    rows = coverage_rows(model)
    assert rows[0]['lessons'][0]['degree'] == '充分讲解'
    assert '材料覆盖' in (project/'学习路线.md').read_text(encoding='utf-8')
    assert source.exists()


def test_route_progress_change_refreshes_and_all_branches_skipped_unblocks(workspace):
    project, model, cfg = route_model(workspace)
    old = lp.prepare(project, model, cfg, workspace)
    model['unit_progress']['U-0']['status'] = 'pending'
    changed = route.choose(model, '继续', old['choice_sha256'], lp.transition)
    assert changed['status'] == 'ready' and '路线' in changed['message']
    model['unit_progress']['U-0']['status'] = 'completed'
    old = lp.prepare(project, model, cfg, workspace)
    for lesson in model['lessons'][2:]: lesson.update(skip=True, status='skipped')
    result = lp.prepare(project, model, cfg, workspace)
    assert result['status'] == 'lesson_decision_required'
    assert model['current_lesson_id'] == model['lessons'][1]['lesson_id']


def test_default_main_uses_recommended_order(workspace):
    project, model, cfg = route_model(workspace)
    model['lessons'][2]['track'] = 'main'; lp.rebuild(model, cfg)
    ordered_units = ['U-0', 'U-2', 'U-1', 'U-3']
    model['candidate_orders'] = {'candidates': [{'candidate_id': 'R', 'unit_ids': ordered_units}], 'selected_order_id': 'R'}
    result = lp.prepare(project, model, cfg, workspace)
    assert route.choose(model, '继续', result['choice_sha256'], lp.transition)['lesson_id'] == model['lessons'][2]['lesson_id']


def test_deferred_point_cannot_be_silently_promoted(workspace):
    project, model, cfg = route_model(workspace)
    model['material_points'] = [{'point_id': 'POINT-branch', 'source_id': 'synthetic', 'fragment_ids': ['F'], 'summary': '暂缓执行', 'track': 'branch', 'reason': '稍后执行', 'unit_ids': ['U-2']}]
    model['units'][1]['prerequisites'] = ['U-2']
    with pytest.raises(ValueError, match='不能静默提升'): lp.rebuild(model, cfg)
    assert model['material_points'][0]['track'] == 'branch'


def test_incremental_navigation_requires_plan_and_discards_route_choice(workspace):
    project, model, cfg = route_model(workspace)
    result = lp.prepare(project, model, cfg, workspace)
    assert result['status'] == 'awaiting_route_choice'
    nav = read_json(Path(model['navigation_json']))
    nav['material_points'] = [{'point_id': 'POINT-next', 'source_id': 'synthetic', 'fragment_ids': ['F'], 'summary': '核心', 'track': 'main', 'reason': '主线', 'unit_ids': ['U-1']}]
    lp.sync_navigation(model, nav)
    assert model['state'] == 'lesson_plan_required' and model['planning_confirmed'] is False
    assert not {'route_choice', 'route_selection', 'branch_session'} & set(model)


def test_public_new_course_rejects_v4_and_prefills_blocks(workspace, capsys):
    project = new_project(workspace, 1); model, receipt = lp.context(project)
    lp.prepare(project, model, receipt['config'], workspace)
    template = read_json(Path(model['run_dir'])/'lesson-decision.template.json')
    block = template['teaching_points'][0]['blocks'][0]
    assert block['block_id'] and block['objective_indices'] and block['text'] == ''
    from .test_interactive_tutor import legacy_decision
    legacy_decision(template); f = Path(model['run_dir'])/'old.json'; write_json(f, template)
    assert runner.main(['publish-lesson', '--project-dir', str(project), '--decision', str(f)]) == 2
    assert 'v7' in capsys.readouterr().out


def test_invalid_feedback_and_reserved_question_ids(workspace):
    from utils.scripts.learning_content import normalize_feedback
    from utils.scripts.learning_question_quality import arrange_options
    for invalid in ([], '', False, 0):
        with pytest.raises(ValueError): normalize_feedback(invalid)
    project = new_project(workspace, 1); model, receipt = lp.context(project)
    d = modern_decision(project, model, receipt['config'])
    d['questions'][0]['question_id'] = 'LEARNER_COMMENT'
    with pytest.raises(ValueError, match='保留标记'): lp.publish(project, model, receipt['config'], d)
    q = {'type': 'multiple_choice', 'correct_index': 0, 'options': ['甲', '乙', '丙', '丁'], 'reference_answer': 'A：甲，因为满足条件'}
    arrange_options(q, 2)
    assert q['options'][2] == '甲' and q['reference_answer'].startswith('C：甲')
