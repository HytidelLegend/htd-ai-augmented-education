"""Material quotes and context/layout gates use synthetic sources only."""
import copy
import json
from pathlib import Path

import pytest

from .test_interactive_tutor import workspace, new_project, runner
from .test_learning_feedback_routes import modern_decision
from .test_material_backup import source_navigation
from .test_teaching_quality import approve
from utils.scripts import learning_project as lp, learning_teaching_context as context
from utils.scripts.learning_material_backup import backup_plan
from utils.scripts.learning_teaching_layout import normalize, render_parts
from utils.scripts.structured_io import read_json, write_json, json_digest
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint


def source_project(workspace):
    nav, _, _ = source_navigation(workspace, '# 合作\n\n讲者用合作的例子说明：有人擅长整理步骤，有人擅长讲解。\n不同任务需要不同技能。\n')
    project = workspace/'outputs/interactive-tutor/runs/source-project'
    plan = backup_plan(workspace, project, read_json(nav))
    lp.create(workspace, nav, project, approved_plan_sha256=json_digest(plan), confirmed_name='合成合作课程')
    model, receipt = lp.context(project)
    d = modern_decision(project, model, receipt['config'], reviewed=False)
    evidence = read_json(Path(model['run_dir'])/'evidence.json')['evidence']
    return project, model, receipt['config'], d, evidence


def sufficient(gate):
    review = read_json(Path(gate['layout_template']))
    for item in review['items']:
        item.update(judgment='sufficient', reason='删改只保留不同技能的举例，背景及解释已给出，未新增事实判断。')
    return review


@pytest.mark.parametrize('edited', [False, True])
def test_material_quote_cli_publication_verify_and_source_tamper(workspace, capsys, edited):
    project, model, cfg, d, evidence = source_project(workspace)
    block = d['teaching_points'][0]['blocks'][0]
    part = {'type': 'material_quote', 'evidence_id': evidence[0]['evidence_id'], 'edited': edited}
    if edited: part['edited_text'] = '有人擅长整理步骤，有人擅长讲解。不同任务需要不同技能。'
    block['parts'] = [{'type':'paragraph','text':'下面的材料用合作场景说明不同任务需要不同技能。'}, part,
                      {'type':'paragraph','text':'从这个例子可以看出，整理步骤和当众讲解是两种不同的技能，可以根据具体任务互相学习。'}]
    input_path = Path(model['run_dir'])/'quote.json'; write_json(input_path, d)
    args = ['publish-lesson','--project-dir',str(project),'--decision',str(input_path)]
    assert runner.main(args) == 3
    gate = json.loads(capsys.readouterr().out)
    if gate['status'] == 'teaching_layout_review_required':
        assert gate['status'] == 'teaching_layout_review_required'
        assert lp.load(project)['state'] == 'lesson_decision_required'
        assert not (project/'课程'/model['lessons'][0]['filename']).exists()
        review_path = Path(model['run_dir'])/'context-review.json'; write_json(review_path, sufficient(gate))
        args += ['--layout-review',str(review_path)]
        assert runner.main(args) == 3
        gate = json.loads(capsys.readouterr().out)
    assert gate['status'] == 'teaching_review_required'
    quality_path = Path(model['run_dir'])/'quality-review.json'; write_json(quality_path, approve(gate))
    assert runner.main(args+['--quality-review',str(quality_path)]) == 0
    capsys.readouterr()
    assert runner.main(['verify','--project-dir',str(project)]) == 0; capsys.readouterr()
    saved = lp.load(project); lesson = saved['lessons'][0]
    rendered = lesson['content']['teaching_points'][0]['blocks'][0]['parts'][1]
    assert '> （有删改）' in lesson['markdown_template'] if edited else '> # 合作' in lesson['markdown_template']
    assert '第 1–4 行' in lesson['markdown_template']
    assert rendered['resolved']['text'] == ('（有删改）'+part['edited_text'] if edited else evidence[0]['text'])
    changed = copy.deepcopy(lesson['content'])
    changed['teaching_points'][0]['blocks'][0]['parts'][1]['resolved']['text'] += '篡改'
    with pytest.raises(ValueError): context.verify_record(changed, lesson['teaching_context'], workspace)
    checkpoint = Path(model['run_dir'])/'teaching-context'/d['lesson_id']/lesson['teaching_context']['fingerprint']/'state.json'
    original = checkpoint.read_bytes()
    state = read_json(checkpoint); state['stateHistory'].append('prepared'); write_json(checkpoint,state)
    assert runner.main(['verify','--project-dir',str(project)]) == 4; capsys.readouterr()
    checkpoint.write_bytes(original)
    archived = checkpoint.parent/'review.json'
    original = archived.read_bytes()
    review = read_json(archived); review['items'][0]['reason'] += '篡改'; write_json(archived,review)
    assert runner.main(['verify','--project-dir',str(project)]) == 4; capsys.readouterr()
    archived.write_bytes(original)
    source = workspace/evidence[0]['source_path']; source.write_text('变更', encoding='utf-8')
    assert runner.main(['verify','--project-dir',str(project)]) == 4
    capsys.readouterr()


