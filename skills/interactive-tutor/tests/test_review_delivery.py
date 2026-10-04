"""Review presentation, public delivery interfaces and transaction recovery."""
import copy
import json
from pathlib import Path

import pytest

from .test_interactive_tutor import workspace, new_project, complete_lesson, decision, runner, acknowledge_feedback
from utils.scripts import learning_project as lp
from utils.scripts.structured_io import write_json


def test_pending_gate_cli_resume_and_delivery(workspace, capsys):
    project = new_project(workspace, 2)
    model, cfg, result = complete_lesson(project, incorrect=True, deliver=False)
    text = result['feedback_markdown']
    for label in ('第 1 题', '你的作答', '作答情况', '批改意见', '完整参考答案', '简略解释', '与参考答案的差异', '本课总结'):
        assert label in text
    assert '你选择的是 B' in text and 'A. 正确' in text
    lesson = model['lessons'][0]
    pending = lp.prepare(project, model, cfg, workspace)
    assert pending['status'] == 'feedback_delivery_required'
    assert model['current_lesson_id'] == lesson['lesson_id']
    assert not (project / '课程/课程_1-2.md').exists()
    args = ['--project-dir', str(project)]
    for command in ('prepare-lesson', 'status', 'resume', 'report'):
        assert runner.main([command, *args]) == 3
        reply = json.loads(capsys.readouterr().out)
        assert reply['feedback_markdown'] == text
    assert runner.main(['deliver-feedback', *args, '--lesson', result['lesson_id'],
                        '--review-sha256', result['review_sha256'], '--feedback-sha256', '0' * 64]) == 2
    capsys.readouterr()
    model = lp.load(project)
    assert model['lessons'][0]['review_delivery']['state'] == 'awaiting_display'
    delivery_args = ['deliver-feedback', '--run-dir', model['run_dir'], '--lesson', result['lesson_id'],
                    '--review-sha256', result['review_sha256'], '--feedback-sha256', result['feedback_sha256']]
    assert runner.main(delivery_args) == 0
    assert json.loads(capsys.readouterr().out)['delivery_status'] == 'delivered'
    before = lp.load(project)
    assert runner.main(delivery_args) == 0
    capsys.readouterr()
    assert lp.load(project)['revision'] == before['revision']
    assert lp.load(project)['lessons'][0]['review_delivery'] == before['lessons'][0]['review_delivery']
    # Historical lessons still have their existing question gate.
    assert runner.main(['questions', *args, '--no-questions']) == 0
    capsys.readouterr()
    assert runner.main(['prepare-lesson', *args]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'lesson_decision_required'
    assert runner.main(['verify', *args]) == 0
    capsys.readouterr()


def test_full_open_reference_explanation_and_large_difference(workspace):
    project = new_project(workspace, 1)
    model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg, open_question=True)
    data['questions'][0]['reference_answer'] = '完整结论。\n\n成立条件：先核对证据，再判断。'
    lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': '不核对证据，直接判断。'})
    review.update(strengths='能够表达观点', weaknesses='结论与依据不一致')
    review['scores'][0].update(unit_scores={'U-0': 0}, feedback='结论与参考答案相反。',
                              reference_explanation='证据是判断结论的前提。', difference_notes='你省略了核对证据，并将先后顺序颠倒。',
                              misconceptions=[{'unit_id': 'U-0', 'title': '前提', 'weakness': '颠倒顺序',
                                               'rule': '先核对证据', 'correction': '核对后再判断'}])
    result = lp.review(project, model, cfg, review)
    assert data['questions'][0]['reference_answer'] in result['feedback_markdown']
    assert '证据是判断结论的前提。' in result['feedback_markdown']
    assert '你省略了核对证据，并将先后顺序颠倒。' in result['feedback_markdown']
    lp.questions_event(model, no_questions=True); lp.save(project, model, cfg)
    assert lp.prepare(project, model, cfg, workspace)['status'] == 'feedback_delivery_required'
    acknowledge_feedback(project, model, cfg, result)
    assert lp.prepare(project, model, cfg, workspace)['status'] == 'completed'


def test_historical_pending_and_tampered_delivery_log(workspace):
    project = new_project(workspace, 1)
    model, cfg, result = complete_lesson(project)
    model['lessons'][0].pop('review_delivery')
    lp.save(project, model, cfg)
    pending = lp.prepare(project, model, cfg, workspace)
    assert pending['status'] == 'feedback_delivery_required'
    record = model['lessons'][0]['review_delivery']
    path = lp.review_delivery.directory(model, record) / 'feedback.md'
    path.write_text('被修改的反馈', encoding='utf-8')
    with pytest.raises(ValueError, match='日志'):
        lp.verify(project, model, cfg)
    candidate = copy.deepcopy(model['lessons'][0])
    candidate['answers']['Q-1'] = '修改后的答案'
    with pytest.raises(ValueError, match='版本'):
        lp.review_delivery.verify_record(candidate, record)


