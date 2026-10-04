"""Human-confirmed staged version updates using the shared release engine."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts import version_workflow as engine
from utils.scripts.recent_skill_runs import collect_recent_runs, render_reminders
from utils.scripts.structured_io import read_json, json_digest, write_text_transaction, validate_json_schema
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.file_transaction import project_lock

WORKFLOW = 'update-project-version'


def directories(root, run):
    return engine.directories(root, run, WORKFLOW)


def start(root):
    state = engine.start(root, 'staged', WORKFLOW)
    log, out = directories(root, state['run_id'])
    if state['status'] == 'awaiting_agent_review':
        try:
            render_diff(log, out, state)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            engine.pause(log, state, exc, 'collecting_commits')
    return state


def render_diff(log, out, state):
    text = diff_text(read_json(log/'packet.json'))
    write_text_transaction({out/'diff.md': text})
    state['diff'] = str(out/'diff.md')
    engine.save_state(log, state)


def diff_text(packet):
    return engine.diff_text(packet)


def proposal(root, log, out, state):
    review = read_json(log/'review.json')['commits'][0]
    from utils.scripts.commit_history import first_parent_commits, version_at
    old = version_at(len(first_parent_commits(root)) - 1)
    lines = ['# 版本更新预览', '', f"当前已提交版本：`{old}`", f"新版本：`{review['version']}`", '', '核心更新：', '']
    lines += ['- ' + u['summary'] for u in review['updates']] or ['- 本次无核心更新']
    lines += ['', '用户确认此预览后，同步更新历史、VERSION、README 和 marketplace；提交由用户执行。', '',
              render_reminders(collect_recent_runs(root))]
    text = '\n'.join(lines) + '\n'
    template = {'run_id': state['run_id'], 'packet_sha256': state['packet_sha256'],
                'preview_sha256': state['publication_sha256'], 'confirmed': False, 'confirmed_at': ''}
    write_text_transaction({out/'proposal.md': text, log/'confirmation-template.json': engine.dumps(template)})
    state['proposal_sha256'] = engine.digest_bytes(text.encode('utf-8'))
    state['proposal'] = str(out/'proposal.md')
    state['confirmation_template'] = str(log/'confirmation-template.json')
    engine.move(log, state, 'awaiting_user_confirmation')


def verify(root, log, out, state):
    engine.verify(root, log, out, state, published=state['status'] == 'completed')
    verify_diff(log, out)
    publication_stage = state['status'] in {'publishing', 'verifying', 'checking_recent_runs', 'completed'} or (
        state['status'] == 'paused_error' and state.get('resume_stage') == 'publishing')
    if not publication_stage and engine.snapshots(root) != state['baseline']:
        raise ValueError('预览后版本或文档已变化；请新建运行重新确认')
    if engine.digest_bytes((out/'proposal.md').read_bytes()) != state.get('proposal_sha256'):
        raise ValueError('用户预览已变化；请新建运行重新确认')
    if (log/'confirmation.json').is_file():
        engine.verify_confirmation(log, state)


def verify_diff(log, out):
    if (out/'diff.md').read_text(encoding='utf-8') != diff_text(read_json(log/'packet.json')):
        raise ValueError('差异文件与冻结证据不一致')


def recover_confirmation(root, log, out, state):
    verify(root, log, out, state)
    target = 'confirmed' if (log/'confirmation.json').is_file() else 'awaiting_user_confirmation'
    state['error'], state['resume_stage'] = None, None
    engine.move(log, state, target)


def execute(root, run, command, input_path=None):
    log, out = directories(root, run)
    with project_lock(log/'.lock', command):
        state = read_json(log/'state.json')
        if not isinstance(state, dict):
            raise ValueError('运行状态必须为对象')
        if state.get('workflow') != WORKFLOW:
            raise ValueError('运行不属于 update-project-version')
        if command == 'status':
            return state
        if command in {'verify', 'deliver'}:
            verify(root, log, out, state)
            if state['status'] not in {'awaiting_user_confirmation', 'confirmed', 'completed'}:
                raise ValueError('尚无可交付预览')
            if command == 'deliver':
                frozen = (out/'proposal.md').read_text(encoding='utf-8')
                summary = frozen.partition('\n# 提交前检查提醒\n')[0]
                return {'markdown': summary + '\n' + render_reminders(collect_recent_runs(root)), 'ok': True}
            return {'ok': True, 'status': state['status']}
        previous_stage = state.get('resume_stage') if state['status'] == 'paused_error' else state['status']
        try:
            if command == 'confirm':
                if state['status'] not in {'awaiting_user_confirmation', 'validating_confirmation', 'paused_error'}:
                    raise ValueError('当前状态不接受确认')
                verify(root, log, out, state)
                if input_path is None:
                    raise ValueError('须提供用户确认记录 --input')
                confirmation = read_json(input_path)
                confirmation['confirmed_at'] = iso_timestamp()
                validate_json_schema(confirmation, ROOT/'skills/update-project-version/references/confirmation.schema.json')
                if any(confirmation[k] != v for k, v in {
                    'run_id': run, 'packet_sha256': state['packet_sha256'],
                    'preview_sha256': state['publication_sha256']}.items()):
                    raise ValueError('用户确认与差异或预览不一致')
                state['confirmation_sha256'] = json_digest(confirmation)
                if state['status'] != 'validating_confirmation':
                    engine.move(log, state, 'validating_confirmation')
                else:
                    engine.save_state(log, state)
                write_text_transaction({log/'confirmation.json': engine.dumps(confirmation)})
                engine.verify_confirmation(log, state)
                state['error'], state['resume_stage'] = None, None
                engine.move(log, state, 'confirmed')
            elif command == 'publish' or (command == 'resume' and state['status'] in {'publishing', 'verifying', 'checking_recent_runs'}):
                verify(root, log, out, state)
                if state['status'] not in {'confirmed', 'publishing', 'verifying', 'checking_recent_runs', 'completed', 'paused_error'}:
                    raise ValueError('请先展示预览并取得用户确认')
                engine.publish(root, log, out, state)
            elif command == 'resume':
                if state['status'] in {'awaiting_user_confirmation', 'confirmed', 'completed'}:
                    if input_path:
                        raise ValueError('已有预览不可替换；请新建运行')
                    verify(root, log, out, state)
                elif state['status'] == 'preview_ready':
                    proposal(root, log, out, state)
                elif state['status'] == 'validating_confirmation' or (
                        state['status'] == 'paused_error' and state['resume_stage'] in {'awaiting_user_confirmation', 'confirmed', 'validating_confirmation'}):
                    if input_path:
                        raise ValueError('已有预览不可替换；请使用 confirm 或新建运行')
                    recover_confirmation(root, log, out, state)
                elif state['status'] == 'paused_error' and state['resume_stage'] == 'preview_ready':
                    if input_path:
                        raise ValueError('已有预览不可替换；请新建运行')
                    engine.verify(root, log, out, state)
                    verify_diff(log, out)
                    engine.move(log, state, 'preview_ready')
                    proposal(root, log, out, state)
                elif state['status'] == 'paused_error' and state['resume_stage'] == 'publishing':
                    verify(root, log, out, state)
                    engine.publish(root, log, out, state)
                elif state['status'] in {'prepared', 'collecting_commits', 'preparing_review'} or (state['status'] == 'paused_error' and state['resume_stage'] == 'collecting_commits'):
                    engine.pause(log, state, ValueError('恢复取证'), 'collecting_commits')
                    engine.prepare(root, log, state)
                    render_diff(log, out, state)
                elif input_path or (log/'review.json').is_file():
                    verify_diff(log, out)
                    if state['status'] in {'validating_review', 'rendering_preview'}:
                        engine.pause(log, state, ValueError('恢复预览'), 'validating_review')
                    engine.review_and_preview(root, log, out, state, input_path)
                    proposal(root, log, out, state)
            else:
                raise ValueError('未知命令')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if state['status'] == 'completed':
                raise engine.HistoryVerificationError(str(exc)) from exc
            if state['status'] in {'publishing', 'verifying', 'checking_recent_runs'} or (
                    command == 'publish' and previous_stage in {'confirmed', 'publishing'}):
                stage = 'publishing'
            elif state['status'] == 'paused_error':
                stage = previous_stage or 'validating_review'
            elif state['status'] in {'awaiting_user_confirmation', 'confirmed', 'validating_confirmation', 'preview_ready'}:
                stage = state['status']
            elif state['status'] in {'prepared', 'collecting_commits', 'preparing_review', 'awaiting_agent_review'} and not (log/'review.json').is_file():
                stage = 'collecting_commits' if command == 'resume' and not input_path else 'validating_review'
            else:
                stage = 'validating_review'
            engine.pause(log, state, exc, stage)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description='列出暂存差异及未忽略的新文件，确认后同步版本；Git 提交由用户执行')
    parser.add_argument('command', choices=('start', 'resume', 'status', 'confirm', 'publish', 'verify', 'deliver'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--run-id')
    parser.add_argument('--input')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == 'start':
            state = start(root)
        else:
            if not args.run_id:
                parser.error('--run-id required')
            state = execute(root, args.run_id, args.command, Path(args.input).resolve() if args.input else None)
        if args.command == 'deliver':
            print(state['markdown'], end='')
        else:
            print(engine.dumps(state), end='')
        return 3 if state.get('status') in {'paused_error', 'awaiting_agent_review', 'awaiting_user_confirmation'} and args.command not in {'status', 'verify'} else 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(engine.dumps({'ok': False, 'error': str(exc)}), end='')
        return 4 if args.command in {'verify', 'deliver'} or isinstance(exc, engine.HistoryVerificationError) else 2


if __name__ == '__main__':
    raise SystemExit(main())
