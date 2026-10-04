"""Recoverable historical-summary and staged-release preparation workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts.commit_history import collect_packet, assert_candidate, first_parent_commits, version_at, git_text, normalized_version_source
from utils.scripts.version_history import HISTORY_PATH, TARGETS, generated_targets, parse_changelog, render_changelog, prior_history, validate_history_review, review_binding
from utils.scripts.structured_io import read_json, json_digest, validate_json_schema, write_text_transaction
from utils.scripts.file_transaction import project_lock
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp

SCHEMA = ROOT/'skills/project-doc-audit/references/history-review.schema.json'


class HistoryVerificationError(ValueError):
    """Completed runs remain terminal; a failed recheck never reports success."""


TRANSITIONS = {
    'prepared': {'collecting_commits', 'paused_error'},
    'collecting_commits': {'preparing_review', 'paused_error'},
    'preparing_review': {'awaiting_agent_review', 'paused_error'},
    'awaiting_agent_review': {'validating_review', 'paused_error'},
    'validating_review': {'rendering_preview', 'paused_error'},
    'rendering_preview': {'preview_ready', 'paused_error'},
    'preview_ready': {'publishing', 'awaiting_user_confirmation', 'paused_error'},
    'awaiting_user_confirmation': {'validating_confirmation', 'paused_error'},
    'validating_confirmation': {'confirmed', 'awaiting_user_confirmation', 'paused_error'},
    'confirmed': {'publishing', 'paused_error'},
    'publishing': {'verifying', 'paused_error'},
    'verifying': {'completed', 'checking_recent_runs', 'paused_error'},
    'checking_recent_runs': {'completed', 'paused_error'},
    'paused_error': {'collecting_commits', 'validating_review', 'validating_confirmation', 'publishing', 'verifying', 'checking_recent_runs', 'awaiting_user_confirmation', 'confirmed', 'preview_ready'},
    'completed': set(),
}


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def diff_text(packet):
    """Render frozen evidence deterministically, including embedded backticks."""
    import re
    evidence = packet['commits'][0]['evidence']
    sections = []
    for item in evidence:
        fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', item['patch'])), default=0))
        source = ('来源：' + item['source'] + '\n\n') if 'source' in item else ''
        sections.append(f"## {item['evidence_id']} · {item['path']}\n\n{source}{fence}diff\n{item['patch']}\n{fence}\n")
    empty = '没有可总结的候选差异。\n' if packet.get('scope') else '没有可总结的暂存差异。\n'
    return '# 待提交版本差异\n\n' + ('\n'.join(sections) if sections else empty)


def directories(root, run, workflow='project-doc-audit'):
    if not run or Path(run).name != run or '/' in run or '\\' in run or run in {'.', '..'}:
        raise ValueError('run ID 无效')
    if workflow == 'update-project-version':
        return root/'logs'/workflow/'runs'/run, root/'outputs'/workflow/'runs'/run
    if workflow != 'project-doc-audit':
        raise ValueError('未知版本流程')
    return root/'logs/project-doc-audit/runs'/run/'history', root/'outputs/project-doc-audit/runs'/run/'history'


def save_state(log, state):
    state['updated_at'] = iso_timestamp()
    write_text_transaction({log/'state.json': dumps(state)})


def move(log, state, target):
    if target not in TRANSITIONS[state['status']]:
        raise ValueError(f"非法历史状态迁移：{state['status']} → {target}")
    state['status'] = target
    state['completed_steps'].append(target)
    save_state(log, state)


def receipt(state):
    return {k: state.get(k) for k in ('run_id', 'mode', 'status', 'error', 'resume_stage', 'packet_sha256', 'review_template', 'packet')}


def snapshots(root):
    return {p: digest_bytes((root/p).read_bytes()) if (root/p).is_file() else None for p in TARGETS}


def assert_packet(root, log, state):
    packet = read_json(log/'packet.json')
    if packet['packet_sha256'] != state['packet_sha256']:
        raise ValueError('证据与运行指纹不一致')
    if packet.get('scope') == 'staged_and_untracked':
        assert_candidate(root, packet)
    elif collect_packet(root, state['mode']) != packet:
        raise ValueError('提交历史或暂存区差异已变化；请新建运行重新审查')
    return packet


def validate_review(packet, review):
    validate_history_review(packet, review, SCHEMA)


def prepare(root, log, state):
    move(log, state, 'collecting_commits')
    packet = collect_packet(root, state['mode'], include_untracked=state.get('include_untracked', False))
    state['packet_sha256'] = packet['packet_sha256']
    state['baseline'] = snapshots(root)
    write_text_transaction({log/'packet.json': dumps(packet)})
    move(log, state, 'preparing_review')
    template = {'schema_version': '1.0', 'packet_sha256': packet['packet_sha256'],
                'commits': [{'version': c['version'], 'updates': [], 'no_core_reason': '',
                             'skipped': [{'evidence_id': e['evidence_id'], 'reason': ''} for e in c['evidence']]}
                            for c in packet['commits']]}
    write_text_transaction({log/'review-template.json': dumps(template)})
    state['error'], state['resume_stage'] = None, None
    move(log, state, 'awaiting_agent_review')


def start(root, mode, workflow='project-doc-audit'):
    base = root/'logs'/workflow/'runs'
    with project_lock(base/'.history-start.lock', 'history-start'):
        run = unique_filename_timestamp(p.name for p in base.iterdir())
        log, _ = directories(root, run, workflow)
        state = {'schema_version': '1.0', 'run_id': run, 'mode': mode, 'status': 'prepared',
                 'created_at': iso_timestamp(), 'updated_at': iso_timestamp(), 'completed_steps': ['prepared'],
                 'packet_sha256': None, 'baseline': {}, 'resume_stage': None, 'error': None}
        if workflow != 'project-doc-audit':
            state['workflow'] = workflow
            state['include_untracked'] = True
        state['review_template'] = str(log/'review-template.json')
        state['packet'] = str(log/'packet.json')
        save_state(log, state)
        try:
            prepare(root, log, state)
        except (OSError, ValueError, RuntimeError) as exc:
            pause(log, state, exc, 'collecting_commits')
    return state


def pause(log, state, exc, resume_stage):
    state['resume_stage'] = resume_stage
    state['error'] = str(exc)
    if state['status'] != 'paused_error':
        move(log, state, 'paused_error')
    else:
        save_state(log, state)


def review_and_preview(root, log, out, state, input_path):
    packet = assert_packet(root, log, state)
    if snapshots(root) != state['baseline']:
        raise ValueError('预览前版本或文档已变化；请新建运行')
    review = read_json(input_path) if input_path else read_json(log/'review.json')
    validate_review(packet, review)
    write_text_transaction({log/'review.json': dumps(review)})
    move(log, state, 'validating_review')
    summaries = [{'version': c['version'], 'updates': [u['summary'] for u in c['updates']]} for c in review['commits']]
    if state['mode'] == 'staged':
        existing = parse_changelog((root/HISTORY_PATH).read_text(encoding='utf-8'))
        base = prior_history(root, first_parent_commits(root))
        candidate = existing and existing[0]['version'] == packet['commits'][0]['version'] and existing[1:] == base
        if existing != base and not candidate:
            raise ValueError('工作区既有历史被改写；须先逐条复核，不覆盖未审查历史')
        summaries += base
        for path in ('README.md', '.claude-plugin/marketplace.json'):
            staged = git_text(root, '', path)
            if staged is None or normalized_version_source(path, (root/path).read_text(encoding='utf-8')) != normalized_version_source(path, staged):
                raise ValueError(f'{path} 存在未暂存内容；先确定提交范围，不混入预览或覆盖工作区')
    move(log, state, 'rendering_preview')
    updates = generated_targets(root, packet['commits'][-1]['version'], render_changelog(summaries),
                                read=(lambda path: git_text(root, '', path)) if state['mode'] == 'staged' else None)
    binding = review_binding(packet, review)
    # Persist recovery data BEFORE preview or publication; public docs contain no hashes.
    journal = {'updates': updates, 'baseline': state['baseline'],
               'desired': {p: digest_bytes(t.encode('utf-8')) for p, t in updates.items()}, 'binding': binding}
    state['publication_sha256'] = json_digest(journal)
    write_text_transaction({log/'publication.json': dumps(journal)})
    write_text_transaction({out/'preview.md': updates[HISTORY_PATH], out/'bindings.json': dumps(binding)})
    state['error'], state['resume_stage'] = None, None
    move(log, state, 'preview_ready')


def verify(root, log, out, state, published=False):
    packet = assert_packet(root, log, state)
    review = read_json(log/'review.json')
    validate_review(packet, review)
    journal = read_json(log/'publication.json')
    if json_digest(journal) != state.get('publication_sha256'):
        raise ValueError('发布记录指纹与运行不一致')
    if set(journal['updates']) != set(TARGETS) or journal['baseline'] != state['baseline']:
        raise ValueError('发布目标或原始指纹与运行不一致')
    expected_binding = review_binding(packet, review)
    if journal['binding'] != expected_binding:
        raise ValueError('核验记录与 Git 证据不一致')
    if journal['binding']['review_sha256'] != json_digest(review):
        raise ValueError('发布记录与审查输入不一致')
    summaries = [{'version': c['version'], 'updates': [u['summary'] for u in c['updates']]} for c in review['commits']]
    if state['mode'] == 'history':
        if journal['updates'][HISTORY_PATH] != render_changelog(summaries):
            raise ValueError('历史预览与审查不一致')
    else:
        records = parse_changelog(journal['updates'][HISTORY_PATH])
        if records[0] != {'version': summaries[0]['version'], 'updates': summaries[0]['updates'] or ['本次无核心更新']}:
            raise ValueError('待提交预览与审查不一致')
    if journal['desired'] != {p: digest_bytes(t.encode('utf-8')) for p, t in journal['updates'].items()}:
        raise ValueError('发布指纹损坏')
    from utils.scripts.version_history import version_values
    if any(v != packet['commits'][-1]['version'] for v in version_values(journal['updates'].get).values()):
        raise ValueError('预览版本值与提交序号不一致')
    if (out/'preview.md').read_text(encoding='utf-8') != journal['updates'][HISTORY_PATH] or read_json(out/'bindings.json') != journal['binding']:
        raise ValueError('预览或核验记录已变化')
    if published and snapshots(root) != journal['desired']:
        raise ValueError('发布后的版本或文档已变化')
    if published and state.get('workflow') == 'update-project-version' and state['status'] == 'completed':
        from utils.scripts.recent_skill_runs import render_reminders
        if (out/'reminders.md').read_text(encoding='utf-8') != render_reminders(state['recent_checks']):
            raise ValueError('提交前提醒与运行记录不一致')
    return True


def verify_confirmation(log, state):
    """Bind human confirmation to the exact immutable publication preview."""
    confirmation = read_json(log/'confirmation.json')
    validate_json_schema(confirmation, ROOT/'skills/update-project-version/references/confirmation.schema.json')
    if (confirmation['run_id'] != state['run_id']
            or confirmation['packet_sha256'] != state['packet_sha256']
            or confirmation['preview_sha256'] != state['publication_sha256']
            or json_digest(confirmation) != state.get('confirmation_sha256')):
        raise ValueError('用户确认与本次差异或预览不一致')
    from datetime import datetime
    stamp = datetime.fromisoformat(confirmation['confirmed_at'])
    if iso_timestamp(stamp) != confirmation['confirmed_at'] or stamp.tzinfo is not None:
        raise ValueError('确认时间格式无效')
    return True


def publish(root, log, out, state):
    # All runs share the same four publication targets, so a per-run lock alone
    # cannot protect the baseline check followed by the multi-file transaction.
    with project_lock(root/'logs/project-doc-audit/runs/.history-publication.lock', 'history-publish'):
        return publish_locked(root, log, out, state)


def publish_locked(root, log, out, state):
    if state.get('workflow') == 'update-project-version':
        verify_confirmation(log, state)
    if state['status'] == 'completed':
        verify(root, log, out, state, published=True)
        return
    if state['status'] not in {'preview_ready', 'confirmed', 'publishing', 'verifying', 'checking_recent_runs', 'paused_error'}:
        raise ValueError('只有已验证预览可以发布')
    verify(root, log, out, state)
    journal = read_json(log/'publication.json')
    current = snapshots(root)
    if any(current[p] not in {journal['baseline'][p], journal['desired'][p]} for p in TARGETS):
        raise ValueError('发布目标存在预览后新增修改，拒绝覆盖；请重新准备')
    if state['status'] not in {'verifying', 'checking_recent_runs'}:
        if state['status'] != 'publishing':
            move(log, state, 'publishing')
        write_text_transaction({root/p: t for p, t in journal['updates'].items()})
        move(log, state, 'verifying')
    verify(root, log, out, state, published=True)
    state['error'], state['resume_stage'] = None, None
    if state.get('workflow') == 'update-project-version':
        from utils.scripts.recent_skill_runs import collect_recent_runs, render_reminders
        if state['status'] != 'checking_recent_runs':
            move(log, state, 'checking_recent_runs')
        checks = collect_recent_runs(root)
        write_text_transaction({out/'reminders.md': render_reminders(checks)})
        state['recent_checks'] = checks
    move(log, state, 'completed')


def execute(root, run, command, input_path=None):
    log, out = directories(root, run)
    with project_lock(log/'.lock', command):
        state = read_json(log/'state.json')
        if not isinstance(state, dict):
            raise ValueError('运行状态必须为对象')
        state.setdefault('review_template', str(log/'review-template.json'))
        state.setdefault('packet', str(log/'packet.json'))
        if command == 'status':
            return state
        if command in {'verify', 'deliver'}:
            verify(root, log, out, state, published=state['status'] == 'completed')
            if state['status'] not in {'preview_ready', 'completed'}:
                raise ValueError('当前运行尚无可交付预览')
            return {'ok': True, 'status': state['status'], 'preview': str(out/'preview.md')}
        try:
            if command == 'publish' or (command == 'resume' and not input_path and state['status'] == 'paused_error' and state['resume_stage'] in {'publishing', 'verifying'} and (log/'publication.json').is_file()):
                publish(root, log, out, state)
            elif command == 'resume':
                if state['status'] in {'prepared', 'collecting_commits', 'preparing_review'}:
                    pause(log, state, ValueError('恢复中断的提交取证'), 'collecting_commits')
                elif state['status'] in {'validating_review', 'rendering_preview'}:
                    pause(log, state, ValueError('恢复中断的审查与预览'), 'validating_review')
                if state['status'] in {'preview_ready', 'completed'}:
                    if input_path:
                        raise ValueError('已有预览不可替换审查；请新建运行')
                    verify(root, log, out, state, published=state['status'] == 'completed')
                elif state['status'] == 'paused_error' and state['resume_stage'] == 'collecting_commits':
                    prepare(root, log, state)
                elif input_path or (log/'review.json').is_file():
                    review_and_preview(root, log, out, state, input_path)
            else:
                raise ValueError('未知命令')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if state['status'] == 'completed':
                raise HistoryVerificationError(str(exc)) from exc
            retry = 'publishing' if state['status'] in {'preview_ready', 'publishing', 'verifying'} or command == 'publish' else 'validating_review'
            pause(log, state, exc, retry)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description='提交历史与版本生成：默认只准备审查和预览，publish 明确写入')
    parser.add_argument('command', choices=('start', 'resume', 'status', 'publish', 'verify', 'deliver'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--mode', choices=('history', 'staged'), default='history')
    parser.add_argument('--run-id')
    parser.add_argument('--input')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == 'start':
            state = start(root, args.mode)
        else:
            if not args.run_id:
                parser.error('--run-id required')
            state = execute(root, args.run_id, args.command, Path(args.input).resolve() if args.input else None)
        if args.command == 'deliver':
            print(Path(state['preview']).read_text(encoding='utf-8'), end='')
        else:
            print(dumps(state if args.command == 'status' else (receipt(state) if 'run_id' in state else state)), end='')
        return 0 if args.command == 'status' else (3 if state.get('status') in {'paused_error', 'awaiting_agent_review'} else 0)
    except HistoryVerificationError as exc:
        print(dumps({'ok': False, 'error': str(exc)}), end='')
        return 4
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(dumps({'ok': False, 'error': str(exc)}), end='')
        return 4 if args.command in {'verify', 'deliver'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
