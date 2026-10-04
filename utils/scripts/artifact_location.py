"""Persist output locations for composed skills while leaving state/logs in logs/."""
from pathlib import Path
import os
from .structured_io import read_json, write_json


def location_file(root, skill, run_id):
    if Path(skill).name != skill or skill in ('', '.', '..') or Path(run_id).name != run_id or run_id in ('', '.', '..'):
        raise ValueError('非法 skill 或 run ID')
    return root / 'logs' / skill / 'runs' / run_id / 'artifact-location.json'


def checked_output(root, value):
    path = Path(value).resolve()
    path.relative_to((root / 'outputs').resolve())
    for part in [Path(value), *Path(value).parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('产物路径不能包含链接')
    return path


def register(root, skill, run_id, output):
    target = checked_output(root, output)
    path = location_file(root, skill, run_id)
    record = {'schema_version': '1.0', 'output_dir': str(target)}
    if path.is_file() and read_json(path) != record:
        raise ValueError('产物目录绑定不能改变')
    write_json(path, record)
    return target


def output_dir(root, skill, run_id):
    path = location_file(root, skill, run_id)
    if path.is_file():
        record = read_json(path)
        if set(record) != {'schema_version', 'output_dir'} or record['schema_version'] != '1.0':
            raise ValueError('产物目录绑定无效')
        return checked_output(root, record['output_dir'])
    return root / 'outputs' / skill / 'runs' / run_id


def is_embedded(root, skill, run_id):
    return location_file(root, skill, run_id).is_file()


def staging_dir(root, skill, run_id):
    if is_embedded(root, skill, run_id):
        target = output_dir(root, skill, run_id) / 'staging'
        # FFmpeg on Windows cannot reliably open temporary files under deeply
        # nested embedded output paths. Keep only staging files in the skill log
        # run when the eventual temporary filename would approach MAX_PATH.
        if os.name != 'nt' or len(str(target)) + 40 < 240:
            return target
    return root / 'logs' / skill / 'runs' / run_id / 'staging'