def test_context_and_steps_review_resume_stale_revision_and_no_bypass(workspace):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    d = modern_decision(project,model,receipt['config'],reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'] = [
        {'type':'paragraph','text':'材料中，讲者引用合作例子。可以把比较分成三步：先指定任务，再观察做法，最后决定是否学习。'}]
    evidence = read_json(Path(model['run_dir'])/'evidence.json')['evidence']
    gate, _ = context.evaluate(d,evidence,workspace,model['run_dir'])
    packet = read_json(Path(gate['layout_packet']))
    assert '前置背景' in packet['findings'][0]['reason'] and 'ordered_list' in packet['findings'][0]['reason']
    assert context.evaluate(d,evidence,workspace,model['run_dir'])[0] == gate
    review = sufficient(gate); stale = copy.deepcopy(review); stale['fingerprint'] = '0'*64
    before = (Path(gate['layout_template']).parent/'state.json').read_bytes()
    with pytest.raises(ValueError): context.evaluate(d,evidence,workspace,model['run_dir'],stale)
    assert (Path(gate['layout_template']).parent/'state.json').read_bytes() == before
    flow = WorkflowCheckpoint(context.TRANSITIONS,Path(gate['layout_template']).parent,resume=True,restart_completed=False)
    flow.move('validating_review')  # Simulate interrupted review.
    review['items'][0]['judgment'] = 'needs_expansion'
    assert context.evaluate(d,evidence,workspace,model['run_dir'],review)[0]['status'] == 'teaching_layout_revision_required'
    review['items'][0]['judgment'] = 'sufficient'
    assert context.evaluate(d,evidence,workspace,model['run_dir'],review)[0]['status'] == 'teaching_layout_revision_required'
    changed = copy.deepcopy(d); changed['questions'][0]['prompt'] += '新题'
    assert context.evaluate(changed,evidence,workspace,model['run_dir'])[0]['status'] == 'teaching_layout_revision_required'
    changed = copy.deepcopy(d); changed['teaching_points'][0]['blocks'][0]['block_id'] += '-new'
    changed['teaching_points'][0]['blocks'][0]['coverage'] = 'mentioned'
    assert context.evaluate(changed,evidence,workspace,model['run_dir'])[0]['status'] == 'teaching_layout_revision_required'
    d['teaching_points'][0]['blocks'][0]['parts'] = [
        {'type':'paragraph','text':'比较可以按以下步骤进行。'},
        {'type':'ordered_list','items':['指定任务','观察做法','决定是否学习']}]
    assert context.evaluate(d,evidence,workspace,model['run_dir'])[0] is None