def test_delivery_rollback_and_render_failure_recovery(workspace, monkeypatch):
    project = new_project(workspace, 1)
    model, cfg, result = complete_lesson(project, deliver=False)
    original = copy.deepcopy(model)
    real = lp.write_text_transaction
    def fail(updates):
        raise OSError('SYNTHETIC transaction failure')
    monkeypatch.setattr(lp, 'write_text_transaction', fail)
    with pytest.raises(OSError):
        acknowledge_feedback(project, model, cfg, result)
    assert model == original
    assert lp.load(project)['lessons'][0]['review_delivery']['state'] == 'awaiting_display'
    monkeypatch.setattr(lp, 'write_text_transaction', real)
    acknowledge_feedback(project, model, cfg, result)
    assert lp.verify(project, model, cfg)['status'] == 'verified'
    candidate = copy.deepcopy(model['lessons'][0]); candidate.pop('review_delivery')
    candidate['review']['strengths'] += '（新批改版本）'
    render = lp.review_delivery.render
    monkeypatch.setattr(lp.review_delivery, 'render', lambda _: (_ for _ in ()).throw(ValueError('SYNTHETIC render failure')))
    with pytest.raises(ValueError): lp.review_delivery.prepare(candidate, model['run_dir'])
    assert candidate['review_delivery']['state'] == 'paused_error'
    restarted = copy.deepcopy(candidate); restarted.pop('review_delivery')
    monkeypatch.setattr(lp.review_delivery, 'render', render)
    record = lp.review_delivery.prepare(restarted, model['run_dir'])
    assert record['state'] == 'awaiting_display'
    assert any(item['from'] == 'paused_error' for item in record['history'])


def test_publish_cannot_bypass_pending_review(workspace):
    project = new_project(workspace, 2)
    model, cfg, _ = complete_lesson(project, deliver=False)
    model['state'] = 'lesson_decision_required'
    current = model['current_lesson_id']
    assert lp.publish(project, model, cfg, {})['status'] == 'feedback_delivery_required'
    assert model['current_lesson_id'] == current


@pytest.mark.parametrize('stage', ['pending', 'rendering', 'paused_error'])
def test_interrupted_render_checkpoint_recovery(workspace, stage):
    project = new_project(workspace, 1)
    model, _, _ = complete_lesson(project)
    lesson = copy.deepcopy(model['lessons'][0]); lesson.pop('review_delivery')
    lesson['review']['strengths'] += '（恢复测试版本）'
    record = {'schema_version': '1.0', 'lesson_id': lesson['lesson_id'],
              'review_sha256': lp.review_delivery.fingerprint(lesson), 'state': 'pending',
              'history': [], 'delivered_at': None}
    if stage == 'rendering': lp.review_delivery.move(record, stage)
    if stage == 'paused_error':
        record['error'] = 'SYNTHETIC render interruption'
        lp.review_delivery.move(record, stage)
    write_json(lp.review_delivery.directory(model, record) / 'state.json', record)
    recovered = lp.review_delivery.prepare(lesson, model['run_dir'])
    assert recovered['state'] == 'awaiting_display'
    assert recovered['history'][:len(record['history'])] == record['history']
    assert lp.review_delivery.verify_record(lesson, recovered)


@pytest.mark.parametrize('file', ['feedback.md', 'state.json'])
def test_resume_refuses_to_repair_corrupt_delivery_logs(workspace, capsys, file):
    project = new_project(workspace, 1)
    model, _, result = complete_lesson(project, deliver=False)
    record = model['lessons'][0]['review_delivery']
    path = lp.review_delivery.directory(model, record) / file
    if file.endswith('.json'):
        altered = copy.deepcopy(record); altered['feedback_sha256'] = '0' * 64
        write_json(path, altered)
    else: path.write_text('SYNTHETIC corrupt feedback', encoding='utf-8')
    before = path.read_bytes()
    for command in ('status', 'resume', 'report', 'prepare-lesson'):
        assert runner.main([command, '--project-dir', str(project)]) == 2
        capsys.readouterr()
        assert path.read_bytes() == before
    assert lp.load(project)['lessons'][0]['review_delivery']['state'] == 'awaiting_display'


