"""Synthetic new-contract CLI, document transaction and composed audio tests."""
import copy
import json
from pathlib import Path
import pytest

from .test_interactive_tutor import workspace, navigation, new_project, decision, complete_lesson, runner
from .test_material_backup import source_navigation
from utils.scripts import learning_project as lp, learning_project_creation as creation
from utils.scripts import dialogue_pipeline as dp
from utils.scripts.learning_material_backup import backup_plan, ensure_backup, verify_backup
from utils.scripts.structured_io import read_json, write_json, json_digest


def test_name_gate_no_project_write_and_cli_confirmation(workspace, capsys):
    nav = navigation(workspace, 1)
    req = workspace / 'request.json'
    write_json(req, {'schema_version': '3.0', 'input_paths': [str(nav)], 'interaction_mode': 'chat'})
    assert runner.main(['start', '--root', str(workspace), '--request', str(req)]) == 3
    pending = json.loads(capsys.readouterr().out)
    assert pending['status'] == 'awaiting_name_confirmation'
    assert not (workspace / 'outputs/interactive-tutor').exists()
    target = ['--creation-dir', pending['creation_dir']]
    assert runner.main(['creation-status', *target]) == 0; capsys.readouterr()
    assert runner.main(['confirm-project-name', *target, '--name', '合成新项目', '--proposal-sha256', 'stale']) == 2; capsys.readouterr()
    args = ['confirm-project-name', *target, '--name', '合成新项目', '--proposal-sha256', pending['proposal_sha256']]
    assert runner.main(args) == 0
    ready = json.loads(capsys.readouterr().out)
    project = Path(ready['project_dir'])
    assert lp.load(project)['title'] == '合成新项目'
    assert str(project) in ready['markdown'] and '学习报告' in ready['markdown']
    assert runner.main(args) == 0
    assert json.loads(capsys.readouterr().out)['project_dir'] == str(project)
    assert runner.main(['creation-status', *target]) == 0
    final = json.loads(capsys.readouterr().out)
    assert final['status'] == 'ready' and final['markdown'] == ready['markdown']


def test_all_materials_required_for_multi_source_name(workspace, monkeypatch, capsys):
    nav = navigation(workspace, 1); data = read_json(nav)
    for i in range(2):
        path = workspace / f'material-{i}.md'; path.write_text(f'# 主题\n合成材料 {i}', encoding='utf-8')
        from utils.scripts.markdown_structure import sha256_file
        data['sources'].append({'source_id': f'S{i}', 'path': path.name, 'sha256': sha256_file(path)})
    monkeypatch.setattr(lp, 'navigation_preflight', lambda *a, **k: data)
    pending = creation.prepare(workspace, nav)
    assert pending['status'] == 'awaiting_name_suggestion'
    template = read_json(Path(pending['template']))
    template.update(title='统一主题', rationale='两份合成材料共同说明同一主题')
    template['reviewed_source_ids'] = ['S0']
    with pytest.raises(ValueError, match='全部'): creation.suggest(Path(pending['creation_dir']), template)
    template['reviewed_source_ids'] = ['S0', 'S1']
    path = workspace / 'decision.json'; write_json(path, template)
    assert runner.main(['suggest-project-name', '--creation-dir', pending['creation_dir'], '--decision', str(path)]) == 3
    proposal = json.loads(capsys.readouterr().out)
    assert proposal['suggested_name'] == '统一主题' and proposal['status'] == 'awaiting_name_confirmation'


def test_chat_answers_feedback_report_and_roadmap(workspace):
    project = new_project(workspace, 1)
    model, cfg, result = complete_lesson(project, incorrect=True)
    lesson = model['lessons'][0]
    text = (project / '课程' / lesson['filename']).read_text(encoding='utf-8')
    assert '> B' in text and text.index('answer:Q-1:end') < text.index('feedback:Q-1:start')
    assert '## 本课学习反馈' in text
    assert '## 课程概述' in text and '## 课程讲解' in text
    lp.save(project, model, cfg)
    assert text == (project / '课程' / lesson['filename']).read_text(encoding='utf-8')
    report = (project / '学习报告.md').read_text(encoding='utf-8')
    assert '作答反馈' not in report and '未验证能力' in report and '#feedback-Q-1' in report
    errors = (project / '错题本.md').read_text(encoding='utf-8')
    assert '**薄弱点：**' in errors and '### 例题 1' in errors
    route = (project / '学习路线.md').read_text(encoding='utf-8')
    assert '## 知识点思维导图' not in route
    assert route.index('## 知识点与前置关系') < route.index('```mermaid') < route.index('| 知识点 |')
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'


