"""Synthetic intake, public CLI and read-only discovery regression coverage."""
from pathlib import Path
import copy
import json
import subprocess
import sys

import pytest

from .test_interactive_tutor import ROOT, workspace, navigation, runner
from utils.scripts import learning_project_selection as selection, learning_project as lp
from utils.scripts.structured_io import read_json, write_json, validate_json_schema
from utils.scripts.timestamp import iso_timestamp


def register(root, path, run_id='synthetic-run', status='completed'):
    checkpoint = root / 'logs/build-curriculum-navigation/runs' / run_id / 'run-state.json'
    now = iso_timestamp()
    write_json(checkpoint, {'schema_version': '2.0', 'run_id': run_id, 'status': status,
                           'current_stage': status, 'created_at': now, 'updated_at': now,
                           'completed_steps': [], 'pending_decisions': [], 'errors': [],
                           'navigation_json': str(path)})
    return checkpoint


def begin(root):
    receipt = runner.execute(runner.parser().parse_args(['start', '--root', str(root)]))
    directory = Path(receipt['selection_dir'])
    return receipt, directory


def choose(directory, receipt, choice='1', tutor=False):
    result = runner.execute(runner.parser().parse_args([
        'select-tutor' if tutor else 'select-project', '--selection-dir', str(directory),
        '--choice', choice, '--catalog-sha256', receipt['catalog_sha256']]))
    pending = result.get('result') or {}
    if pending.get('status') == 'awaiting_name_confirmation':
        from utils.scripts.learning_project_creation import confirm
        result['result'] = confirm(Path(pending['creation_dir']), pending['suggested_name'], pending['proposal_sha256'])
    return result


