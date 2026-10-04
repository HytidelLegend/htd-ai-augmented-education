"""Teaching-body scope, stale review rejection and public-interface recovery."""
import copy
from pathlib import Path
import pytest
from .test_interactive_tutor import workspace, new_project, runner
from .test_learning_feedback_routes import modern_decision
from utils.scripts import learning_project as lp, learning_teaching_quality as quality
from utils.scripts.structured_io import read_json, write_json


def approve(gate):
    review = read_json(Path(gate['quality_template']))
    review['short_complete_reason'] = '正文明确核对动作、具体条件及解释对象，逐项覆盖本课合成目标，不存在遗漏的推理步骤。'
    for item in review['items']:
        item.update(judgment='sufficient', reason='该块明确给出引用目标要求的动作及对象。')
    return review


def test_counting_scope_code_math_and_markdown():
    body = '### 标题\n**中文** [English](https://example.invalid/long)\n`x = 1` $a+b$\n```python\n# code\ny = 2\n```'
    stats = quality.metrics(body)
    assert stats == {'body_chars': 23, 'code_chars': 11, 'formula_chars': 3}
    md = '# 课程\n## 课程概述\n' + '概述'*1000 + '\n## 课程讲解\n'+body+'\n## 正式习题\n'+'题目'*1000
    assert quality.metrics(quality.section(md)) == stats
    assert quality.metrics('`a|b` $a*b$ [x](https://example.invalid/a(b))') == {
        'body_chars': 7, 'code_chars': 3, 'formula_chars': 3}
    assert quality.metrics('```python\n"$x$"\n```') == {'body_chars': 5, 'code_chars': 5, 'formula_chars': 0}


def test_short_md_gate_no_publish_and_review_recovery(workspace, capsys):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False); d['overview'] = '概述'*1000
    decision = Path(model['run_dir'])/'short.json'; write_json(decision, d)
    args = ['publish-lesson','--project-dir',str(project),'--decision',str(decision)]
    assert runner.main(args) == 3
    import json
    gate = json.loads(capsys.readouterr().out)
    assert read_json(Path(gate['quality_packet']))['body_chars'] < 800
    lesson = model['lessons'][0]
    assert not (project/'课程'/lesson['filename']).exists()
    assert lp.load(project)['state'] == 'lesson_decision_required'
    review = approve(gate); invalid = copy.deepcopy(review); invalid['short_complete_reason'] = ' '
    with pytest.raises(ValueError, match='偏短'): lp.publish(project, model, cfg, d, quality_review=invalid)
    review_file = Path(model['run_dir'])/'accepted-review.json'; write_json(review_file, review)
    assert runner.main(args+['--quality-review',str(review_file)]) == 0; capsys.readouterr()
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'


@pytest.mark.parametrize('change', ['body', 'config', 'plan', 'objectives', 'chapter', 'providers', 'neighbor'])
def test_old_review_cannot_be_reused(workspace, change):
    project = new_project(workspace, 2 if change == 'neighbor' else 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d); review = approve(gate)
    if change == 'body': d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] += '检查条件。'
    if change == 'config': cfg['lesson']['reference_chars_min'] = 801
    if change == 'plan': model['lessons'][0]['track'] = 'branch'
    if change == 'objectives': model['units'][0]['learning_objectives'][0] = '能说明具体条件'
    if change == 'chapter': model['chapters'][0]['title'] = '新的章节计划'
    if change == 'providers': model['lessons'][0]['prerequisite_providers'] = {'U-0': 'SYNTHETIC-provider'}
    if change == 'neighbor': model['lessons'][1]['title'] = '下游课程的新规划'
    with pytest.raises(ValueError, match='已变化'): lp.publish(project, model, cfg, d, quality_review=review)
    assert model['state'] == 'lesson_decision_required'


def test_needs_expansion_resume_and_completed_checkpoint(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d); review = approve(gate)
    review['items'][0]['judgment'] = 'needs_expansion'
    result = lp.publish(project, model, cfg, d, quality_review=review)
    assert result['status'] == 'teaching_revision_required'
    review['items'][0]['judgment'] = 'sufficient'
    assert lp.publish(project, model, cfg, d, quality_review=review)['status'] == 'teaching_revision_required'
    assert read_json(Path(gate['quality_packet']).parent/'review.json')['items'][0]['judgment'] == 'needs_expansion'
    changed_config = copy.deepcopy(cfg); changed_config['lesson']['reference_chars_min'] = 1
    assert lp.publish(project, model, changed_config, d)['status'] == 'teaching_revision_required'
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] += '先列出当前观察，再逐项核对条件，说明条件改变如何对应观察变化。'
    new_gate = lp.publish(project, model, cfg, d)
    assert lp.publish(project, model, cfg, d, quality_review=approve(new_gate))['status'] == 'awaiting_answer'
    state = read_json(Path(gate['quality_packet']).parent/'state.json')
    assert state['state'] == 'revision_required'
    assert read_json(Path(new_gate['quality_packet']).parent/'state.json')['state'] == 'completed'