def test_multiline_answers_roundtrip_and_file_change_blocks_review(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg, open_question=True); lp.publish(project, model, cfg, data)
    answer = '第一段 **重点** > 判断\n\n**我的回答：**\n第二段\n<!-- 任意文本 -->'
    lp.collect_answers(project, model, {'Q-1': answer}); lp.save(project, model, cfg)
    lp.collect_answers(project, model)
    assert model['lessons'][0]['answers']['Q-1'] == answer
    lp.save(project, model, cfg)
    text = (project / '课程/课程_1-1.md').read_text(encoding='utf-8')
    (project / '课程/课程_1-1.md').write_text(text.replace('第一段', '新回答'), encoding='utf-8')
    review = read_json(Path(model['run_dir']) / 'review.template.json')
    review.update(strengths='已表达观点', weaknesses='需要检查条件')
    review['scores'][0]['unit_scores'] = {'U-0': 1.0}
    with pytest.raises(ValueError, match='作答内容已变化'): lp.review(project, model, cfg, review)


def test_single_shallow_material_and_cross_directory_resource(workspace):
    nav, source, image = source_navigation(workspace)
    pending = creation.prepare(workspace, nav)
    assert pending['suggested_name'] == source.stem
    from utils.scripts.learning_material_display import display_mapping, display_bytes
    outside = workspace / 'shared/extra.png'; outside.parent.mkdir(); outside.write_bytes(b'synthetic image')
    examples = ('`![示例](../shared/extra.png)`\n\n    ![缩进示例](../shared/extra.png)\n'
                '> ~~~\n> ![引用代码](../shared/extra.png)\n> ~~~~\n')
    source.write_text('# 基础\n![跨目录](../shared/extra.png)\n' + examples, encoding='utf-8')
    from utils.scripts.learning_material_backup import collect_files
    files = collect_files(workspace, source)
    mapping = display_mapping(files, source.relative_to(workspace).as_posix(), Path('学习材料'))
    main = next(f for f in files if f['original_path'] == source.relative_to(workspace).as_posix())
    text = display_bytes(workspace, main, mapping).decode('utf-8')
    assert '_resources/' in text and '`![示例](../shared/extra.png)`' in text
    assert examples in text.replace('\r\n', '\n')
    assert Path(mapping[main['original_path']]).parent == Path('学习材料')


def test_chat_answer_file_edits_require_recheck(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg); lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': 'A'}); lp.save(project, model, cfg)
    review.update(strengths='已作答', weaknesses='尚未考查迁移能力')
    review['scores'][0]['feedback'] = '判断正确'
    path = project / '课程/课程_1-1.md'
    path.write_text(path.read_text(encoding='utf-8').replace('> A', '> B'), encoding='utf-8')
    with pytest.raises(ValueError, match='作答内容已变化'): lp.review(project, model, cfg, review)