def test_quotes_schema_bindings_and_quote_only_coverage(workspace):
    project, model, cfg, d, evidence = source_project(workspace)
    block = d['teaching_points'][0]['blocks'][0]
    block['parts'] = [{'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':False}]
    normalized = normalize(d,model['run_dir'],evidence=evidence,root=workspace)
    with pytest.raises(ValueError, match='引用块不能单独'): lp.content_tools.validate_teaching(normalized,model['lessons'][0],model)
    for invalid in [
        {'type':'material_quote','evidence_id':'missing','edited':False},
        {'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':True},
        {'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':False,'edited_text':'偷改'},
        {'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':True,'edited_text':'（有删改）重复'},
    ]:
        block['parts'] = [invalid]
        with pytest.raises(ValueError): normalize(d,model['run_dir'],evidence=evidence,root=workspace)
    block['parts'] = [{'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':False}]
    altered = copy.deepcopy(evidence); altered[0]['text'] += '伪造'
    with pytest.raises(ValueError): normalize(d,model['run_dir'],evidence=altered,root=workspace)
    for change in [{'unit_id':'other-unit'}, {'evidence_kind':'navigation_context'},
                   {'locator':{**evidence[0]['locator'],'start_line':0}}]:
        altered = copy.deepcopy(evidence); altered[0].update(change)
        with pytest.raises(ValueError): normalize(d,model['run_dir'],evidence=altered,root=workspace)
    block['parts'][0]['resolved'] = {'text':'伪造正文','source':'伪造来源','evidence':{}}
    normalized = normalize(d,model['run_dir'],evidence=evidence,root=workspace)
    assert normalized['teaching_points'][0]['blocks'][0]['parts'][0]['resolved']['text'] == evidence[0]['text']


def test_legacy_v6_layout_and_punctuation_are_preserved(workspace):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    d = modern_decision(project,model,receipt['config'],reviewed=False); d['schema_version'] = '6.0'
    d['teaching_points'][0]['blocks'][0]['parts'] = [{'type':'paragraph','text':'原句；保留标点。'}]
    assert normalize(d)['teaching_points'][0]['blocks'][0]['text'] == '原句；保留标点。'
    assert context.evaluate(d,[],workspace,model['run_dir']) == (None,None)


def test_review_persistence_failure_pauses_and_recovers(workspace, monkeypatch):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    d = modern_decision(project,model,receipt['config'],reviewed=False)
    original = context.write_json
    def fail_packet(path, data):
        if path.name == 'packet.json': raise OSError('synthetic persistence failure')
        return original(path,data)
    monkeypatch.setattr(context,'write_json',fail_packet)
    with pytest.raises(OSError): context.evaluate(d,[],workspace,model['run_dir'])
    run = Path(model['run_dir'])/'teaching-context'/d['lesson_id']/json_digest(context.review_basis(d,[]))
    assert read_json(run/'state.json')['state'] == 'paused_error'
    monkeypatch.setattr(context,'write_json',original)
    gate, record = context.evaluate(d,[],workspace,model['run_dir'])
    assert gate is None and record
    assert read_json(run/'state.json')['state'] == 'completed'
    assert context.evaluate(d,[],workspace,model['run_dir']) == (None,record)


def test_all_public_commands_help_and_layout_argument(capsys):
    parser = runner.parser()
    names = parser._subparsers._group_actions[0].choices
    for name in names:
        with pytest.raises(SystemExit) as exit:
            parser.parse_args([name,'--help'])
        assert exit.value.code == 0
        capsys.readouterr()
    args = parser.parse_args(['publish-lesson','--project-dir','synthetic',
                              '--decision','lesson.json','--layout-review','review.json'])
    assert args.layout_review == Path('review.json')