def test_long_body_allowed_and_multiple_units_aggregate(workspace):
    project = new_project(workspace, 2); model, receipt = lp.context(project); cfg = receipt['config']
    first, second = model['lessons']
    first['teaches_unit_ids'] = ['U-0','U-1']; first['internal_unit_order'] = ['U-0','U-1']
    second['archived'] = True; lp.save(project, model, cfg)
    d = modern_decision(project, model, cfg, reviewed=False)
    for p in d['teaching_points']: p['blocks'][0]['parts'][0]['text'] = '根据具体条件核对证据并解释变化。'*60
    d['questions'][0].update(type='open_ended',unit_ids=['U-0','U-1'],options=[],correct_index=None,
                             expected_points={'U-0':['核对条件'],'U-1':['解释变化']})
    assert lp.publish(project, model, cfg, d)['status'] == 'awaiting_answer'
    data = model['lessons'][0]['teaching_quality']['packet']
    assert data['body_chars'] > 1500 and data['over_reference']
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'


@pytest.mark.parametrize('text,chars,code_chars', [
    ('标题\n===\n\n正文', 2, 0),
    ('标题\n---\n\n正文', 2, 0),
    (r'\*字\* \| \#', 5, 0),
    ('````python\n```\n# 注释\n````', 6, 6),
    ('    # 注释\n    x = 1', 6, 6),
    ('<!-- `隐藏代码` $隐藏公式$ -->\n正文', 2, 0),
    ('[标签]\n\n[标签]: https://example.invalid/path', 2, 0),
    ('`<!-- 保留 -->`', 9, 9),
    ('> ### 引用中的标题\n正文', 2, 0),
    ('<h3>HTML 标题</h3>\n正文', 2, 0),
])
def test_markdown_visible_character_boundaries(text, chars, code_chars):
    assert quality.metrics(text) == {'body_chars': chars, 'code_chars': code_chars, 'formula_chars': 0}


@pytest.mark.parametrize('chars,short,over', [(799,True,False),(800,False,False),(1500,False,False),(1501,False,True)])
def test_reference_boundaries_are_soft_and_apply_to_total_body(chars, short, over):
    from utils.scripts.learning_content import render_teaching_blocks
    point = {'unit_id': 'U', 'blocks': [{'block_id': 'B', 'kind': 'explanation', 'text': '字'*chars,
              'objective_indices': [0], 'point_ids': [], 'coverage': 'explained'}]}
    point['text'] = render_teaching_blocks(point)
    lesson = {'lesson_id':'L','teaches_unit_ids':['U'],'internal_unit_order':['U'],'track':'main'}
    model = {'units':[{'unit_id':'U','learning_objectives':['说明目标']}], 'lessons':[lesson],
             'unit_progress': {'U':{'skip':False}}}
    data = quality.packet({'schema_version':'5.0','teaching_points':[point]}, lesson, model,
                          {'lesson':{'reference_chars_min':800,'reference_chars_max':1500}})
    assert (data['body_chars'], data['short'], data['over_reference']) == (chars, short, over)
    assert not data['review_items']


def test_review_template_and_invalid_response_do_not_erase_checkpoint(workspace):
    from utils.scripts.workflow_checkpoint import WorkflowCheckpoint
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d); directory = Path(gate['quality_packet']).parent
    filled = approve(gate); write_json(Path(gate['quality_template']), filled)
    assert lp.publish(project, model, cfg, d)['status'] == 'teaching_review_required'
    assert read_json(Path(gate['quality_template'])) == filled
    flow = WorkflowCheckpoint(quality.TRANSITIONS, directory, resume=True, restart_completed=False)
    flow.move('validating_review')
    before = (directory/'state.json').read_bytes()
    invalid = copy.deepcopy(filled); invalid['items'] = []
    with pytest.raises(ValueError, match='遗漏'): lp.publish(project, model, cfg, d, quality_review=invalid)
    assert (directory/'state.json').read_bytes() == before


def test_snapshot_validation_detects_block_and_metric_tampering(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d)
    assert lp.publish(project, model, cfg, d, quality_review=approve(gate))['status'] == 'awaiting_answer'
    original = model['lessons'][0]
    changed = copy.deepcopy(original)
    changed['content']['teaching_points'][0]['blocks'][0]['text'] += '尚未被验证的内容。'
    with pytest.raises(ValueError, match='快照|派生'): quality.verify_published(changed, changed['markdown_template'])
    changed = copy.deepcopy(original); changed['teaching_quality']['packet']['short'] = False
    with pytest.raises(ValueError, match='冻结依据'): quality.verify_published(changed, changed['markdown_template'])
    legacy = copy.deepcopy(original)
    legacy['teaching_quality']['packet'].pop('basis')
    legacy['teaching_quality']['packet'].pop('metrics_version')
    before = copy.deepcopy(legacy)
    quality.verify_published(legacy, legacy['markdown_template'])
    assert legacy == before