def test_podcast_gate_embedded_children_and_all_interfaces(workspace, capsys, monkeypatch):
    import shutil
    target = workspace / 'utils/references'; target.mkdir(parents=True)
    for name in ('artifact-manifest-v1.schema.json', 'workflow-state-v1.schema.json'):
        shutil.copyfile(lp.ROOT / 'utils/references' / name, target / name)
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    cfg['podcast']['enabled'] = True
    data = decision(project, model, cfg)
    result = lp.publish(project, model, cfg, data, podcast_mock=True)
    assert result['status'] == 'podcast_required'
    assert not (project / '课程/课程_1-1.md').exists()
    assert lp.verify(project, lp.load(project), cfg)['status'] == 'verified'
    with pytest.raises(ValueError): lp.collect_answers(project, model, {'Q-1': 'A'})
    response = workspace / 'response.json'; write_json(response, {'extra_candidates': []})
    assert runner.main(['resume', '--project-dir', str(project), '--podcast-response', str(response)]) == 3
    pending = json.loads(capsys.readouterr().out)
    rid = pending['podcast']['run_id']
    f = dp.Flow(workspace, 'podcast', rid).load()
    assert f.s['status'] == 'invoking_transcript'
    child = dp.Flow(workspace, 'convert', f.s['children']['transcript']).load()
    write_json(response, {'schema_version': '1.0', 'pairs': [{'source_block_ids': [b['block_id']],
                'question': '应如何判断？', 'answer': '根据当前证据解释。'} for b in child.s['blocks'][:2]]})
    from utils.scripts import structured_io
    replace = structured_io.os.replace
    state_target = Path(model['run_dir'])/'state.json'
    def fail_publication(source, target):
        if Path(target) == state_target: raise OSError('synthetic final publication failure')
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(structured_io.os, 'replace', fail_publication)
        assert runner.main(['resume', '--project-dir', str(project), '--podcast-response', str(response)]) == 5
        capsys.readouterr()
    assert lp.load(project)['state'] == 'podcast_required'
    assert not (project/'课程/课程_1-1.md').exists()
    assert not (project/'播客/课程_1-1.mp3').exists()
    assert runner.main(['resume', '--project-dir', str(project)]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready['status'] == 'awaiting_answer'
    assert str(project) in ready['markdown'] and '学习报告' in ready['markdown']
    text = Path(ready['lesson']).read_text(encoding='utf-8')
    assert text.splitlines()[2].startswith('[🎧')
    model = lp.load(project); podcast = model['lessons'][0]['podcast']
    assert (project / podcast['audio_path']).is_file()
    assert Path(podcast['audio_path']) == Path('播客') / Path(model['lessons'][0]['filename']).with_suffix('.mp3')
    checkpoint = Path(model['run_dir'])/'lesson-podcasts'/podcast['run_id']/'state.json'
    state = read_json(checkpoint); state['stateHistory'].pop(); state['state'] = state['stateHistory'][-1]
    write_json(checkpoint, state)
    assert runner.main(['resume', '--project-dir', str(project)]) == 0; capsys.readouterr()
    assert read_json(checkpoint)['state'] == 'completed'
    # A transaction failing after replacing the binary audio restores every file.
    from utils.scripts import structured_io
    audio = project/podcast['audio_path']; original_audio = audio.read_bytes()
    doc = project/'课程'/model['lessons'][0]['filename']; original_doc = doc.read_bytes()
    original_replace = structured_io.os.replace
    failure_target = project/'SYNTHETIC-failure-target.md'
    def fail_once(source, target):
        if Path(target) == failure_target: raise OSError('synthetic publication failure')
        return original_replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(structured_io.os, 'replace', fail_once)
        with pytest.raises(OSError):
            structured_io.write_text_transaction({audio: b'new-audio', doc: 'new lecture', failure_target: 'fail'})
    assert audio.read_bytes() == original_audio and doc.read_bytes() == original_doc
    for skill in dp.SKILLS.values():
        for state_path in (workspace / 'logs' / skill / 'runs').glob('*/state.json'):
            state = read_json(state_path)
            if state.get('output_dir'): assert Path(state['output_dir']).is_relative_to(project / 'artifacts')
    for command in ('status', 'report', 'verify', 'resume'):
        assert runner.main([command, '--project-dir', str(project)]) == 0
        capsys.readouterr()
    for command in ('status', 'verify', 'deliver', 'resume'):
        assert dp.main('podcast', [command, '--root', str(workspace), '--run-id', rid]) == 0
        capsys.readouterr()

    # Restoring and publishing the same content must retain the old audio and
    # allocate a separate output location instead of colliding with it.
    lesson = model['lessons'][0]
    old_audio = project / lesson['podcast']['audio_path']
    old_bytes = old_audio.read_bytes()
    model['state'] = 'ready'; model['current_lesson_id'] = None
    lesson['status'] = 'pending'
    cfg['podcast']['enabled'] = True
    repeated = decision(project, model, cfg)
    again = lp.publish(project, model, cfg, repeated, podcast_mock=True)
    assert again['status'] == 'podcast_required'
    assert model['lessons'][0]['podcast']['run_id'] != rid
    assert old_audio.read_bytes() == old_bytes


def test_good_mastery_retains_partial_answer_weakness(workspace):
    project = new_project(workspace, 1)
    model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg, open_question=True)
    lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': '结论正确，条件未说明'})
    lp.save(project, model, cfg)
    review.update(strengths='能说明结论', weaknesses='需要补充适用条件')
    review['scores'][0].update(unit_scores={'U-0': .9}, feedback='缺少条件', difference_notes='结论正确，但遗漏参考答案中的适用条件。', misconceptions=[{
        'unit_id': 'U-0', 'title': '条件遗漏', 'weakness': '遗漏适用条件',
        'rule': '先说明适用条件', 'correction': '补充条件'}])
    lp.review(project, model, cfg, review)
    report = read_json(lp.art(project, '学习报告'))
    assert report['aspects'][0]['classification'] == '良好'
    assert report['strengths'] and any('遗漏适用条件' in item for item in report['weaknesses'])
    text = (project / '课程' / model['lessons'][0]['filename']).read_text(encoding='utf-8')
    assert '知识点 0：90%' in text and 'U-0：90%' not in text


def test_review_requires_each_question_feedback(workspace):
    project = new_project(workspace, 1)
    model, receipt = lp.context(project); cfg = receipt['config']
    data = decision(project, model, cfg)
    lp.publish(project, model, cfg, data)
    review = lp.collect_answers(project, model, {'Q-1': 'A'})
    lp.save(project, model, cfg)
    review.update(strengths='判断正确', weaknesses='迁移能力尚未验证')
    review['scores'][0]['feedback'] = ' \n '
    before = copy.deepcopy(model)
    with pytest.raises(ValueError, match='每道习题'):
        lp.review(project, model, cfg, review)
    assert model == before
    review['scores'][0]['feedback'] = '回答正确，依据符合定义。'
    assert lp.review(project, model, cfg, review)['status'] == 'awaiting_questions'