def test_open_equivalence_requires_comparison_and_failed_review_is_unchanged(workspace):
    project = new_project(workspace, 1)
    model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg, open_question=True)
    lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': '这是正确的结论。'})
    lp.save(project, model, cfg)
    review.update(strengths='能说明结论', weaknesses='其他应用未验证')
    review['scores'][0].update(unit_scores={'U-0': 1}, feedback='覆盖全部评分点。')
    before = copy.deepcopy(model)
    omitted = copy.deepcopy(review)
    omitted['scores'][0].pop('difference_notes')
    with pytest.raises(ValueError, match='必须保留'):
        lp.review(project, model, cfg, omitted)
    assert model == before
    with pytest.raises(ValueError, match='等价表达说明'):
        lp.review(project, model, cfg, review)
    assert model == before and lp.load(project)['state'] == 'review_decision_required'
    review['scores'][0]['difference_notes'] = '措辞不同，但表达了同一正确结论，不扣分。'
    result = lp.review(project, model, cfg, review)
    assert '措辞不同，但表达了同一正确结论，不扣分。' in result['feedback_markdown']
    assert '**作答情况：** 正确' in result['feedback_markdown']


def test_multiline_multiple_questions_and_corrupt_checkpoint(workspace):
    project = new_project(workspace, 1)
    model, _, _ = complete_lesson(project)
    lesson = copy.deepcopy(model['lessons'][0]); lesson.pop('review_delivery')
    second = copy.deepcopy(lesson['content']['questions'][0]); second['question_id'] = 'Q-2'
    second['type'] = 'open_ended'; second['prompt'] = '解释结论。\n\n说明适用条件。'
    second['reference_answer'] = '完整结论。\n\n完整条件。'
    lesson['content']['questions'].append(second)
    lesson['answers']['Q-2'] = '第一段原话。\n\n第二段原话。'
    score = copy.deepcopy(lesson['review']['scores'][0])
    score.update(question_id='Q-2', difference_notes='结论等价，条件完整。')
    lesson['review']['scores'].insert(0, score)
    record = lp.review_delivery.prepare(lesson)
    text = lp.review_delivery.render(lesson)
    assert text.index('## 第 1 题') < text.index('## 第 2 题')
    assert '> 第一段原话。\n> \n> 第二段原话。' in text
    assert second['prompt'] in text and second['reference_answer'] in text
    corrupt = copy.deepcopy(record); corrupt['history'][0]['from'] = 'delivered'
    lesson['review_delivery'] = corrupt
    with pytest.raises(ValueError, match='迁移历史'):
        lp.review_delivery.prepare(lesson)


def test_partial_feedback_transaction_rolls_back_project_and_logs(workspace, monkeypatch):
    from utils.scripts import structured_io
    project = new_project(workspace, 1)
    model, cfg, result = complete_lesson(project, deliver=False)
    record = model['lessons'][0]['review_delivery']
    directory = lp.review_delivery.directory(model, record)
    targets = [project / '项目.json', lp.art(project, '学习路线'),
               project / '课程/课程_1-1.md', directory / 'state.json', directory / 'feedback.md']
    before = {p: p.read_bytes() for p in targets}
    replace = structured_io.os.replace
    interrupted = False
    def fail_once(source, target):
        nonlocal interrupted
        if Path(target) == directory / 'feedback.md' and not interrupted:
            interrupted = True
            raise OSError('SYNTHETIC feedback publication interruption')
        return replace(source, target)
    monkeypatch.setattr(structured_io.os, 'replace', fail_once)
    with pytest.raises(OSError): acknowledge_feedback(project, model, cfg, result)
    assert interrupted and {p: p.read_bytes() for p in targets} == before
    assert model['lessons'][0]['review_delivery']['state'] == 'awaiting_display'
    acknowledge_feedback(project, model, cfg, result)
    assert lp.verify(project, model, cfg)['status'] == 'verified'


def test_failed_review_render_resumes_from_original_submission(workspace, monkeypatch):
    project = new_project(workspace, 1)
    model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg)
    lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': 'A'})
    lp.save(project, model, cfg)
    review.update(strengths='能判断结论', weaknesses='其他应用未验证')
    review['scores'][0]['feedback'] = '判断正确。'
    before = copy.deepcopy(model)
    render = lp.review_delivery.render
    monkeypatch.setattr(lp.review_delivery, 'render', lambda _: (_ for _ in ()).throw(ValueError('SYNTHETIC render failure')))
    with pytest.raises(ValueError): lp.review(project, model, cfg, review)
    assert model == before and lp.load(project)['state'] == 'review_decision_required'
    monkeypatch.setattr(lp.review_delivery, 'render', render)
    restarted = lp.load(project)
    result = lp.review(project, restarted, cfg, review)
    record = restarted['lessons'][0]['review_delivery']
    assert result['delivery_status'] == 'awaiting_display'
    assert any(item['from'] == 'paused_error' for item in record['history'])
    assert lp.verify(project, restarted, cfg)['status'] == 'verified'