def test_republication_preserves_teaching_quality_history(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    original = modern_decision(project, model, cfg)
    assert lp.publish(project, model, cfg, original)['status'] == 'awaiting_answer'
    old_record = copy.deepcopy(model['lessons'][0]['teaching_quality'])
    uid = model['current_lesson_id']; path = lp.art(project, '调整指令')
    for op in ('skip', 'restore'):
        write_json(path, {'actions': [{'action_id': op, 'target_type': 'lesson', 'target_id': uid, 'operation': op}]})
        lp.apply_actions(project, model)
    revised = modern_decision(project, model, cfg, reviewed=False)
    revised['teaching_points'][0]['blocks'][0]['parts'][0]['text'] += '补充观察变化与具体条件的联系。'
    revised['questions'][0]['reuse_review'] = [{'history_id': uid+'/'+original['questions'][0]['question_id'],
                                               'purpose': 'retest', 'reason': '课程恢复后的明确复测，不作为新的迁移证据。'}]
    gate = lp.publish(project, model, cfg, revised)
    assert lp.publish(project, model, cfg, revised, quality_review=approve(gate))['status'] == 'awaiting_answer'
    assert model['lessons'][0]['publication_history'][0]['teaching_quality'] == old_record
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'


def test_teaching_gate_precedes_podcast_and_visible_empty_is_rejected(workspace, monkeypatch):
    from utils.scripts import learning_lesson_podcast
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    cfg['podcast']['enabled'] = True
    d = modern_decision(project, model, cfg, reviewed=False)
    monkeypatch.setattr(learning_lesson_podcast, 'advance', lambda *a, **k: pytest.fail('未完成讲解复核就进入播客'))
    assert lp.publish(project, model, cfg, d)['status'] == 'teaching_review_required'
    assert model['state'] == 'lesson_decision_required'
    assert not (project/'课程'/model['lessons'][0]['filename']).exists()
    with pytest.raises(ValueError, match='不接受作答'): lp.collect_answers(project, model, {'Q-1':'A'})
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '<!-- 没有可见教学正文 -->'
    with pytest.raises(ValueError, match='可见正文'): lp.publish(project, model, cfg, d)


def test_block_cannot_escape_teaching_section(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '解释\n## 其他小节\n越界正文'
    with pytest.raises(ValueError, match='标题'): lp.publish(project, model, cfg, d)


def test_grouping_suggestions_allow_single_point_and_do_not_mutate(workspace):
    from utils.scripts.learning_content import grouping_context, teaching_template
    project = new_project(workspace, 2); model, receipt = lp.context(project)
    before = copy.deepcopy(model['lessons'])
    assert grouping_context(model, receipt['config'])['candidates']
    assert model['lessons'] == before
    blocks = teaching_template('U', ['目标一','目标二'], [{'unit_ids':['U'],'point_id':'P'}], [])['blocks']
    assert len(blocks) == 3 and all(x['coverage'] == 'mentioned' for x in blocks)


def test_completed_record_rejects_corruption_and_same_length_body_change(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d); review = approve(gate)
    assert lp.publish(project, model, cfg, d, quality_review=review)['status'] == 'awaiting_answer'
    lesson = copy.deepcopy(model['lessons'][0])
    lesson['content']['teaching_points'][0]['text'] = lesson['content']['teaching_points'][0]['text'].replace('具体', '任意')
    with pytest.raises(ValueError, match='哈希|快照'): quality.verify_published(lesson, lesson['markdown_template'])
    record_path = Path(gate['quality_packet']).parent/'result.json'
    saved = read_json(record_path); saved['review']['items'] = []; write_json(record_path, saved)
    current = copy.deepcopy(model); current['state'] = 'lesson_decision_required'
    with pytest.raises(ValueError, match='项目不一致'): lp.publish(project, current, cfg, d)


def test_interrupted_checkpoint_resumes_without_publishing(workspace):
    from utils.scripts.workflow_checkpoint import WorkflowCheckpoint
    from utils.scripts.learning_content import validate_teaching
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    gate = lp.publish(project, model, cfg, d)
    directory = Path(gate['quality_packet']).parent
    flow = WorkflowCheckpoint(quality.TRANSITIONS, directory, resume=True, restart_completed=False)
    flow.move('validating_review')  # Simulate interruption in the semantic validation step.
    assert lp.publish(project, model, cfg, d)['status'] == 'teaching_review_required'
    assert model['state'] == 'lesson_decision_required'
    invalid = approve(gate); invalid['items'].append(copy.deepcopy(invalid['items'][0]))
    with pytest.raises(ValueError, match='重复'): lp.publish(project, model, cfg, d, quality_review=invalid)
    assert read_json(directory/'state.json')['state'] == 'review_required'
    # An interruption after automatic completion but before writing its result is recoverable.
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '明确核对具体条件并解释观察变化。'*60
    validate_teaching(d, model['lessons'][0], model)
    data = quality.packet(d, model['lessons'][0], model, cfg)
    new_dir = Path(model['run_dir'])/'teaching-quality'/d['lesson_id']/data['fingerprint']
    flow = WorkflowCheckpoint(quality.TRANSITIONS, new_dir)
    flow.move('measuring'); flow.move('completed')
    assert not (new_dir/'result.json').exists()
    assert lp.publish(project, model, cfg, d)['status'] == 'awaiting_answer'
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'
