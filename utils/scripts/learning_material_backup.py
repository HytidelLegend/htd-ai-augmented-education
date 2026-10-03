"""Versioned, byte-preserving study material backups and path resolution."""
from __future__ import annotations

import re
import os
import shutil
import html
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .markdown_structure import sha256_file
from .structured_io import read_json, write_json, json_digest, validate_json_schema
from .timestamp import iso_timestamp

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'utils/references/learning-material-backup-v1.schema.json'
TRANSITIONS = {
    None: {'planning_backup'},
    'planning_backup': {'awaiting_backup_approval', 'paused_backup_error'},
    'awaiting_backup_approval': {'planning_backup', 'copying_materials', 'paused_backup_error', 'paused_backup_conflict'},
    'copying_materials': {'verifying_backup', 'planning_backup', 'paused_backup_error', 'paused_backup_conflict'},
    'verifying_backup': {'backup_ready', 'planning_backup', 'paused_backup_error'},
    'backup_ready': {'planning_backup'},
    'paused_backup_error': {'planning_backup'},
    'paused_backup_conflict': {'planning_backup'},
}


def backup_stage(run_dir, name, **fields):
    path = run_dir / 'material-backup-state.json'
    current = read_json(path).get('state') if path.is_file() else None
    if name not in TRANSITIONS.get(current, set()):
        raise ValueError(f'非法备份状态迁移：{current} → {name}')
    write_json(path, {'state': name, 'updated_at': iso_timestamp(), **fields})


def finish_backup(run_dir, plan):
    backup_stage(run_dir, 'backup_ready', plan_sha256=json_digest(plan))


