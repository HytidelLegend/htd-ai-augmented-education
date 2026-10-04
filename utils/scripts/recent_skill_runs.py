"""Read formal check starts in the last hour and render actionable reminders."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from utils.scripts.structured_io import read_json
from utils.scripts.timestamp import iso_timestamp

SKILLS = ('project-doc-audit', 'sensitive-commit-check')


def check_verified(root, skill, run):
    """Verify archived artifacts without starting a run or refreshing its time."""
    import importlib.util
    path = root/'skills'/skill/'scripts'/('cli.py' if skill == 'project-doc-audit' else 'sensitive_commit_check.py')
    if not path.is_file():
        return False
    spec = importlib.util.spec_from_file_location('_recent_' + skill.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        if skill == 'project-doc-audit':
            return module.verify_report(root, run)
        module.ROOT = root
        module.RUNS = root/'logs'/skill/'runs'
        from contextlib import redirect_stdout
        from io import StringIO
        with redirect_stdout(StringIO()):
            state, code = module.verify(run)
        return code == 0 and state['status'] == 'approved'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, AttributeError, SyntaxError):
        return False


def collect_recent_runs(root: Path, now: str | None = None) -> dict:
    stamp = now or iso_timestamp()
    current = datetime.fromisoformat(stamp)
    result = {'checked_at': stamp, 'skills': {}}
    for skill in SKILLS:
        runs, invalid = [], []
        for path in sorted((root/'logs'/skill/'runs').glob('*/state.json')):
            try:
                state = read_json(path)
                started = datetime.fromisoformat(state['created_at'])
                if started.tzinfo is not None or iso_timestamp(started) != state['created_at']:
                    raise ValueError('启动时间格式无效')
                if started > current:
                    invalid.append(path.parent.name)
                    continue
                if current - started > timedelta(hours=1):
                    continue
                status = state['status']
                completed = status in {'completed', 'approved', 'blocked'}
                verified = check_verified(root, skill, path.parent.name) if completed else False
                runs.append({'run_id': path.parent.name, 'created_at': state['created_at'],
                             'status': status, 'completed': completed, 'verified': verified})
            except (OSError, ValueError, KeyError, TypeError):
                invalid.append(path.parent.name)
        result['skills'][skill] = {'runs': runs, 'invalid_records': invalid}
    return result


def render_reminders(checks: dict) -> str:
    lines = ['# 提交前检查提醒', '', f"检查时间：{checks['checked_at']}", '']
    for skill, item in checks['skills'].items():
        if not item['runs']:
            lines.append(f'- 最近 1h 未启动 `{skill}`，请在提交代码之前运行它。')
        for run in item['runs']:
            detail = ('已完成并通过核验' if run['verified'] else
                      '已结束但未通过核验，请处理问题并重新检查' if run['completed'] else
                      '失败或暂停，请恢复并完成检查' if run['status'] in {'failed', 'paused_error'} else
                      '尚未完成，请继续完成检查')
            lines.append(f"- `{skill}` / `{run['run_id']}`：`{run['status']}`，{detail}。")
        if item['invalid_records']:
            lines.append(f'- `{skill}` 有无法核实启动时间的记录，未计入最近 1h。')
    return '\n'.join(lines) + '\n'
