"""Exercise the real CLI and shared publication gates in disposable repositories."""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('update_version_workflow', ROOT/'skills/update-project-version/scripts/version_workflow.py')
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
from utils.scripts import version_workflow as engine
from utils.scripts import recent_skill_runs as recent
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.structured_io import write_text_transaction
from utils.scripts.version_history import history_findings

# Reuse the existing release fixture and evidence authoring helper.
fixture_spec = importlib.util.spec_from_file_location('release_fixtures', ROOT/'skills/project-doc-audit/scripts/test_version_history.py')
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)
repo = fixtures.repo
git, put = fixtures.git, fixtures.put
CLI = ROOT/'skills/update-project-version/scripts/cli.py'


def review_path(root, state):
    log, _ = workflow.directories(root, state['run_id'])
    value = json.loads((log/'review-template.json').read_text(encoding='utf-8'))
    for item in value['commits']:
        ids = [e['evidence_id'] for e in item['skipped']]
        item['updates'] = [{'summary': '增加新功能', 'evidence_ids': ids}] if ids else []
        item['skipped'] = []
        item['no_core_reason'] = '没有核心内容' if not ids else ''
    path = log/'input.json'
    put(root, str(path.relative_to(root)), json.dumps(value, ensure_ascii=False))
    return path


def preview(root, staged=True):
    if staged:
        put(root, 'feature.txt', 'staged feature\n')
        git(root, 'add', 'feature.txt')
    state = workflow.start(root)
    assert state['status'] == 'awaiting_agent_review'
    state = workflow.execute(root, state['run_id'], 'resume', review_path(root, state))
    assert state['status'] == 'awaiting_user_confirmation', state.get('error')
    return state


def confirmation(root, state):
    log, _ = workflow.directories(root, state['run_id'])
    value = json.loads((log/'confirmation-template.json').read_text(encoding='utf-8'))
    value['confirmed'] = True
    path = log/'human-input.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def confirm(root, state):
    value = workflow.execute(root, state['run_id'], 'confirm', confirmation(root, state))
    assert value['status'] == 'confirmed', value.get('error')
    return value


def test_user_gate_and_publication_audit(repo):
    before = engine.snapshots(repo)
    state = preview(repo)
    assert engine.snapshots(repo) == before
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'awaiting_user_confirmation'
    rejected = workflow.execute(repo, state['run_id'], 'publish')
    assert rejected['status'] == 'paused_error'
    assert engine.snapshots(repo) == before
    confirm(repo, state)
    assert engine.snapshots(repo) == before
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    assert history_findings(repo, 'staged')[0] == []
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    log, out = workflow.directories(repo, state['run_id'])
    assert (out/'reminders.md').exists()
    assert json.loads((log/'confirmation.json').read_text(encoding='utf-8'))['confirmed_at']
    assert len(git(repo, 'rev-list', '--first-parent', 'HEAD').splitlines()) == 1


@pytest.mark.parametrize('kind', ['stage', 'head', 'proposal', 'journal', 'readme', 'confirmation'])
def test_confirmation_stale_or_tampered_blocks_publish(repo, kind):
    state = confirm(repo, preview(repo))
    log, out = workflow.directories(repo, state['run_id'])
    if kind == 'stage':
        put(repo, 'feature.txt', 'new stage'); git(repo, 'add', 'feature.txt')
    elif kind == 'head':
        git(repo, 'commit', '-m', 'new head')
    elif kind == 'proposal':
        (out/'proposal.md').write_text('tampered', encoding='utf-8')
    elif kind == 'journal':
        (log/'publication.json').write_text('{}', encoding='utf-8')
    elif kind == 'confirmation':
        (log/'confirmation.json').write_text('{}', encoding='utf-8')
    else:
        put(repo, 'README.md', 'user edit')
    before = engine.snapshots(repo)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
    assert engine.snapshots(repo) == before


def test_confirmation_wrong_fingerprint_and_false_flag(repo):
    state = preview(repo)
    path = confirmation(repo, state)
    value = json.loads(path.read_text(encoding='utf-8'))
    value['confirmed'] = False
    path.write_text(json.dumps(value), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'confirm', path)['status'] == 'paused_error'
    value['confirmed'] = True; value['packet_sha256'] = '0'*64
    path.write_text(json.dumps(value), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'confirm', path)['status'] == 'paused_error'
    confirm(repo, state)