def markdown_references(text):
    """Collect used destinations; code examples and unused definitions are not resources."""
    lines = []; fence = None; list_indent = None
    for line in text.splitlines():
        line = re.sub(r'^(?: {0,3}> ?)+', '', line)
        item = re.match(r'^( *)(?:[-+*]|\d+[.)]) +', line)
        indent = len(line) - len(line.lstrip(' '))
        if not fence:
            if item:
                list_indent = len(item[0]); line = line[len(item[0]):]
            elif line.strip() and list_indent is not None and indent < list_indent:
                list_indent = None
            elif line.strip() and indent >= (list_indent or 0) + 4:
                continue  # Indented code, including code nested inside a list.
            elif list_indent is not None and indent >= list_indent:
                line = line[list_indent:]
        elif list_indent is not None and indent >= list_indent:
            line = line[list_indent:]
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            if re.fullmatch(r'\s{0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1])); continue
        lines.append(line)
    text = re.sub(r'<!--.*?-->', '', '\n'.join(lines), flags=re.S)
    text = re.sub(r'(`+).*?\1(?!`)', '', text, flags=re.S)
    def label(value): return ' '.join(value.split()).casefold()
    definitions = {}
    def definition(match):
        definitions.setdefault(label(match[1]), match[2])
        return ''
    text = re.sub(r'^\s{0,3}\[([^\]]+)\]:\s*(<[^>]*>|\S+)[^\n]*$', definition, text, flags=re.M)
    refs = []
    for match in re.finditer(r'!?\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', text):
        refs.append(match[1])
    text = re.sub(r'!?\[\[[^\]]*\]\]', '', text)
    index = 0
    while index < len(text):
        if text[index] != '[' or (index and text[index - 1] == '\\'):
            index += 1; continue
        start = index + 1; depth = 1; index += 1
        while index < len(text) and depth:
            if text[index] == '\\': index += 2; continue
            if text[index] == '[': depth += 1
            elif text[index] == ']': depth -= 1
            index += 1
        if depth: break
        title = text[start:index - 1]
        if '![' in title:
            refs.extend(markdown_references(title))
        if index < len(text) and text[index] == '(':
            index += 1
            while index < len(text) and text[index].isspace(): index += 1
            start = index; depth = 0; angle = index < len(text) and text[index] == '<'
            if angle: index += 1; start = index
            while index < len(text):
                char = text[index]
                if char == '\\': index += 2; continue
                if angle and char == '>': break
                if not angle:
                    if char == '(' : depth += 1
                    elif char == ')':
                        if depth == 0: break
                        depth -= 1
                    elif char.isspace() and depth == 0: break
                index += 1
            refs.append(text[start:index])
        else:
            key = title
            if index < len(text) and text[index] == '[':
                end = text.find(']', index + 1)
                if end != -1:
                    key = text[index + 1:end] or title; index = end + 1
            if label(key) in definitions:
                refs.append(definitions[label(key)].strip('<>'))
    class Resources(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if value and key in ('src', 'href', 'poster'):
                    refs.append(value)
                elif value and key == 'srcset':
                    refs.extend(part.strip().split()[0] for part in value.split(',') if part.strip() and not value.startswith('data:'))
    Resources().feed(text)
    return [html.unescape(re.sub(r'\\([^\w\s])', r'\1', ref)) for ref in refs]


class BackupApprovalRequired(ValueError):
    def __init__(self, plan, digest, project, run_dir):
        super().__init__('请确认全部备份路径后，使用 --backup-plan-sha256 恢复')
        self.receipt = {'status': 'awaiting_backup_approval', 'project_dir': str(project),
                        'run_dir': str(run_dir), 'backup_plan_sha256': digest,
                        'planned_paths': [f['backup_path'] for m in plan['materials'] for f in material_files(m)],
                        'manifest_path': 'artifacts/学习材料备份.json', 'message': str(self)}
        self.receipt['planned_paths'].append('artifacts/学习材料备份.json')
        if manifest_path(project).is_file():
            old = read_json(manifest_path(project))
            self.receipt['planned_paths'].append(f'artifacts/material-backup-history/{json_digest(old)}.json')


class BackupConflict(ValueError):
    def __init__(self, path):
        super().__init__(f'备份目标冲突，须用户选择处理方式：{path}')
        self.receipt = {'status': 'paused_backup_conflict', 'conflict_path': path, 'message': str(self)}


def checked_path(root, value):
    root = root.resolve()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('材料路径不可包含链接')
    path = path.resolve()
    path.relative_to(root)
    return path


def manifest_path(project):
    return project / 'artifacts/学习材料备份.json'


def checked_project(root, project):
    project = checked_path(root, project)
    for excluded in ('runtime', 'logs'):
        if project.is_relative_to(root.resolve() / excluded):
            raise ValueError('学习材料产物不可写入 runtime 或 logs')
    return project


def collect_files(root, source):
    """Follow local Markdown resources only, keeping their original root-relative tree."""
    pending = [source]; files = {}
    while pending:
        path = checked_path(root, pending.pop())
        relative = path.relative_to(root).as_posix()
        if relative in files:
            continue
        if not path.is_file():
            raise ValueError(f'本地材料或资源不存在：{relative}')
        files[relative] = {'original_path': relative, 'sha256': sha256_file(path), 'size_bytes': path.stat().st_size}
        if path.suffix.lower() not in ('.md', '.markdown'):
            continue
        text = path.read_text(encoding='utf-8-sig')
        for raw in markdown_references(text):
            url = urlsplit(raw)
            if url.scheme == 'file' or re.match(r'^[A-Za-z]:[\\/]', raw):
                raise ValueError('材料含绝对本地引用，须先确认可迁移的引用方案')
            if url.scheme or url.netloc or not url.path:
                continue
            local = unquote(url.path)
            if local.startswith('/'):
                raise ValueError('材料含绝对本地引用，须先确认可迁移的引用方案')
            target = path.parent / local
            pending.append(checked_path(root, target))
    return [files[key] for key in sorted(files)]


def backup_plan(root, project, navigation):
    root = root.resolve(); project = checked_project(root, project)
    materials = []
    for source in navigation['sources']:
        original = checked_path(root, source['path'])
        if sha256_file(original) != source['sha256']:
            raise ValueError(f"来源哈希已变化：{source['path']}")
        files = collect_files(root, original)
        version = json_digest(files)
        base = Path('学习材料') / source['source_id'] / version
        for file in files:
            file['backup_path'] = (base / file['original_path']).as_posix()
        materials.append({'source_id': source['source_id'], 'original_path': source['path'],
                          'backup_path': (base / source['path']).as_posix(), 'sha256': source['sha256'],
                          'size_bytes': original.stat().st_size, 'version': version,
                          'resources': [f for f in files if f['original_path'] != source['path']]})
    plan = {'schema_version': '1.0', 'navigation_revision': json_digest(navigation), 'materials': materials}
    validate_json_schema(plan, SCHEMA)
    return plan


def material_files(material):
    return [{key: material[key] for key in ('original_path', 'backup_path', 'sha256', 'size_bytes')}, *material['resources']]


def protect_inputs(root, plan, targets):
    # Compare recorded input names without inspecting the original files again.
    # Normal teaching must still work if those paths have since moved or changed.
    inputs = {(root.resolve() / f['original_path']).absolute() for m in plan['materials'] for f in material_files(m)}
    if inputs.intersection(path.resolve() for path in targets):
        raise ValueError('学习材料与运行写入路径重合，禁止修改输入文件')


def verify_backup(root, project, navigation, plan=None):
    project = checked_project(root, project)
    plan = read_json(manifest_path(project)) if plan is None else plan
    validate_json_schema(plan, SCHEMA)
    if plan['navigation_revision'] != json_digest(navigation):
        raise ValueError('材料备份与导航版本不一致；请显式同步新版导航')
    expected = {s['source_id']: s for s in navigation['sources']}
    if len(plan['materials']) != len(expected) or {m['source_id'] for m in plan['materials']} != set(expected):
        raise ValueError('材料备份来源清单不一致')
    paths = {}
    for material in plan['materials']:
        source = expected[material['source_id']]
        if (material['original_path'], material['sha256']) != (source['path'], source['sha256']):
            raise ValueError('材料备份来源版本不一致')
        records = material_files(material)
        originals = [f['original_path'] for f in records]
        if len(set(originals)) != len(originals):
            raise ValueError('材料备份资源路径重复')
        version = json_digest(sorted([{k: f[k] for k in ('original_path', 'sha256', 'size_bytes')} for f in records], key=lambda f: f['original_path']))
        if version != material['version']:
            raise ValueError('材料备份资源清单与版本不一致')
        for file in material_files(material):
            relative = Path(file['original_path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('材料备份原始路径越界')
            expected_path = (Path('学习材料') / material['source_id'] / version / relative).as_posix()
            if file['backup_path'] != expected_path:
                raise ValueError('材料备份路径与版本不一致')
            path = checked_path(project, file['backup_path'])
            path.relative_to(project / '学习材料')
            if not path.is_file() or path.stat().st_size != file['size_bytes'] or sha256_file(path) != file['sha256']:
                raise ValueError(f"材料副本缺失或损坏：{file['backup_path']}")
        paths[source['path']] = checked_path(project, material['backup_path'])
    return paths


def ensure_backup(root, project, navigation, run_dir, approved_plan_sha256=None, refresh=False, publish=True):
    """The invoking skill must preview the plan and obtain authorization before calling."""
    project = checked_project(root, project)
    if manifest_path(project).is_file():
        old = read_json(manifest_path(project))
        if not refresh and old.get('navigation_revision') == json_digest(navigation):
            paths = verify_backup(root, project, navigation)
            return paths if publish else old
    def stage(name, **fields):
        backup_stage(run_dir, name, **fields)
    # Read and check inputs before touching state/plan files that might be referenced.
    plan = backup_plan(root, project, navigation)
    controls = [run_dir / name for name in ('material-backup-state.json', 'material-backup-plan.json', 'material-backup-approval.json')]
    controls.append(manifest_path(project))
    controls.extend(project / 'artifacts/material-backup-staging' / f['sha256'] for m in plan['materials'] for f in material_files(m))
    protect_inputs(root, plan, controls)
    stage('planning_backup')
    try:
        digest = json_digest(plan)
        write_json(run_dir / 'material-backup-plan.json', plan)
        stage('awaiting_backup_approval', plan_sha256=digest)
        approval_path = run_dir / 'material-backup-approval.json'
        prior = read_json(approval_path) if approval_path.is_file() else {}
        if plan['materials'] and approved_plan_sha256 != digest and prior.get('plan_sha256') != digest:
            raise BackupApprovalRequired(plan, digest, project, run_dir)
        write_json(approval_path, {'plan_sha256': digest, 'confirmed_at': iso_timestamp()})
        # Check the whole batch before creating any material files.
        for material in plan['materials']:
            for file in material_files(material):
                target = checked_path(project, file['backup_path'])
                if target.exists() and (not target.is_file() or sha256_file(target) != file['sha256']):
                    raise BackupConflict(file['backup_path'])
                for parent in target.parents:
                    if parent == project:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise BackupConflict(parent.relative_to(project).as_posix())
        stage('copying_materials', plan_sha256=digest)
        for material in plan['materials']:
            for file in material_files(material):
                source = checked_path(root, file['original_path'])
                target = checked_path(project, file['backup_path'])
                if target.exists():
                    if not target.is_file() or sha256_file(target) != file['sha256']:
                        raise BackupConflict(file['backup_path'])
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # A partial copy stays in project staging, never at a published path.
                temporary = checked_path(project, Path('artifacts/material-backup-staging') / file['sha256'])
                temporary.parent.mkdir(parents=True, exist_ok=True)
                # A crash may leave a staging hard link to an already published copy.
                # Detach it before writing, so a failed retry cannot damage that copy.
                temporary.unlink(missing_ok=True)
                shutil.copyfile(source, temporary)
                if sha256_file(temporary) != file['sha256'] or sha256_file(source) != file['sha256']:
                    raise ValueError('复制期间源文件发生变化')
                os.link(temporary, target)
                temporary.unlink()
        stage('verifying_backup', plan_sha256=digest)
        for material in plan['materials']:
            for file in material_files(material):
                if sha256_file(checked_path(project, file['backup_path'])) != file['sha256'] or sha256_file(checked_path(root, file['original_path'])) != file['sha256']:
                    raise ValueError('备份复制校验失败')
        verify_backup(root, project, navigation, plan=plan)
        if not publish:
            return plan
        if manifest_path(project).is_file():
            old = read_json(manifest_path(project))
            write_json(project / 'artifacts/material-backup-history' / f"{json_digest(old)}.json", old)
        write_json(manifest_path(project), plan)
        finish_backup(run_dir, plan)
        return verify_backup(root, project, navigation)
    except BackupApprovalRequired:
        raise
    except BackupConflict as exc:
        stage('paused_backup_conflict', error=str(exc), resume_stage='planning_backup')
        raise
    except Exception as exc:
        stage('paused_backup_error', error=str(exc), resume_stage='planning_backup')
        raise