@pytest.mark.parametrize('judgment', ['sufficient', 'needs_expansion'])
@pytest.mark.parametrize('state', ['validating_review', 'paused_error'])
def test_archived_review_replays_after_interruption(workspace, judgment, state):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    d = modern_decision(project,model,receipt['config'],reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '材料中，讲者引用合作例子。'
    gate, _ = context.evaluate(d,[],workspace,model['run_dir'])
    review = sufficient(gate); review['items'][0]['judgment'] = judgment
    run = Path(gate['layout_template']).parent
    flow = WorkflowCheckpoint(context.TRANSITIONS,run,resume=True,restart_completed=False)
    flow.move('validating_review'); write_json(run/'review.json', review)
    if state == 'paused_error': flow.move('paused_error')
    contradictory = copy.deepcopy(review); contradictory['items'][0]['judgment'] = 'sufficient' if judgment == 'needs_expansion' else 'needs_expansion'
    gate, record = context.evaluate(d,[],workspace,model['run_dir'],contradictory)
    if judgment == 'needs_expansion': assert gate['status'] == 'teaching_layout_revision_required' and record is None
    else:
        assert gate is None and record['review'] == review
        context.verify_record(d,record,workspace,run_dir=model['run_dir'])
    assert read_json(run/'review.json') == review


def test_lists_punctuation_and_unrelated_quotes_are_reviewed():
    d = {'teaching_points':[{'blocks':[{'block_id':'B','parts':[
        {'type':'material_quote','edited':False},
        {'type':'unordered_list','items':['讲者说另一个例子。', '先指定任务，再观察做法，最后决定是否学习。']},
        {'type':'paragraph','text':'逐字稿记录的是講者的说法；本课不把这些讲述当作已独立核实的历史事实。'},
    ]}]}]}
    reason = context.findings(d)[0]['reason']
    assert '必要背景' in reason and '不相关引用' in reason and 'ordered_list' in reason and '独立句子' in reason
    d['teaching_points'][0]['blocks'][0]['parts'] = [{'type':'quote','text':'原文；未经核实。','source':'合成材料'}]
    assert context.findings(d) == []


def test_damaged_pending_packet_is_not_regenerated(workspace):
    project = new_project(workspace,1); model, receipt = lp.context(project)
    d = modern_decision(project,model,receipt['config'],reviewed=False)
    d['teaching_points'][0]['blocks'][0]['parts'][0]['text'] = '材料中，讲者引用合作例子。'
    gate, _ = context.evaluate(d,[],workspace,model['run_dir'])
    path = Path(gate['layout_packet']); packet = read_json(path); packet['findings'] = []; write_json(path,packet)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='拒绝覆盖'): context.evaluate(d,[],workspace,model['run_dir'])
    assert path.read_bytes() == before


def test_preexisting_v7_record_uses_frozen_review_policy(workspace):
    project, model, cfg, d, evidence = source_project(workspace)
    d['teaching_points'][0]['blocks'][0]['parts'].insert(0, {'type':'material_quote','evidence_id':evidence[0]['evidence_id'],'edited':False})
    d = normalize(d,model['run_dir'],evidence=evidence,root=workspace)
    basis = context.review_basis(d,evidence); basis.pop('policy_version')
    record = {'basis':basis,'fingerprint':json_digest(basis),'findings':context.findings(d,version=1),'review':None}
    assert record['findings'] == [] and context.findings(d)
    context.verify_record(d,record,workspace)


def test_contract_generation_rolls_back_all_documents(workspace, monkeypatch):
    import shutil
    from utils.scripts import render_learning_teaching_context_contract as contract, structured_io
    relatives = ['utils/references/interactive-tutor-lesson-decision-v6.schema.json',
                 'utils/references/interactive-tutor-lesson-decision-v7.schema.json',
                 'skills/interactive-tutor/SKILL.md','docs/Skills、应用说明书.md','docs/PRDs/交互式学习.md',
                 'applications/交互式学习/README.md']
    for relative in relatives:
        target = workspace/relative; target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(contract.ROOT/relative,target)
    original = {workspace/r:(workspace/r).read_bytes() for r in relatives}
    replace = structured_io.os.replace
    def fail(source,target):
        if Path(target) == workspace/'applications/交互式学习/README.md': raise OSError('synthetic document publication failure')
        return replace(source,target)
    monkeypatch.setattr(structured_io.os,'replace',fail)
    with pytest.raises(OSError): contract.generate(workspace)
    assert all(path.read_bytes() == data for path,data in original.items())
    runs = list((workspace/'logs/interactive-tutor/runs').glob('*/contract-generation/state.json'))
    assert len(runs) == 1 and read_json(runs[0])['state'] == 'paused_error'
    monkeypatch.setattr(structured_io.os,'replace',replace)
    result = contract.generate(workspace,run_dir=runs[0].parent)
    assert result['status'] == 'completed'
    assert read_json(runs[0])['stateHistory'].count('preparing') == 2
    assert contract.generate(workspace,run_dir=runs[0].parent) == result
    with pytest.raises(ValueError, match='检查点必须位于'):
        contract.generate(workspace,run_dir=workspace/'runtime/contract-generation')
    assert not (workspace/'runtime/contract-generation').exists()