def test_no_core_and_repeated_same_candidate(repo):
    state = confirm(repo, preview(repo, staged=False))
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    before = engine.snapshots(repo)
    state = confirm(repo, preview(repo, staged=False))
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    assert engine.snapshots(repo) == before
    assert '本次无核心更新' in (repo/'docs/更新历史.md').read_text(encoding='utf-8')


def test_publication_interruption_resumes(repo, monkeypatch):
    state = confirm(repo, preview(repo))
    real = engine.write_text_transaction
    def failing(updates):
        if repo/'VERSION' in updates:
            raise OSError('injected failure')
        return real(updates)
    monkeypatch.setattr(engine, 'write_text_transaction', failing)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
    monkeypatch.setattr(engine, 'write_text_transaction', real)
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'completed'


@pytest.mark.parametrize('new_file', [False, True])
def test_cli_all_interfaces(repo, new_file):
    if new_file:
        put(repo, '新目录/new file.txt', 'new content\n')
    def call(*args):
        return subprocess.run([sys.executable, str(CLI), *args, '--root', str(repo)], capture_output=True, text=True, encoding='utf-8')
    result = call('start')
    assert result.returncode == 3, result.stderr
    state = json.loads(result.stdout); run = state['run_id']
    assert call('status', '--run-id', run).returncode == 0
    assert call('verify', '--run-id', run).returncode == 4
    assert call('deliver', '--run-id', run).returncode == 4
    assert call('resume', '--run-id', run, '--input', str(review_path(repo, state))).returncode == 3
    assert call('verify', '--run-id', run).returncode == 0
    assert '新版本' in call('deliver', '--run-id', run).stdout
    assert call('confirm', '--run-id', run, '--input', str(confirmation(repo, state))).returncode == 0
    assert call('publish', '--run-id', run).returncode == 0
    assert call('resume', '--run-id', run).returncode == 0
    assert call('verify', '--run-id', run).returncode == 0
    assert call('deliver', '--run-id', run).returncode == 0
    assert call('status').returncode == 2
    assert call('verify', '--run-id', '../bad').returncode == 4


def test_untracked_evidence_and_identical_staging(repo):
    put(repo, '新目录/new file.txt', '```\n新增内容\n```\n')
    put(repo, 'empty.txt', '')
    (repo/'binary.bin').write_bytes(b'\0\xff\x01')
    put(repo, 'outputs/ignored.txt', 'ignored')
    state = workflow.start(repo)
    log, out = workflow.directories(repo, state['run_id'])
    packet = json.loads((log/'packet.json').read_text(encoding='utf-8'))
    entries = {e['path']: e for e in packet['commits'][0]['evidence']}
    assert set(entries) == {'新目录/new file.txt', 'empty.txt', 'binary.bin'}
    assert all(e['source'] == 'untracked' for e in entries.values())
    assert entries['binary.bin']['file_type'] == 'binary'
    assert entries['empty.txt']['size_bytes'] == 0
    assert '新增内容' in (out/'diff.md').read_text(encoding='utf-8')
    state = workflow.execute(repo, state['run_id'], 'resume', review_path(repo, state))
    assert state['status'] == 'awaiting_user_confirmation', state.get('error')
    git(repo, 'add', '新目录', 'empty.txt', 'binary.bin')
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    state = confirm(repo, state)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', *engine.TARGETS)
    assert history_findings(repo, 'staged')[0] == []


@pytest.mark.parametrize('change', ['edit', 'delete', 'add', 'ignore', 'stage_changed'])
def test_new_file_changes_invalidate_confirmation(repo, change):
    put(repo, 'new.txt', 'original\n')
    state = confirm(repo, preview(repo, staged=False))
    if change == 'edit':
        put(repo, 'new.txt', 'changed\n')
    elif change == 'delete':
        (repo/'new.txt').unlink()
    elif change == 'add':
        put(repo, 'extra.txt', 'extra\n')
    elif change == 'ignore':
        put(repo, '.git/info/exclude', 'new.txt\n')
    else:
        put(repo, 'new.txt', 'changed\n')
        git(repo, 'add', 'new.txt')
    before = engine.snapshots(repo)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
    assert engine.snapshots(repo) == before


