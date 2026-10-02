"""Validated, revisioned skill configuration and safe-point snapshots."""
from pathlib import Path
import yaml
from .structured_io import read_json, write_json, json_digest, write_text_atomic
from .timestamp import iso_timestamp
from .file_transaction import project_lock

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ('beta-build-curriculum-navigation', 'beta-interactive-tutor')

def validate_config(skill, value):
    if skill not in SKILLS or not isinstance(value, dict):
        raise ValueError('配置对象或 skill 无效')
    expected = {'assessment', 'ordering'} if skill == SKILLS[0] else {'lesson', 'mastery', 'podcast'}
    if set(value) != expected:
        raise ValueError('配置分组不完整或包含未知参数')
    fields = {'assessment': {'max_multiple_choice_questions'}, 'ordering': {'max_candidate_orders', 'weights'},
              'lesson': {'max_multiple_choice_questions', 'max_open_ended_questions', 'max_new_units'},
              'mastery': {'good_min', 'medium_min'}, 'podcast': {'enabled'}}
    for group in expected:
        if not isinstance(value[group], dict) or set(value[group]) != fields[group]:
            raise ValueError(f'配置字段无效：{group}')
    if skill == SKILLS[0]:
        nums = [value['assessment']['max_multiple_choice_questions'], value['ordering']['max_candidate_orders']]
        weights = value['ordering']['weights']
        if set(weights) != {'goal_match', 'background_match', 'difficulty_smoothness', 'topic_continuity', 'downstream_unlock'}:
            raise ValueError('评分权重字段无效')
        if any(type(x) not in (int, float) or not 0 <= x <= 1 for x in weights.values()) or abs(sum(weights.values()) - 1) > 1e-8:
            raise ValueError('权重必须非负且总和为 1')
    else:
        nums = list(value['lesson'].values())
        m = value['mastery']
        if any(type(x) not in (int, float) for x in m.values()) or not 0 <= m['medium_min'] <= m['good_min'] <= 1:
            raise ValueError('掌握阈值必须满足 0 ≤ 中等 ≤ 良好 ≤ 1')
        if type(value['podcast']['enabled']) is not bool:
            raise ValueError('播客开关必须为布尔值')
    if any(type(x) is not int or not 1 <= x <= 100 for x in nums):
        raise ValueError('数量参数必须是 1～100 的整数')
    return value

def load_config(skill, root=ROOT):
    value = yaml.safe_load((root / 'skills' / skill / 'config.yaml').read_text(encoding='utf-8-sig'))
    validate_config(skill, value)
    return {'config': value, 'revision': json_digest(value)}

def save_config(skill, value, expected_revision, root=ROOT):
    validate_config(skill, value)
    path = root / 'skills' / skill / 'config.yaml'
    with project_lock(root / 'logs' / '交互式学习' / f'{skill}.lock', 'save-config'):
        if load_config(skill, root)['revision'] != expected_revision:
            raise ValueError('配置版本冲突，请重新加载')
        write_text_atomic(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
    return load_config(skill, root)

def freeze_config(skill, run_dir, root=ROOT):
    current = load_config(skill, root)
    meta = run_dir / 'config-meta.json'
    old = read_json(meta) if meta.is_file() else None
    changed = old is not None and old['revision'] != current['revision']
    if old is None or changed:
        run_dir.mkdir(parents=True, exist_ok=True)
        if old:
            history = run_dir / 'config-history' / old['revision']
            history.mkdir(parents=True, exist_ok=True)
            write_text_atomic(history / 'config.yaml', (run_dir / 'config.yaml').read_text(encoding='utf-8'))
            write_json(history / 'meta.json', old)
        write_text_atomic(run_dir / 'config.yaml', yaml.safe_dump(current['config'], allow_unicode=True, sort_keys=False))
        write_json(meta, {**current, 'applied_at': iso_timestamp(), 'notification_pending': changed or bool(old and old.get('notification_pending'))})
    pending = changed or bool(old and old.get('notification_pending'))
    return {**current, 'changed': changed, 'message': '参数已改变，本次运行以新参数为准。' if pending else ''}

def acknowledge_config(run_dir):
    """A failed command must not consume the configuration-change notification."""
    meta = run_dir / 'config-meta.json'
    if meta.is_file():
        value = read_json(meta)
        if value.get('notification_pending'):
            value['notification_pending'] = False
            write_json(meta, value)
