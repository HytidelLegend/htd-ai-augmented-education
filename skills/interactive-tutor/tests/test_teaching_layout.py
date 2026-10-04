"""Structured layouts, publication gates, fingerprints and legacy compatibility."""
import copy
from pathlib import Path
import pytest
from .test_interactive_tutor import workspace, new_project, runner
from .test_learning_feedback_routes import modern_decision
from .test_teaching_quality import approve
from utils.scripts import learning_project as lp, learning_teaching_quality as quality
from utils.scripts.learning_teaching_layout import normalize, render_parts
from utils.scripts.structured_io import read_json, write_json


PARTS = [
    {'type': 'paragraph', 'text': '先理解 **条件**，再观察结果。'},
    {'type': 'unordered_list', 'items': ['明确对象', '检查前提\n保留必要背景']},
    {'type': 'ordered_list', 'items': ['记录条件', '逐项核对']},
    {'type': 'formula', 'latex': r'\mathrm{unit}+b=b+\mathrm{unit}', 'caption': '交换顺序不改变和。'},
    {'type': 'code', 'language': 'python', 'code': 'unit = {"lesson": "``` sample"}\n# 保留换行', 'caption': '代码示例'},
    {'type': 'quote', 'text': '引用合成材料 unit lesson。\n保留原文。', 'source': '合成材料', 'url': 'https://example.invalid/unit/lesson'},
]


def test_all_parts_preserve_structure_and_metrics():
    md = render_parts(PARTS)
    assert '**条件**' in md and '\n\n- 明确对象' in md
    assert '\n  保留必要背景' in md and '1. 记录条件\n2. 逐项核对' in md
    assert r'\mathrm{unit}' in md and '````python' in md
    assert '> 引用合成材料 unit lesson。\n> 保留原文。' in md
    assert '[合成材料](https://example.invalid/unit/lesson)' in md
    assert quality.metrics(md)['formula_chars'] > 7
    assert quality.metrics(md)['code_chars'] > 0


def test_v7_publication_review_verify_and_source_tamper(workspace, capsys):
    project = new_project(workspace, 1); model, receipt = lp.context(project); cfg = receipt['config']
    d = modern_decision(project, model, cfg, reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'] = copy.deepcopy(PARTS)
    d['teaching_points'][0]['blocks'][0]['text'] = '过时派生正文应被替换'
    path = Path(model['run_dir']) / 'layout.json'; write_json(path, d)
    assert runner.main(['publish-lesson', '--project-dir', str(project), '--decision', str(path)]) == 3
    import json
    gate = json.loads(capsys.readouterr().out); review = approve(gate)
    r = Path(model['run_dir']) / 'layout-review.json'; write_json(r, review)
    assert runner.main(['publish-lesson', '--project-dir', str(project), '--decision', str(path), '--quality-review', str(r)]) == 0
    capsys.readouterr(); saved = lp.load(project)
    assert lp.verify(project, saved, cfg)['status'] == 'verified'
    lesson = saved['lessons'][0]
    assert '过时派生正文' not in lesson['markdown_template']
    for original in ('unit = {"lesson":', '引用合成材料 unit lesson。', r'\mathrm{unit}', 'https://example.invalid/unit/lesson'):
        assert original in lesson['markdown_template']
        assert original in quality.teaching_body(lesson['content'])
    assert lesson['content']['teaching_points'][0]['blocks'][0]['text'] == render_parts(PARTS)
    changed = copy.deepcopy(lesson); changed['content']['teaching_points'][0]['blocks'][0]['parts'][0]['text'] += '变更'
    with pytest.raises(ValueError): quality.verify_published(changed, lesson['markdown_template'])


def test_invalid_parts_pause_and_recover_without_publication(workspace):
    project = new_project(workspace, 1); model, receipt = lp.context(project)
    d = modern_decision(project, model, receipt['config'], reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'] = [{'type': 'quote', 'text': '无来源'}]
    with pytest.raises(ValueError): normalize(d, model['run_dir'])
    checkpoints = list((Path(model['run_dir'])/'teaching-layout').glob('*/state.json'))
    assert read_json(checkpoints[0])['state'] == 'paused_error'
    assert not (project/'课程'/model['lessons'][0]['filename']).exists()
    d['teaching_points'][0]['blocks'][0]['parts'] = copy.deepcopy(PARTS)
    assert normalize(d,model['run_dir'])['teaching_points'][0]['blocks'][0]['text'] == render_parts(PARTS)
    for bad in [{'type':'paragraph','text':'## 越界标题'}, {'type':'quote','text':'内容','source':'来源','url':'file:///private'}, {'type':'formula','latex':'x$$'}]:
        with pytest.raises(ValueError): render_parts([bad])


def test_v5_body_is_not_migrated():
    d = {'schema_version':'5.0', 'teaching_points':[{'text':'旧正文', 'blocks':[{'text':'旧块'}]}]}
    assert normalize(d) == d