def test_unstaged_new_file_publish_then_staged_audit(repo):
    put(repo, 'new.txt', 'candidate\n')
    state = confirm(repo, preview(repo, staged=False))
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', *engine.TARGETS)
    assert 'changelog_review_missing' in {f['kind'] for f in history_findings(repo, 'staged')[0]}
    git(repo, 'add', 'new.txt')
    assert history_findings(repo, 'staged')[0] == []
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    put(repo, 'new.txt', 'wrong staged content\n')
    git(repo, 'add', 'new.txt')
    assert 'changelog_review_missing' in {f['kind'] for f in history_findings(repo, 'staged')[0]}


@pytest.mark.parametrize('path', ['README.md', '.claude-plugin/marketplace.json'])
def test_version_document_content_survives_publication_staging(repo, path):
    if path == 'README.md':
        put(repo, path, (repo/path).read_text(encoding='utf-8') + '\nNew capability\n')
    else:
        value = json.loads((repo/path).read_text(encoding='utf-8'))
        value['description'] = 'new capability'
        put(repo, path, json.dumps(value))
    git(repo, 'add', path)
    put(repo, 'new.txt', 'new addition\n')
    state = confirm(repo, preview(repo, staged=False))
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', *engine.TARGETS, 'new.txt')
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    assert history_findings(repo, 'staged')[0] == []
    if path == 'README.md':
        put(repo, path, (repo/path).read_text(encoding='utf-8') + '\nLater change\n')
    else:
        value = json.loads((repo/path).read_text(encoding='utf-8'))
        value['description'] = 'later change'
        put(repo, path, json.dumps(value))
    git(repo, 'add', path)
    assert 'changelog_review_missing' in {f['kind'] for f in history_findings(repo, 'staged')[0]}


def test_extra_staged_file_rejects_published_candidate(repo):
    state = confirm(repo, preview(repo))
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', *engine.TARGETS)
    assert history_findings(repo, 'staged')[0] == []
    put(repo, 'extra.txt', 'unreviewed addition\n')
    git(repo, 'add', 'extra.txt')
    assert 'changelog_review_missing' in {f['kind'] for f in history_findings(repo, 'staged')[0]}


def test_new_file_mode_change_invalidates_review(repo):
    put(repo, 'new.txt', 'new addition\n')
    state = preview(repo, staged=False)
    git(repo, 'add', 'new.txt')
    git(repo, 'update-index', '--chmod=+x', 'new.txt')
    with pytest.raises(ValueError, match='新文件暂存内容已变化'):
        workflow.execute(repo, state['run_id'], 'verify')


def test_spaced_directory_does_not_confuse_patch_paths(repo):
    put(repo, 'foo.txt', 'first addition\n')
    put(repo, 'x b/foo.txt', 'second addition\n')
    state = preview(repo, staged=False)
    git(repo, 'add', 'foo.txt', 'x b/foo.txt')
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']


@pytest.mark.parametrize('invalid', ['missing_hash', 'invalid_type', 'patch_hash', 'parent', 'duplicate'])
def test_candidate_structure_and_semantic_bindings(repo, invalid):
    from utils.scripts.commit_history import collect_packet, assert_candidate
    from utils.scripts.structured_io import json_digest
    put(repo, 'new.txt', 'new addition\n')
    packet = collect_packet(repo, 'staged', include_untracked=True)
    entry = packet['commits'][0]['evidence'][0]
    git(repo, 'add', 'new.txt')
    if invalid == 'missing_hash':
        entry.pop('content_sha256')
    elif invalid == 'invalid_type':
        entry['file_type'] = 'unknown'
    elif invalid == 'patch_hash':
        entry['patch'] += 'tampered'
    elif invalid == 'parent':
        packet['commits'][0]['parent'] = '0' * 40
    else:
        packet['commits'][0]['evidence'].append(dict(entry))
    packet['packet_sha256'] = json_digest({k: v for k, v in packet.items() if k != 'packet_sha256'})
    with pytest.raises(ValueError):
        assert_candidate(repo, packet)


