"""Release workflow regressions using disposable repositories, never the real index."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'utils/scripts'))
from utils.scripts.commit_history import collect_packet, first_parent_commits, version_at
from utils.scripts.version_history import history_findings, parse_changelog, render_changelog, generated_targets
from utils.scripts.structured_io import write_text_transaction

from utils.scripts import version_workflow as workflow
CLI = Path(__file__).with_name('cli.py')


def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    return subprocess.check_output(['git', '-c', 'user.name=TEST', '-c', 'user.email=test@example.invalid',
                                    '-c', 'commit.gpgsign=false', *args], cwd=root, env=env, text=True, encoding='utf-8').strip()


def put(root, path, text):
    target = root/path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8', newline='\n')


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-b', 'main')
    put(tmp_path, '.gitignore', 'logs/\noutputs/\n')
    put(tmp_path, 'VERSION', '0.1.0\n')
    put(tmp_path, 'README.md', '# Demo\n\n**Version：** [0.1.0](VERSION)\n')
    put(tmp_path, '.claude-plugin/marketplace.json', json.dumps({'plugins': [{'name': 'htd-ai-augmented-education', 'version': '0.1.0'}]}))
    put(tmp_path, 'docs/更新历史.md', '# 更新历史\n\n## 0.1.0\n\n- 建立项目\n')
    put(tmp_path, 'SOURCE_OF_TRUTH.md', '版本与提交更新历史 docs/更新历史.md VERSION 第一父链')
    put(tmp_path, 'AGENTS.md', '版本与提交维护 SOURCE_OF_TRUTH.md history-start scope staged')
    put(tmp_path, 'CODE_OF_CONDUCT.md', 'SOURCE_OF_TRUTH.md 更新历史 未提交 同次提交')
    put(tmp_path, 'feature.txt', 'first\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-m', 'initial')
    return tmp_path


def reviewed(root, state, summary='新增功能'):
    log, _ = workflow.directories(root, state['run_id'])
    review = json.loads((log/'review-template.json').read_text(encoding='utf-8'))
    for c in review['commits']:
        ids = [e['evidence_id'] for e in c['skipped']]
        c['updates'] = [{'summary': summary, 'evidence_ids': ids}] if ids else []
        c['skipped'] = []
        c['no_core_reason'] = '只有版本同步，无核心内容' if not ids else ''
    path = log/'input.json'
    path.write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
    return path


def prepare_preview(root, mode='history'):
    state = workflow.start(root, mode)
    assert state['status'] == 'awaiting_agent_review'
    state = workflow.execute(root, state['run_id'], 'resume', reviewed(root, state))
    assert state['status'] == 'preview_ready', state['error']
    return state


def test_first_commit_and_audit(repo):
    packet = collect_packet(repo, 'history')
    assert [c['version'] for c in packet['commits']] == ['0.1.0']
    assert packet['commits'][0]['parent'] is None
    assert 'feature.txt' in {e['path'] for e in packet['commits'][0]['evidence']}
    assert history_findings(repo)[0] == []
    assert version_at(10) == '0.1.10'


def test_merge_counts_first_parent_only(repo):
    git(repo, 'checkout', '-b', 'topic')
    put(repo, 'topic.txt', 'topic')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'topic')
    topic = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', 'main')
    git(repo, 'merge', '--no-ff', '-m', 'merge topic', 'topic')
    commits = first_parent_commits(repo)
    assert len(commits) == 2 and topic not in commits
    packet = collect_packet(repo, 'history')
    assert packet['commits'][-1]['version'] == '0.1.1'
    assert {e['path'] for e in packet['commits'][-1]['evidence']} == {'topic.txt'}


def test_shallow_repository_pauses(tmp_path, repo):
    clone = tmp_path/'clone'
    subprocess.check_call(['git', '-c', 'core.longpaths=true', 'clone', '--config', 'core.longpaths=true',
                           '--depth', '1', repo.as_uri(), str(clone)], stdout=subprocess.DEVNULL)
    with pytest.raises(ValueError, match='浅克隆'):
        first_parent_commits(clone)
    state = workflow.start(clone, 'history')
    assert state['status'] == 'paused_error' and state['resume_stage'] == 'collecting_commits'


def test_history_publication_idempotence_and_verification(repo):
    state = prepare_preview(repo)
    log, out = workflow.directories(repo, state['run_id'])
    assert workflow.verify(repo, log, out, state)
    published = workflow.execute(repo, state['run_id'], 'publish')
    assert published['status'] == 'completed'
    before = workflow.snapshots(repo)
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    assert workflow.snapshots(repo) == before
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']
    assert workflow.execute(repo, state['run_id'], 'deliver')['preview'] == str(out/'preview.md')
    (out/'preview.md').write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='预览'):
        workflow.execute(repo, state['run_id'], 'verify')


def test_waiting_resume_never_bypasses_review(repo):
    state = workflow.start(repo, 'history')
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'awaiting_agent_review'
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'


def test_review_evidence_gate_and_recovery(repo):
    state = workflow.start(repo, 'history')
    path = reviewed(repo, state)
    review = json.loads(path.read_text(encoding='utf-8'))
    saved = review['commits'][0]['updates'][0]['evidence_ids']
    review['commits'][0]['updates'][0]['evidence_ids'] = ['E9999']
    path.write_text(json.dumps(review), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'resume', path)['status'] == 'paused_error'
    review['commits'][0]['updates'][0]['evidence_ids'] = saved
    path.write_text(json.dumps(review), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'resume', path)['status'] == 'preview_ready'


def test_staged_ignores_unstaged_and_release_metadata(repo):
    put(repo, 'feature.txt', 'staged feature\n')
    git(repo, 'add', 'feature.txt')
    packet = collect_packet(repo, 'staged')
    put(repo, 'feature.txt', 'uncommitted other feature\n')
    assert collect_packet(repo, 'staged') == packet
    changes = generated_targets(repo, '0.1.1', '# 更新历史\n\n## 0.1.1\n\n- 更新功能\n\n## 0.1.0\n\n- 建立项目\n')
    write_text_transaction({repo/p: t for p, t in changes.items()})
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    assert collect_packet(repo, 'staged') == packet
    assert {f['kind'] for f in history_findings(repo, 'staged')[0]} == {'changelog_review_missing'}
    assert history_findings(repo)[0]  # Worktree is intentionally preparing next commit.


def test_staged_changed_input_invalidates_review(repo):
    put(repo, 'feature.txt', 'staged\n')
    git(repo, 'add', 'feature.txt')
    state = workflow.start(repo, 'staged')
    path = reviewed(repo, state)
    put(repo, 'feature.txt', 'changed stage\n')
    git(repo, 'add', 'feature.txt')
    result = workflow.execute(repo, state['run_id'], 'resume', path)
    assert result['status'] == 'paused_error' and '差异已变化' in result['error']


def test_staged_publication_preserves_history(repo):
    put(repo, 'feature.txt', 'new\n')
    git(repo, 'add', 'feature.txt')
    state = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    records = parse_changelog((repo/'docs/更新历史.md').read_text(encoding='utf-8'))
    assert records[0]['version'] == '0.1.1' and records[1]['updates'] == ['建立项目']
    git(repo, 'add', '.')
    # Staging generated docs does not invalidate evidence, and count remains one increment.
    assert workflow.execute(repo, state['run_id'], 'verify')['ok']


def test_no_core_commit_keeps_version(repo):
    state = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    assert parse_changelog((repo/'docs/更新历史.md').read_text(encoding='utf-8'))[0]['updates'] == ['本次无核心更新']


def test_head_change_invalidates_packet(repo):
    state = prepare_preview(repo)
    put(repo, 'feature.txt', 'new commit\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'next')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'


def test_post_preview_edit_is_never_overwritten(repo):
    state = prepare_preview(repo)
    put(repo, 'README.md', '# user edit')
    result = workflow.execute(repo, state['run_id'], 'publish')
    assert result['status'] == 'paused_error'
    assert (repo/'README.md').read_text(encoding='utf-8') == '# user edit'


def test_publication_failure_then_resume(repo, monkeypatch):
    state = prepare_preview(repo)
    log, out = workflow.directories(repo, state['run_id'])
    real = workflow.write_text_transaction
    def fail_once(updates):
        if repo/'VERSION' in updates:
            raise OSError('injected publishing failure')
        return real(updates)
    monkeypatch.setattr(workflow, 'write_text_transaction', fail_once)
    result = workflow.execute(repo, state['run_id'], 'publish')
    assert result['status'] == 'paused_error' and result['resume_stage'] == 'publishing'
    monkeypatch.setattr(workflow, 'write_text_transaction', real)
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'completed'
    assert workflow.verify(repo, log, out, json.loads((log/'state.json').read_text(encoding='utf-8')), published=True)


def test_resume_after_partial_process_interruption(repo):
    state = prepare_preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    workflow.move(log, state, 'publishing')
    journal = json.loads((log/'publication.json').read_text(encoding='utf-8'))
    put(repo, 'docs/更新历史.md', journal['updates']['docs/更新历史.md'])
    # A killed process left a mixture of baseline and desired files.
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'


def test_resume_after_write_before_checkpoint(repo):
    state = prepare_preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    workflow.move(log, state, 'publishing')
    journal = json.loads((log/'publication.json').read_text(encoding='utf-8'))
    write_text_transaction({repo/p: text for p, text in journal['updates'].items()})
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'


def test_legacy_exception_is_exact_prefix(repo, monkeypatch):
    put(repo, 'feature.txt', 'next')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'bad historical version')
    assert any(f['kind'] == 'commit_version_mismatch' for f in history_findings(repo)[0])
    import utils.scripts.version_history as module
    monkeypatch.setattr(module, 'LEGACY_THROUGH', git(repo, 'rev-parse', 'HEAD'))
    assert not any(f['kind'] == 'commit_version_mismatch' for f in history_findings(repo)[0])
    put(repo, 'feature.txt', 'later')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'bad post migration')
    assert any(f['kind'] == 'commit_version_mismatch' for f in history_findings(repo)[0])


@pytest.mark.parametrize('text', ['# 更新历史\n\n## 当前工作区变更\n- abc\n',
    '# 更新历史\n\n## 2026-10-04T12:00:00\n- abc\n',
    '# 更新历史\n\n## 0.1.0\n- a\n\n## 0.1.0\n- b\n',
    '# 更新历史\n\n## 0.1.0\n- TODO: later\n'])
def test_invalid_history_is_rejected(text):
    with pytest.raises(ValueError):
        parse_changelog(text)


def test_cli_interfaces(repo):
    def call(*args):
        return subprocess.run([sys.executable, str(CLI), *args, '--root', str(repo)], capture_output=True, text=True, encoding='utf-8')
    result = call('history-start', '--mode', 'history')
    assert result.returncode == 3, result.stderr
    state = json.loads(result.stdout)
    run = state['run_id']
    path = reviewed(repo, state)
    assert call('history-status', '--run-id', run).returncode == 0
    assert call('history-resume', '--run-id', run, '--input', str(path)).returncode == 0
    assert call('history-verify', '--run-id', run).returncode == 0
    assert call('history-deliver', '--run-id', run).stdout.startswith('# 更新历史')
    assert call('history-publish', '--run-id', run).returncode == 0
    assert call('history-resume', '--run-id', run).returncode == 0
    assert call('history-verify', '--run-id', 'missing').returncode == 4


def test_staged_readme_content_is_evidence(repo):
    put(repo, 'README.md', (repo/'README.md').read_text(encoding='utf-8') + '\nNew capability\n')
    git(repo, 'add', 'README.md')
    packet = collect_packet(repo, 'staged')
    assert packet['commits'][0]['evidence'][0]['path'] == 'README.md'
    assert 'New capability' in packet['commits'][0]['evidence'][0]['patch']


def test_repeated_same_second_runs_have_unique_ids(repo):
    first = workflow.start(repo, 'history')
    second = workflow.start(repo, 'history')
    assert first['run_id'] != second['run_id']


def test_resume_interrupted_preview_stage(repo):
    state = prepare_preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    state['status'] = 'rendering_preview'
    workflow.save_state(log, state)
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'preview_ready'


def test_publication_journal_tamper_is_rejected(repo):
    state = prepare_preview(repo)
    log, _ = workflow.directories(repo, state['run_id'])
    journal = json.loads((log/'publication.json').read_text(encoding='utf-8'))
    journal['updates']['VERSION'] = '0.1.99\n'
    (log/'publication.json').write_text(json.dumps(journal), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
    assert (repo/'VERSION').read_text(encoding='utf-8').strip() == '0.1.0'


def test_new_commit_includes_changelog_and_versions(repo):
    put(repo, 'feature.txt', 'new\n')
    git(repo, 'add', 'feature.txt')
    state = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    assert history_findings(repo, 'staged')[0] == []
    git(repo, 'commit', '-m', 'release')
    assert history_findings(repo)[0] == []


def test_unstaged_rules_cannot_mask_invalid_staged_contract(repo):
    put(repo, 'CODE_OF_CONDUCT.md', '# missing release rules\n')
    git(repo, 'add', 'CODE_OF_CONDUCT.md')
    put(repo, 'CODE_OF_CONDUCT.md', 'SOURCE_OF_TRUTH.md 更新历史 未提交 同次提交')
    assert any(f['path'] == 'CODE_OF_CONDUCT.md' and f['kind'] == 'changelog_scope_violation'
               for f in history_findings(repo, 'staged')[0])


@pytest.mark.parametrize('path', ['README.md', '.claude-plugin/marketplace.json'])
def test_unstaged_release_document_content_never_enters_preview(repo, path):
    put(repo, 'feature.txt', 'staged feature\n')
    git(repo, 'add', 'feature.txt')
    if path == 'README.md':
        put(repo, path, (repo/path).read_text(encoding='utf-8') + '\nUnstaged capability\n')
    else:
        value = json.loads((repo/path).read_text(encoding='utf-8'))
        value['description'] = 'unstaged marketplace change'
        put(repo, path, json.dumps(value))
    before = (repo/path).read_bytes()
    state = workflow.start(repo, 'staged')
    result = workflow.execute(repo, state['run_id'], 'resume', reviewed(repo, state))
    assert result['status'] == 'paused_error' and '未暂存' in result['error']
    assert (repo/path).read_bytes() == before


def test_marketplace_nonversion_change_is_evidence(repo):
    value = json.loads((repo/'.claude-plugin/marketplace.json').read_text(encoding='utf-8'))
    value['plugins'][0]['strict'] = True
    put(repo, '.claude-plugin/marketplace.json', json.dumps(value))
    git(repo, 'add', '.claude-plugin/marketplace.json')
    record = collect_packet(repo, 'staged')['commits'][0]['evidence'][0]
    assert record['path'] == '.claude-plugin/marketplace.json' and 'strict' in record['patch']


def test_reprepare_same_staged_commit_does_not_duplicate_version(repo):
    put(repo, 'feature.txt', 'new feature\n')
    git(repo, 'add', 'feature.txt')
    first = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, first['run_id'], 'publish')['status'] == 'completed'
    before = workflow.snapshots(repo)
    second = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, second['run_id'], 'publish')['status'] == 'completed'
    assert workflow.snapshots(repo) == before
    assert [r['version'] for r in parse_changelog((repo/'docs/更新历史.md').read_text(encoding='utf-8'))] == ['0.1.1', '0.1.0']


def test_staged_audit_rejects_stale_summary_even_with_valid_versions(repo):
    put(repo, 'feature.txt', 'reviewed feature\n')
    git(repo, 'add', 'feature.txt')
    state = prepare_preview(repo, 'staged')
    assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'completed'
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    assert history_findings(repo, 'staged')[0] == []
    put(repo, 'feature.txt', 'another feature after summary\n')
    git(repo, 'add', 'feature.txt')
    assert {f['kind'] for f in history_findings(repo, 'staged')[0]} == {'changelog_review_missing'}


def test_cross_run_publication_lock_blocks_writes_and_recovers(repo):
    state = prepare_preview(repo)
    before = workflow.snapshots(repo)
    with workflow.project_lock(repo/'logs/project-doc-audit/runs/.history-publication.lock', 'another-run'):
        assert workflow.execute(repo, state['run_id'], 'publish')['status'] == 'paused_error'
        assert workflow.snapshots(repo) == before
    assert workflow.execute(repo, state['run_id'], 'resume')['status'] == 'completed'


def test_successful_resume_clears_old_error_and_reports_recovery_fields(repo):
    state = workflow.start(repo, 'history')
    path = reviewed(repo, state)
    review = json.loads(path.read_text(encoding='utf-8'))
    original = review['packet_sha256']
    review['packet_sha256'] = '0'*64
    path.write_text(json.dumps(review), encoding='utf-8')
    assert workflow.execute(repo, state['run_id'], 'resume', path)['status'] == 'paused_error'
    failed = workflow.execute(repo, state['run_id'], 'status')
    assert failed['resume_stage'] == 'validating_review'
    review['packet_sha256'] = original
    path.write_text(json.dumps(review), encoding='utf-8')
    resumed = workflow.execute(repo, state['run_id'], 'resume', path)
    assert resumed['status'] == 'preview_ready' and resumed['error'] is None and resumed['resume_stage'] is None


def test_staged_audit_does_not_mix_worktree_version_values(repo):
    put(repo, 'feature.txt', 'new\n')
    git(repo, 'add', 'feature.txt')
    state = prepare_preview(repo, 'staged')
    workflow.execute(repo, state['run_id'], 'publish')
    git(repo, 'add', 'VERSION', 'README.md', '.claude-plugin/marketplace.json', 'docs/更新历史.md')
    put(repo, 'VERSION', '0.1.99\n')
    from utils.scripts.document_audit import audit
    report, _ = audit(repo, scope='staged')
    assert not any(f['kind'] in {'version_mismatch', 'missing_version', 'changelog_review_missing'} for f in report['findings'])


def test_worktree_history_review_detects_changed_old_summary(repo):
    state = prepare_preview(repo)
    workflow.execute(repo, state['run_id'], 'publish')
    assert history_findings(repo)[0] == []
    put(repo, 'docs/更新历史.md', '# 更新历史\n\n## 0.1.0\n\n- 尚未提交的功能\n')
    assert {f['kind'] for f in history_findings(repo)[0]} == {'changelog_review_missing'}


def test_completed_run_recheck_failure_does_not_mask_error(repo):
    state = prepare_preview(repo)
    workflow.execute(repo, state['run_id'], 'publish')
    put(repo, 'VERSION', '0.1.99\n')
    with pytest.raises(workflow.HistoryVerificationError, match='发布后的版本'):
        workflow.execute(repo, state['run_id'], 'publish')
    assert workflow.execute(repo, state['run_id'], 'status')['status'] == 'completed'
    result = subprocess.run([sys.executable, str(CLI), 'history-publish', '--root', str(repo),
                             '--run-id', state['run_id']], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 4 and '非法历史状态' not in result.stdout


def test_readme_mode_change_is_not_discarded_as_version_metadata(repo):
    git(repo, 'update-index', '--chmod=+x', 'README.md')
    records = collect_packet(repo, 'staged')['commits'][0]['evidence']
    assert len(records) == 1 and records[0]['path'] == 'README.md'
    assert 'new mode 100755' in records[0]['patch']


def test_invalid_marketplace_shape_pauses_instead_of_crashing(repo):
    put(repo, '.claude-plugin/marketplace.json', '[]\n')
    git(repo, 'add', '.claude-plugin/marketplace.json')
    state = workflow.start(repo, 'staged')
    assert state['status'] == 'paused_error' and 'marketplace' in state['error']


def test_malformed_local_review_state_does_not_crash_audit(repo):
    put(repo, 'logs/project-doc-audit/runs/broken/history/state.json', '[]')
    findings, _ = history_findings(repo, 'staged')
    assert any(f['kind'] == 'changelog_review_missing' for f in findings)


def test_marketplace_project_is_selected_by_name_not_position(repo):
    value = json.loads((repo/'.claude-plugin/marketplace.json').read_text(encoding='utf-8'))
    value['plugins'].insert(0, {'name': 'other-project', 'version': '9.9.9'})
    put(repo, '.claude-plugin/marketplace.json', json.dumps(value))
    from utils.scripts.document_audit import audit
    report, _ = audit(repo)
    assert not any(f['kind'] == 'version_mismatch' for f in report['findings'])
