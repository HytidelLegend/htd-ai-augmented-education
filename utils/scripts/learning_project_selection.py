"""Read-only local navigation discovery and persistent tutor intake decisions."""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path

from .structured_io import read_json, write_json_transaction, json_digest, validate_json_schema
from .timestamp import iso_timestamp, unique_filename_timestamp
from .markdown_report import markdown_table
from .file_transaction import project_lock
from .learning_navigation_bundle import load_navigation_bundle

ROOT = Path(__file__).resolve().parents[2]
NAV = 'beta-build-curriculum-navigation'
TUTOR = 'beta-interactive-tutor'
TRANSITIONS = {
    'discovering_projects': {'awaiting_project_selection', 'navigation_required'},
    'awaiting_project_selection': {'validating_selection', 'discovering_projects'},
    'validating_selection': {'awaiting_tutor_selection', 'project_resolved', 'discovering_projects'},
    'awaiting_tutor_selection': {'validating_selection', 'discovering_projects'},
    'navigation_required': {'discovering_projects'},
    'project_resolved': set(),
}


def safe_path(root: Path, value: str | Path) -> Path:
    from .learning_project import safe_path as resolve
    return resolve(root, value)


@lru_cache(maxsize=1)
def navigation_verifier():
    path = ROOT / 'skills' / NAV / 'scripts/build_curriculum_navigation.py'
    spec = importlib.util.spec_from_file_location('selection_navigation_verifier', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify


def valid_navigation(root: Path, path: Path, material_project: Path | None = None):
    nav = read_json(path)
    validate_json_schema(nav, ROOT / 'utils/references/learning-navigation-v2.schema.json')
    assessment = nav['planning_profile'].get('assessment')
    if (not isinstance(assessment, dict) or assessment.get('schema_version') != '2.0'
            or assessment.get('status') != 'completed'):
        raise ValueError('缺少已完成的新版诊断')
    try:
        navigation_verifier()(root, path, material_project, refresh_config=False)
        nav = load_navigation_bundle(root, path, material_project)['navigation']
    except (TypeError, AttributeError, IndexError) as exc:
        raise ValueError('导航内部数据结构无效') from exc
    return nav


def tutor_projects(root: Path, navigation: Path):
    """Find standard and custom tutor projects through their run checkpoints."""
    paths = set((root / 'outputs' / TUTOR / 'runs').glob('*/项目.json'))
    for checkpoint in (root / 'logs' / TUTOR / 'runs').glob('*/state.json'):
        try:
            saved = read_json(checkpoint)
            if isinstance(saved, dict) and isinstance(saved.get('project_dir'), str):
                paths.add(safe_path(root, saved['project_dir']) / '项目.json')
        except (ValueError, KeyError, OSError):
            continue
    projects = []
    for path in sorted(paths):
        try:
            path = safe_path(root, path)
            model_path = safe_path(root, path.parent / 'artifacts/学习路线.json')
            if not path.is_file() or not model_path.is_file():
                continue
            from . import learning_project as lp
            with lp.lock(path.parent):
                model = lp.load(path.parent)
            if safe_path(root, model['navigation_json']) != navigation:
                continue
            # Metadata discovery does not alter progress or require an intact backup.
            projects.append({'project_id': model['project_id'], 'title': model['title'],
                             'project_dir': path.parent.relative_to(root).as_posix(),
                             'state': model['state'], 'revision': model['revision']})
        except (ValueError, KeyError, OSError):
            continue
    return projects


def checkpoint_order(path):
    """Order same-second runs by numeric collision suffix, rather than lexically."""
    base, separator, suffix = path.parent.name.rpartition('_')
    return (base, int(suffix)) if separator and suffix.isdigit() else (path.parent.name, 0)


def discover(root: Path):
    root = root.resolve()
    candidates = {}
    known = {}
    # The latest checkpoint for a path governs eligibility, including unfinished runs.
    for checkpoint in sorted((root / 'logs' / NAV / 'runs').glob('*/run-state.json'), key=checkpoint_order, reverse=True):
        claimed = root / 'outputs' / NAV / 'runs' / checkpoint.parent.name / 'navigation.json'
        try:
            state = read_json(safe_path(root, checkpoint))
            if isinstance(state, dict) and isinstance(state.get('navigation_json'), str):
                claimed = safe_path(root, state['navigation_json'])
            validate_json_schema(state, ROOT / 'utils/references/learning-navigation-run-state-v2.schema.json')
            if (isinstance(state, dict)
                    and isinstance(state.get('navigation_json'), str)
                    and isinstance(state.get('run_id'), str) and state['run_id']):
                path = safe_path(root, state['navigation_json'])
                if path not in known:
                    known[path] = state.get('status')
                    if state.get('status') == 'completed':
                        candidates[path] = state['run_id']
            else:
                known.setdefault(claimed, 'invalid')
        except (ValueError, KeyError, OSError):
            known.setdefault(claimed, 'invalid')
            continue
    # Published default runs also work when local audit logs were removed.
    for path in sorted((root / 'outputs' / NAV / 'runs').glob('*/navigation.json')):
        try:
            path = safe_path(root, path)
            if path not in known:
                candidates.setdefault(path, path.parent.name)
        except ValueError:
            continue
    entries = []
    navigation_hashes = {}
    for path, run_id in candidates.items():
        try:
            projects = tutor_projects(root, path)
            reason = ''
            try:
                nav = valid_navigation(root, path)
            except (ValueError, KeyError, OSError):
                nav = None
                for project in projects:
                    try:
                        nav = valid_navigation(root, path, safe_path(root, project['project_dir']))
                        reason = '已有材料副本可恢复；新建仍需原始材料有效'
                        break
                    except (ValueError, KeyError, OSError):
                        continue
                if nav is None:
                    continue
            relative = path.relative_to(root).as_posix()
            entry = {'candidate_id': 'NAV-' + json_digest(relative)[:16],
                            'run_id': run_id, 'title': nav['title'], 'updated_at': nav['updated_at'],
                            'navigation_path': relative, 'navigation_status': 'completed',
                            'availability': 'available', 'reason': reason,
                            'tutor_projects': projects}
            validate_json_schema({'schema_version': '1.0', 'catalog_sha256': json_digest([]), 'entries': [entry]},
                                 ROOT / 'utils/references/learning-project-catalog-v1.schema.json')
            navigation_hashes[relative] = json_digest(nav)
            entries.append(entry)
        except (ValueError, KeyError, OSError):
            continue
    entries.sort(key=lambda item: (item['updated_at'], item['run_id'], item['candidate_id']), reverse=True)
    catalog = {'schema_version': '1.0', 'catalog_sha256': json_digest({'entries': entries, 'navigation_hashes': navigation_hashes}), 'entries': entries}
    validate_json_schema(catalog, ROOT / 'utils/references/learning-project-catalog-v1.schema.json')
    return catalog


def render(catalog):
    if not catalog['entries']:
        return '没有已完成且有效的学习导航，请先调用 beta-build-curriculum-navigation 完成导航。\n'
    return markdown_table(['序号', '项目名称', 'run ID', '更新时间', '状态', '已有导师项目数'],
                          [(i, e['title'], e['run_id'], e['updated_at'],
                            '可恢复；新建需原材料' if e['reason'] else '可选择', len(e['tutor_projects']))
                           for i, e in enumerate(catalog['entries'], 1)]) + '\n\n请回复序号或 candidate_id 选择项目。\n'


def transition(state, target):
    if target not in TRANSITIONS[state['status']]:
        raise ValueError(f"非法选择状态迁移：{state['status']} → {target}")
    state['status'] = target
    state['updated_at'] = iso_timestamp()


def save(directory, state):
    validate_json_schema(state['catalog'], ROOT / 'utils/references/learning-project-catalog-v1.schema.json')
    validate_json_schema(state, ROOT / 'utils/references/learning-project-selection-v1.schema.json')
    # Publish the state and its exact catalog together, including on recovery.
    write_json_transaction({
        directory / 'selection-state.json': state,
        directory / 'project-catalog.json': state['catalog'],
    })


def load_state(directory):
    state = read_json(directory / 'selection-state.json')
    validate_json_schema(state, ROOT / 'utils/references/learning-project-selection-v1.schema.json')
    validate_json_schema(state['catalog'], ROOT / 'utils/references/learning-project-catalog-v1.schema.json')
    if state['request']:
        validate_json_schema(state['request'], ROOT / 'utils/references/interactive-tutor-request-v2.schema.json')
    root = Path(state['root']).resolve()
    directory = safe_path(root, directory)
    if directory.parent != root / 'logs' / TUTOR / 'runs':
        raise ValueError('选择状态必须位于导师 logs/runs 目录')
    if state['status'] == 'awaiting_tutor_selection' and not any(
            e['candidate_id'] == state['selected_candidate_id'] and e['tutor_projects']
            for e in state['catalog']['entries']):
        raise ValueError('导师选择状态缺少对应导航及关联项目')
    return directory, state


def resume(directory):
    directory, _ = load_state(directory)
    with project_lock(directory / '.selection.lock', 'learning-project-selection'):
        _, state = load_state(directory)
        if state['status'] == 'project_resolved':
            return receipt(directory, state)
        if state['status'] in ('awaiting_project_selection', 'awaiting_tutor_selection'):
            if discover(Path(state['root'])) == state['catalog']:
                return receipt(directory, state)
        return refresh(directory, state)


def receipt(directory, state):
    return {'status': state['status'], 'selection_dir': str(directory),
            'catalog_sha256': state['catalog']['catalog_sha256'],
            'markdown': state['markdown'], 'result': state.get('result')}


def refresh(directory, state):
    if state['status'] != 'discovering_projects':
        transition(state, 'discovering_projects')
    save(directory, state)
    state['catalog'] = discover(Path(state['root']))
    state['markdown'] = render(state['catalog'])
    state['selected_candidate_id'] = None
    transition(state, 'awaiting_project_selection' if state['catalog']['entries'] else 'navigation_required')
    save(directory, state)
    return receipt(directory, state)


def start(root, request=None):
    root = root.resolve()
    runs = root / 'logs' / TUTOR / 'runs'
    stamp = unique_filename_timestamp([p.name for p in runs.iterdir()] if runs.exists() else [])
    directory = runs / stamp
    directory.mkdir(parents=True, exist_ok=False)
    now = iso_timestamp()
    state = {'schema_version': '1.0', 'status': 'discovering_projects', 'root': str(root),
             'created_at': now, 'updated_at': now, 'catalog': {'schema_version': '1.0',
             'catalog_sha256': json_digest([]), 'entries': []}, 'selected_candidate_id': None, 'markdown': '',
             'request': request or {}}
    return refresh(directory, state)


def select(directory, choice, catalog_sha256, *, tutor=False, finish):
    """Resolve a tiny user decision; finish receives new/resume and its safe target."""
    directory, _ = load_state(directory)
    with project_lock(directory / '.selection.lock', 'learning-project-selection'):
        _, state = load_state(directory)
        root = Path(state['root'])
        safe_path(root, directory)
        if state['status'] == 'project_resolved':
            return receipt(directory, state)
        if state['status'] == 'navigation_required':
            return refresh(directory, state)
        expected = 'awaiting_tutor_selection' if tutor else 'awaiting_project_selection'
        if state['status'] != expected:
            raise ValueError('当前状态不能提交此类选择')
        current = discover(root)
        if catalog_sha256 != state['catalog']['catalog_sha256'] or current != state['catalog']:
            return refresh(directory, state)
        entries = current['entries']
        if tutor:
            entry = next(e for e in entries if e['candidate_id'] == state['selected_candidate_id'])
        else:
            entry = next((e for i, e in enumerate(entries, 1)
                          if choice in (str(i), e['candidate_id'])), None)
            if entry is None:
                raise ValueError('项目序号或 candidate_id 无效')
        mode, project = 'new', None
        if tutor:
            choices = entry['tutor_projects']
            picked = next((p for i, p in enumerate(choices, 1)
                           if choice in (str(i), p['project_id'], p['project_dir'])), None)
            if choice not in ('new', '0') and picked is None:
                raise ValueError('导师项目选择无效；新建请用 new 或 0')
            if picked:
                mode, project = 'resume', safe_path(root, picked['project_dir'])
        transition(state, 'validating_selection')
        state['selected_candidate_id'] = entry['candidate_id']
        save(directory, state)
        nav_path = safe_path(root, entry['navigation_path'])
        try:
            if not tutor and entry['tutor_projects']:
                transition(state, 'awaiting_tutor_selection')
                state['markdown'] = markdown_table(['序号', '导师项目', '项目 ID', '状态', '项目目录'],
                    [(i, p['title'], p['project_id'], p['state'], p['project_dir'])
                     for i, p in enumerate(entry['tutor_projects'], 1)]) + '\n\n0. 新建导师项目（也可回复 new）。\n'
                if entry['reason']:
                    state['markdown'] += '\n' + entry['reason'] + '。\n'
                save(directory, state)
                return receipt(directory, state)
            valid_navigation(root, nav_path, project)
            result = finish(mode, root, nav_path, project, state['request'])
        except Exception:
            refresh(directory, state)
            raise
        state['result'] = result
        transition(state, 'project_resolved')
        state['markdown'] = '已选择学习项目。\n'
        save(directory, state)
        return receipt(directory, state)