def test_legacy_update_run_remains_usable_with_untracked_files(repo, monkeypatch):
    real_collect = engine.collect_packet
    with monkeypatch.context() as context:
        context.setattr(engine, 'collect_packet', lambda root, mode, **kwargs: real_collect(root, mode))
        state = workflow.start(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    state.pop('include_untracked')
    engine.save_state(log, state)
    put(repo, 'new.txt', 'outside legacy scope\n')
    state = workflow.execute(repo, state['run_id'], 'resume', review_path(repo, state))
    assert state['status'] == 'awaiting_user_confirmation'
    state = confirm(repo, state)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', *engine.TARGETS)
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    assert history_findings(repo, 'staged')[0] == []


def test_new_scope_does_not_include_unstaged_tracked_edits(repo):
    put(repo, 'feature.txt', 'unselected edit\n')
    put(repo, 'new.txt', 'selected addition\n')
    state = workflow.start(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    packet = json.loads((log/'packet.json').read_text(encoding='utf-8'))
    assert [e['path'] for e in packet['commits'][0]['evidence']] == ['new.txt']


def test_legacy_packet_retains_staged_scope(repo):
    state = engine.start(repo, 'staged')
    put(repo, 'new.txt', 'not part of legacy review\n')
    log, _ = engine.directories(repo, state['run_id'])
    packet = engine.assert_packet(repo, log, state)
    assert packet['schema_version'] == '1.0'
    assert packet['commits'][0]['evidence'] == []


def test_new_file_staging_with_git_newline_conversion(repo):
    git(repo, 'config', 'core.autocrlf', 'true')
    (repo/'new.txt').write_bytes(b'first\r\nsecond\r\n')
    state = preview(repo, staged=False)
    git(repo, 'add', 'new.txt')
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']


def test_untracked_symlink_does_not_follow_target(repo):
    import os
    try:
        os.symlink('feature.txt', repo/'new-link')
    except OSError:
        pytest.skip('Symbolic link creation unavailable on this host')
    state = workflow.start(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    packet = json.loads((log/'packet.json').read_text(encoding='utf-8'))
    entry = packet['commits'][0]['evidence'][0]
    assert entry['file_type'] == 'symlink'
    assert 'feature.txt' in entry['patch']
    assert '+first' not in entry['patch']


@pytest.mark.parametrize('skill', recent.SKILLS)
def test_recent_start_boundary_and_incomplete(repo, monkeypatch, skill):
    current = datetime(2026, 10, 4, 15)
    states = [('boundary', 3600, 'semantic_review_required'), ('old', 3601, 'approved'),
              ('done', 10, 'completed' if skill == 'project-doc-audit' else 'approved'),
              ('failed', 20, 'failed'), ('future', -1, 'approved')]
    for run, seconds, status in states:
        put(repo, f'logs/{skill}/runs/{run}/state.json', json.dumps({'created_at': iso_timestamp(current-timedelta(seconds=seconds)), 'status': status}))
    put(repo, f'logs/{skill}/runs/broken/state.json', '[]')
    put(repo, f'logs/{skill}/runs/history/history/state.json', json.dumps({'created_at': iso_timestamp(current), 'status': 'completed'}))
    monkeypatch.setattr(recent, 'check_verified', lambda root, skill, run: run == 'done')
    report = recent.collect_recent_runs(repo, iso_timestamp(current))
    item = report['skills'][skill]
    assert {r['run_id'] for r in item['runs']} == {'boundary', 'done', 'failed'}
    assert set(item['invalid_records']) == {'broken', 'future'}
    text = recent.render_reminders(report)
    assert '尚未完成' in text and '失败或暂停' in text and '已完成并通过核验' in text
    assert '请在提交代码之前运行' in text  # The other check is absent.


def test_failed_verification_and_queries_do_not_refresh_start(repo, monkeypatch):
    put(repo, 'logs/project-doc-audit/runs/x/state.json', json.dumps({'created_at': '2026-10-04T12:00:00', 'updated_at': '2026-10-04T15:00:00', 'status': 'completed'}))
    assert not recent.collect_recent_runs(repo, '2026-10-04T15:00:00')['skills']['project-doc-audit']['runs']
    put(repo, 'logs/project-doc-audit/runs/y/state.json', json.dumps({'created_at': '2026-10-04T14:59:00', 'status': 'completed'}))
    monkeypatch.setattr(recent, 'check_verified', lambda *a: False)
    assert '未通过核验' in recent.render_reminders(recent.collect_recent_runs(repo, '2026-10-04T15:00:00'))


def test_unstaged_preview_content_is_preserved(repo):
    put(repo, 'README.md', (repo/'README.md').read_text(encoding='utf-8')+'\nunstaged content')
    before = engine.snapshots(repo)
    state = workflow.start(repo)
    assert workflow.execute(repo, state['run_id'], 'resume', review_path(repo, state))['status'] == 'paused_error'
    assert engine.snapshots(repo) == before


def test_resume_without_confirmation_cannot_publish(repo):
    state = preview(repo)
    before = engine.snapshots(repo)
    workflow.execute(repo, state['run_id'], 'publish')
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'awaiting_user_confirmation'
    assert engine.snapshots(repo) == before
    assert not (workflow.directories(repo, state['run_id'])[0]/'confirmation.json').exists()


def test_interrupted_confirmation_is_retryable(repo):
    state = preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    engine.move(log, state, 'validating_confirmation')
    confirm(repo, state)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'


def test_interrupted_publication_after_write_and_reminder_tamper(repo):
    state = confirm(repo, preview(repo))
    log, out = workflow.directories(repo, state['run_id'])
    engine.move(log, state, 'publishing')
    journal = json.loads((log/'publication.json').read_text(encoding='utf-8'))
    write_text_transaction({repo/p: text for p, text in journal['updates'].items()})
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'completed'
    (out/'reminders.md').write_text('changed reminder', encoding='utf-8')
    with pytest.raises(ValueError, match='提醒'):
        workflow.execute(repo, state['run_id'], 'verify')


def test_confirmed_cross_run_lock_and_resume(repo):
    state = confirm(repo, preview(repo))
    before = engine.snapshots(repo)
    with engine.project_lock(repo/'logs/project-doc-audit/runs/.history-publication.lock', 'another-run'):
        assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
        assert engine.snapshots(repo) == before
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'completed'


def test_audit_rejects_modified_human_receipt(repo):
    state = confirm(repo, preview(repo))
    workflow.execute(repo, state['run_id'], 'publish')
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    assert history_findings(repo, 'staged')[0] == []
    log, _ = workflow.directories(repo, state['run_id'])
    (log/'confirmation.json').write_text('{}', encoding='utf-8')
    assert {f['kind'] for f in history_findings(repo, 'staged')[0]} == {'changelog_review_missing'}


def test_real_sensitive_verify_adapter_is_silent_and_read_only(repo, capsys):
    import shutil
    path = repo/'skills/sensitive-commit-check/scripts/sensitive_commit_check.py'
    path.parent.mkdir(parents=True)
    shutil.copyfile(ROOT/'skills/sensitive-commit-check/scripts/sensitive_commit_check.py', path)
    put(repo, 'logs/sensitive-commit-check/runs/synthetic/state.json', json.dumps({
        'status': 'approved', 'verification': {'review_paths_valid': True}}))
    before = (repo/'logs/sensitive-commit-check/runs/synthetic/state.json').read_bytes()
    assert recent.check_verified(repo, 'sensitive-commit-check', 'synthetic')
    assert capsys.readouterr().out == ''
    assert (repo/'logs/sensitive-commit-check/runs/synthetic/state.json').read_bytes() == before


@pytest.mark.parametrize('confirmed', [False, True])
def test_rejected_review_input_does_not_regenerate_or_publish(repo, confirmed):
    state = preview(repo)
    if confirmed:
        state = confirm(repo, state)
    log, out = workflow.directories(repo, state['run_id'])
    before = engine.snapshots(repo)
    original = (out/'proposal.md').read_bytes()
    assert workflow.execute(repo, state['run_id'], 'resume', review_path(repo, state))['status'] == 'paused_error'
    recovered = workflow.execute(repo, state['run_id'], 'resume')
    assert recovered['status'] == ('confirmed' if confirmed else 'awaiting_user_confirmation')
    assert (out/'proposal.md').read_bytes() == original
    assert engine.snapshots(repo) == before


def test_rejected_confirmation_resume_keeps_preview(repo):
    state = preview(repo)
    log, out = workflow.directories(repo, state['run_id'])
    original = (out/'proposal.md').read_bytes()
    path = confirmation(repo, state)
    data = json.loads(path.read_text(encoding='utf-8')); data['confirmed'] = False
    path.write_text(json.dumps(data), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'confirm', path)['status'] == 'paused_error'
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'awaiting_user_confirmation'
    assert (out/'proposal.md').read_bytes() == original
    assert not (log/'confirmation.json').exists()


def test_confirm_process_interruption_without_record_never_confirms(repo):
    state = preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    engine.move(log, state, 'validating_confirmation')
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'awaiting_user_confirmation'


def test_confirm_process_interruption_with_record_restores_confirmed(repo):
    state = confirm(repo, preview(repo))
    log, _ = workflow.directories(repo, state['run_id'])
    state['status'] = 'validating_confirmation'
    engine.save_state(log, state)
    before = engine.snapshots(repo)
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'confirmed'
    assert engine.snapshots(repo) == before


def test_diff_tampering_blocks_review_and_final_audit(repo):
    state = workflow.start(repo)
    log, out = workflow.directories(repo, state['run_id'])
    (out/'diff.md').write_text('wrong evidence', encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'resume', review_path(repo, state))['status'] == 'paused_error'
    state = confirm(repo, preview(repo))
    workflow.execute(repo, state['run_id'], 'publish')
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    _, out = workflow.directories(repo, state['run_id'])
    (out/'diff.md').write_text('wrong evidence', encoding='utf-8')
    with pytest.raises(ValueError, match='差异文件'):
        workflow.execute(repo, state['run_id'], 'verify')
    assert {f['kind'] for f in history_findings(repo, 'staged')[0]} == {'changelog_review_missing'}


def test_deliver_only_shows_fresh_reminders(repo, monkeypatch):
    state = preview(repo)
    monkeypatch.setattr(workflow, 'collect_recent_runs', lambda root: {'checked_at':'2026-10-04T16:00:00', 'skills':{}})
    text = workflow.execute(repo, state['run_id'], 'deliver')['markdown']
    assert text.count('# 提交前检查提醒') == 1
    assert '2026-10-04T16:00:00' in text
    assert '最近 1h 未启动' not in text


def test_malformed_run_state_returns_structured_error(repo):
    state = workflow.start(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    (log/'state.json').write_text('[]', encoding='utf-8')
    result = subprocess.run([sys.executable, str(CLI), 'status', '--root', str(repo), '--run-id', state['run_id']], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 2
    assert json.loads(result.stdout)['ok'] is False
    assert 'Traceback' not in result.stderr


def test_unavailable_check_module_keeps_run_and_reports_unverified(repo):
    put(repo, 'skills/project-doc-audit/scripts/cli.py', 'import missing_synthetic_dependency\n')
    put(repo, 'logs/project-doc-audit/runs/x/state.json', json.dumps({'created_at':'2026-10-04T15:00:00', 'status':'completed'}))
    checks = recent.collect_recent_runs(repo, '2026-10-04T15:00:00')
    assert len(checks['skills']['project-doc-audit']['runs']) == 1
    assert not checks['skills']['project-doc-audit']['runs'][0]['verified']
    assert '未通过核验' in recent.render_reminders(checks)


def test_target_edit_invalidates_verify_and_confirm(repo):
    state = preview(repo)
    put(repo, 'README.md', 'new user edit')
    with pytest.raises(ValueError, match='预览后'):
        workflow.execute(repo, state['run_id'], 'verify')
    assert workflow.execute(repo, state['run_id'], 'confirm', confirmation(repo, state))['status'] == 'paused_error'