def test_read_only_valid_filter_custom_path_and_dedup(workspace):
    path = navigation(workspace)
    checkpoint = register(workspace, path)
    register(workspace, path, 'duplicate')
    register(workspace, workspace / 'missing.json', 'missing')
    register(workspace, path, 'pending', 'awaiting_approval')
    invalid = workspace / 'logs/build-curriculum-navigation/runs/invalid/run-state.json'
    write_json(invalid, [])
    register(workspace, None, 'bad-path')
    broken = workspace / 'outputs/build-curriculum-navigation/runs/broken/navigation.json'
    broken.parent.mkdir(parents=True); broken.write_text('{broken', encoding='utf-8')
    before = {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    catalog = selection.discover(workspace)
    assert len(catalog['entries']) == 1
    assert catalog['entries'][0]['navigation_path'] == 'navigation.json'
    assert before == {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    assert checkpoint.exists()


def test_tutor_registry_queries_are_read_only_even_for_missing_projects(workspace):
    path = navigation(workspace); register(workspace, path)
    project = Path(lp.create(workspace, path, confirmed_name="合成测试课程")['project_dir'])
    missing = workspace / 'outputs/missing-tutor'
    write_json(workspace / 'logs/interactive-tutor/runs/missing/state.json', {'project_dir': str(missing)})
    before_files = {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    before_dirs = {p for p in workspace.rglob('*') if p.is_dir()}
    catalog = selection.discover(workspace)
    assert len(catalog['entries'][0]['tutor_projects']) == 1
    assert before_files == {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    assert before_dirs == {p for p in workspace.rglob('*') if p.is_dir()}
    assert project.exists() and not missing.exists()


def test_default_published_run_without_logs(workspace):
    path = navigation(workspace)
    destination = workspace / 'outputs/build-curriculum-navigation/runs/published/navigation.json'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(path.read_bytes())
    destination.with_suffix('.md').write_bytes(path.with_suffix('.md').read_bytes())
    destination.with_suffix('.sources').mkdir()
    assert len(selection.discover(workspace)['entries']) == 1


@pytest.mark.parametrize('status', ['awaiting_approval', 'paused_error', 'invalid', 'incomplete_state'])
def test_unfinished_or_corrupt_checkpoint_cannot_use_output_fallback(workspace, status):
    path = navigation(workspace)
    destination = workspace / 'outputs/build-curriculum-navigation/runs/published/navigation.json'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(path.read_bytes())
    destination.with_suffix('.md').write_bytes(path.with_suffix('.md').read_bytes())
    destination.with_suffix('.sources').mkdir()
    checkpoint = register(workspace, destination, 'published', status)
    if status == 'invalid': checkpoint.write_text('{broken', encoding='utf-8')
    if status == 'incomplete_state': write_json(checkpoint, {'status': 'completed', 'run_id': 'published', 'navigation_json': str(destination)})
    assert selection.discover(workspace)['entries'] == []


def test_latest_run_status_controls_shared_path(workspace):
    path = navigation(workspace)
    register(workspace, path, '20261002T100000')
    register(workspace, path, '20261002T100001', 'ordering_decision_required')
    assert selection.discover(workspace)['entries'] == []
    register(workspace, path, '20261002T100002')
    assert selection.discover(workspace)['entries'][0]['run_id'] == '20261002T100002'


def test_same_second_run_suffix_order(workspace):
    path = navigation(workspace)
    register(workspace, path, '20261002T100000_2')
    register(workspace, path, '20261002T100000_10', 'paused_error')
    assert selection.discover(workspace)['entries'] == []


def test_multiple_same_title_and_candidate_id_selection(workspace):
    first = navigation(workspace)
    second = workspace / 'second.json'
    second.write_bytes(first.read_bytes())
    second.with_suffix('.md').write_bytes(first.with_suffix('.md').read_bytes())
    second.with_suffix('.sources').mkdir()
    register(workspace, first, 'first'); register(workspace, second, 'second')
    receipt, directory = begin(workspace)
    entries = read_json(directory / 'project-catalog.json')['entries']
    assert len(entries) == 2 and entries[0]['title'] == entries[1]['title']
    assert len({e['candidate_id'] for e in entries}) == 2
    result = choose(directory, receipt, entries[1]['candidate_id'])
    assert Path(lp.load(Path(result['result']['project_dir']))['navigation_json']) == workspace / entries[1]['navigation_path']


def test_empty_start_and_resume_selection(workspace, capsys):
    request = workspace / 'selection-request.json'
    assert runner.main(['init-request', '--file', str(request)]) == 0
    capsys.readouterr()
    assert read_json(request)['input_paths'] == []
    assert runner.main(['start', '--root', str(workspace)]) == 3
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'navigation_required'
    assert not (workspace / 'outputs/interactive-tutor').exists()
    path = navigation(workspace); register(workspace, path)
    assert runner.main(['resume-selection', '--selection-dir', receipt['selection_dir']]) == 3
    assert json.loads(capsys.readouterr().out)['status'] == 'awaiting_project_selection'


def test_one_candidate_waits_then_new_and_idempotent(workspace):
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    assert receipt['status'] == 'awaiting_project_selection'
    assert '序号' in receipt['markdown']
    assert not (workspace / 'outputs/interactive-tutor').exists()
    result = choose(directory, receipt)
    assert result['status'] == 'project_resolved'
    project = Path(result['result']['project_dir'])
    assert (project / '项目.json').exists()
    assert choose(directory, receipt)['result'] == result['result']
    assert len(list((workspace / 'outputs/interactive-tutor/runs').iterdir())) == 1


@pytest.mark.parametrize('choice', ['1', 'new'])
def test_existing_tutor_or_explicit_new(workspace, choice):
    path = navigation(workspace); register(workspace, path)
    project = Path(lp.create(workspace, path, confirmed_name="合成测试课程")['project_dir'])
    receipt, directory = begin(workspace)
    receipt = choose(directory, receipt)
    assert receipt['status'] == 'awaiting_tutor_selection'
    assert '新建导师项目' in receipt['markdown']
    result = choose(directory, receipt, choice, tutor=True)
    picked = Path(result['result']['project_dir'])
    assert (picked == project) == (choice == '1')


def test_catalog_change_and_invalid_choice_preserve_selection(workspace):
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    with pytest.raises(ValueError): choose(directory, receipt, '99')
    assert read_json(directory / 'selection-state.json')['status'] == 'awaiting_project_selection'
    # A new tutor changes the catalog even if the navigation metadata is unchanged.
    lp.create(workspace, path, confirmed_name="合成测试课程")
    refreshed = choose(directory, receipt)
    assert refreshed['status'] == 'awaiting_project_selection'
    assert refreshed['catalog_sha256'] != receipt['catalog_sha256']
    assert choose(directory, refreshed)['status'] == 'awaiting_tutor_selection'


def test_unchanged_metadata_changed_navigation_invalidates_hash(workspace):
    from utils.scripts.learning_navigation import render_navigation_markdown
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    nav = read_json(path); nav['course_overview']['core_questions'] = ['新的合成问题']
    write_json(path, nav); path.with_suffix('.md').write_text(render_navigation_markdown(nav), encoding='utf-8')
    result = choose(directory, receipt)
    assert result['status'] == 'awaiting_project_selection'
    assert result['catalog_sha256'] != receipt['catalog_sha256']


def test_request_without_navigation_preserves_options(workspace):
    path = navigation(workspace); register(workspace, path)
    request = workspace / 'request.json'
    output = workspace / 'outputs/custom-tutor'
    write_json(request, {'schema_version': '3.0', 'input_paths': [], 'interaction_mode': 'chat', 'project_dir': str(output)})
    receipt = runner.execute(runner.parser().parse_args(['start', '--root', str(workspace), '--request', str(request)]))
    result = choose(Path(receipt['selection_dir']), receipt)
    assert Path(result['result']['project_dir']) == output
    assert lp.load(output)['request']['interaction_mode'] == 'chat'
    assert selection.discover(workspace)['entries'][0]['tutor_projects'][0]['project_dir'] == 'outputs/custom-tutor'


def test_backup_approval_exit_code_and_restore_without_source(workspace, capsys):
    from .test_material_backup import source_navigation
    path, source, image = source_navigation(workspace)
    register(workspace, path)
    receipt, directory = begin(workspace)
    assert runner.main(['select-project', '--selection-dir', str(directory), '--choice', '1',
                        '--catalog-sha256', receipt['catalog_sha256']]) == 3
    result = json.loads(capsys.readouterr().out)['result']
    assert result['status'] == 'awaiting_name_confirmation'
    assert runner.main(['confirm-project-name', '--creation-dir', result['creation_dir'], '--name', result['suggested_name'], '--proposal-sha256', result['proposal_sha256']]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'awaiting_backup_approval'
    project = Path(result['project_dir'])
    assert runner.main(['resume', '--project-dir', str(project), '--backup-plan-sha256',
                        result['backup_plan_sha256']]) == 0
    capsys.readouterr()
    source.unlink(); image.unlink()
    receipt, directory = begin(workspace)
    assert '可恢复' in receipt['markdown']
    receipt = choose(directory, receipt)
    result = choose(directory, receipt, '1', tutor=True)
    assert Path(result['result']['project_dir']) == project


def test_restore_keeps_question_gate(workspace):
    from .test_interactive_tutor import complete_lesson
    path = navigation(workspace); register(workspace, path)
    project = Path(lp.create(workspace, path, confirmed_name="合成测试课程")['project_dir'])
    complete_lesson(project)
    receipt, directory = begin(workspace)
    receipt = choose(directory, receipt)
    result = choose(directory, receipt, '1', tutor=True)
    assert result['result']['status'] == 'awaiting_questions'
    assert lp.load(project)['state'] == 'awaiting_questions'


def test_failed_finish_and_interrupted_selection_recover(workspace):
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    def failure(*args): raise ValueError('synthetic callback error')
    with pytest.raises(ValueError):
        selection.select(directory, '1', receipt['catalog_sha256'], finish=failure)
    assert read_json(directory / 'selection-state.json')['status'] == 'awaiting_project_selection'
    state = read_json(directory / 'selection-state.json')
    selection.transition(state, 'validating_selection'); selection.save(directory, state)
    result = runner.execute(runner.parser().parse_args(['resume-selection', '--selection-dir', str(directory)]))
    assert result['status'] == 'awaiting_project_selection'


def test_resume_preserves_second_stage_and_cli_select_tutor(workspace, capsys):
    path = navigation(workspace); register(workspace, path)
    project = Path(lp.create(workspace, path, confirmed_name="合成测试课程")['project_dir'])
    assert runner.main(['list-projects', '--root', str(workspace)]) == 3
    receipt = json.loads(capsys.readouterr().out); directory = Path(receipt['selection_dir'])
    receipt = choose(directory, receipt)
    before = read_json(directory / 'selection-state.json')
    assert runner.main(['resume-selection', '--selection-dir', str(directory)]) == 3
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['status'] == 'awaiting_tutor_selection'
    assert read_json(directory / 'selection-state.json') == before
    assert runner.main(['select-tutor', '--selection-dir', str(directory), '--choice', lp.load(project)['project_id'],
                        '--catalog-sha256', resumed['catalog_sha256']]) == 0
    assert Path(json.loads(capsys.readouterr().out)['result']['project_dir']) == project


def test_catalog_state_transaction_rolls_back(workspace, monkeypatch):
    from utils.scripts import structured_io
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    before = {p: p.read_bytes() for p in (directory / 'selection-state.json', directory / 'project-catalog.json')}
    state = read_json(directory / 'selection-state.json')
    state['markdown'] = 'synthetic replacement'
    original = structured_io.os.replace
    failed = False
    def fail_catalog_once(source, target):
        nonlocal failed
        if Path(target) == directory / 'project-catalog.json' and not failed:
            failed = True
            raise OSError('synthetic catalog publication failure')
        return original(source, target)
    monkeypatch.setattr(structured_io.os, 'replace', fail_catalog_once)
    with pytest.raises(OSError): selection.save(directory, state)
    assert failed
    assert before == {p: p.read_bytes() for p in before}
    assert selection.resume(directory)['status'] == 'awaiting_project_selection'


def test_bad_timestamp_does_not_poison_other_candidates(workspace):
    from utils.scripts.learning_navigation import render_navigation_markdown
    first = navigation(workspace); register(workspace, first, 'first')
    bad = workspace / 'bad-time.json'; nav = read_json(first)
    nav['updated_at'] = 'invalid'
    write_json(bad, nav); bad.with_suffix('.md').write_text(render_navigation_markdown(nav), encoding='utf-8')
    bad.with_suffix('.sources').mkdir(); register(workspace, bad, 'second')
    assert len(selection.discover(workspace)['entries']) == 1


def test_bad_nested_assessment_does_not_poison_other_candidates(workspace):
    first = navigation(workspace); register(workspace, first, 'first')
    bad = workspace / 'bad-assessment.json'; nav = read_json(first)
    nav['planning_profile']['assessment'] = None
    write_json(bad, nav); bad.with_suffix('.md').write_bytes(first.with_suffix('.md').read_bytes())
    bad.with_suffix('.sources').mkdir(); register(workspace, bad, 'second')
    assert len(selection.discover(workspace)['entries']) == 1


def test_illegal_transition_and_all_cli_help():
    with pytest.raises(ValueError): selection.transition({'status': 'project_resolved'}, 'awaiting_project_selection')
    names = runner.parser()._subparsers._group_actions[0].choices
    for command in names:
        result = subprocess.run([sys.executable, str(ROOT / 'skills/interactive-tutor/scripts/cli.py'),
                                 command, '--help'], capture_output=True, text=True)
        assert result.returncode == 0, (command, result.stderr)


def test_selection_schema_and_catalog(workspace):
    path = navigation(workspace); register(workspace, path)
    receipt, directory = begin(workspace)
    state = read_json(directory / 'selection-state.json')
    validate_json_schema(state, ROOT / 'utils/references/learning-project-selection-v1.schema.json')
    validate_json_schema(state['catalog'], ROOT / 'utils/references/learning-project-catalog-v1.schema.json')
    bad = copy.deepcopy(state); bad['status'] = 'unregistered'
    with pytest.raises(ValueError):
        validate_json_schema(bad, ROOT / 'utils/references/learning-project-selection-v1.schema.json')
