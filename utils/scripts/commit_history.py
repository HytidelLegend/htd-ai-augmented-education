"""First-parent commit evidence shared by release preparation and document audit."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import os
from pathlib import Path

try:
    from .git_repository import run_git, GitCommandError, untracked_paths, blob_oid
    from .structured_io import json_digest, validate_json_schema
except ImportError:
    from git_repository import run_git, GitCommandError, untracked_paths, blob_oid
    from structured_io import json_digest, validate_json_schema

RELEASE_ONLY = {'VERSION', 'docs/更新历史.md'}
VERSION_LINK = re.compile(r'(\*\*Version：\*\*\s*\[)0\.1\.\d+(\]\(VERSION\))')


def normalized_version_source(path: str, text: str) -> str:
    """Ignore only this project's derived version, retaining all other content."""
    if path == 'README.md':
        return VERSION_LINK.sub(r'\1VERSION\2', text)
    if path == '.claude-plugin/marketplace.json':
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get('plugins', []), list):
            raise ValueError('marketplace 必须为包含 plugins 列表的对象')
        for plugin in value.get('plugins', []):
            if not isinstance(plugin, dict):
                raise ValueError('marketplace plugins 成员必须为对象')
            if plugin.get('name') == 'htd-ai-augmented-education':
                plugin['version'] = 'VERSION'
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    return text


def version_at(index: int) -> str:
    if type(index) is not int or index < 0:
        raise ValueError('版本序号必须为非负整数')
    return f'0.1.{index}'


def first_parent_commits(root: Path) -> list[str]:
    if run_git(root, ['rev-parse', '--is-shallow-repository']) != 'false':
        raise ValueError('浅克隆无法确定首次提交；请先补齐 Git 历史')
    commits = run_git(root, ['rev-list', '--first-parent', '--reverse', 'HEAD']).splitlines()
    if not commits:
        raise ValueError('仓库尚无首次提交')
    return commits


def git_text(root: Path, ref: str, path: str) -> str | None:
    # Read an exact tree object, never a worktree substitute.
    args = ['show', f'{ref}:{path}' if ref else f':{path}']
    try:
        return run_git(root, args, strip=False)
    except GitCommandError:
        return None


def evidence(root: Path, commit: str, *, staged: bool = False) -> list[dict]:
    if staged:
        names = run_git(root, ['diff', '--cached', '--no-renames', '--name-only', '-z', commit, '--'], strip=False).split('\0')
        prefix = ['diff', '--cached', '--no-renames', '--binary', '--no-ext-diff', '--no-textconv', commit]
        parent = commit
    else:
        parents = run_git(root, ['rev-list', '--parents', '-n', '1', commit]).split()[1:]
        parent = parents[0] if parents else None
        names = (run_git(root, ['diff', '--no-renames', '--name-only', '-z', parent, commit, '--'], strip=False) if parent
                 else run_git(root, ['diff-tree', '--root', '--no-renames', '--no-commit-id', '--name-only', '-r', '-z', commit], strip=False)).split('\0')
        prefix = (['diff', '--no-renames', '--binary', '--no-ext-diff', '--no-textconv', parent, commit] if parent
                  else ['show', '--format=', '--root', '--no-renames', '--binary', '--no-ext-diff', '--no-textconv', commit])
    # One patch read per commit, rather than spawning Git for every file.
    full_patch = run_git(root, ['-c', 'core.quotepath=false', *prefix, '--'])
    blocks = re.split(r'(?=^diff --git )', full_patch, flags=re.M)
    patches = {}
    for block in blocks:
        if not block.startswith('diff --git '):
            continue
        header = block.splitlines()[0]
        # A spaced directory name can end in another path's " b/<name>".
        # Prefer the full longest path before testing suffix matches.
        for path in sorted(names, key=len, reverse=True):
            if path and (header.endswith(' b/' + path) or header.endswith(' ' + json.dumps('b/' + path, ensure_ascii=False))):
                patches[path] = block.rstrip()
                break
    records = []
    for path in sorted(set(names) - {''}):
        if staged and path in RELEASE_ONLY:
            continue
        if staged and path in {'README.md', '.claude-plugin/marketplace.json'}:
            before = normalized_version_source(path, git_text(root, parent, path) or ('{}' if path.endswith('.json') else ''))
            after = normalized_version_source(path, git_text(root, '', path) or ('{}' if path.endswith('.json') else ''))
            # Mode/type changes must remain reviewable even when text is unchanged.
            raw_modes = run_git(root, ['diff', '--cached', '--raw', '--no-abbrev', commit, '--', path]).split()
            modes_changed = bool(raw_modes and raw_modes[0].lstrip(':') != raw_modes[1])
            patch = (patches[path] if modes_changed else ''.join(difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), fromfile='a/' + path, tofile='b/' + path)))
            if not patch:
                continue
        else:
            if path not in patches:
                raise ValueError(f'无法定位提交差异：{path}')
            patch = patches[path]
        records.append({'evidence_id': f'E{len(records) + 1:04d}', 'path': path,
                        'patch_sha256': hashlib.sha256(patch.encode('utf-8')).hexdigest(), 'patch': patch})
    return records


def new_file_evidence(root: Path, path: str) -> dict:
    """Snapshot an addition without following links or changing the index."""
    target = root/path
    if target.is_symlink():
        content = os.readlink(target).encode('utf-8')
        mode = '120000'
        oid = blob_oid(root, content)
    elif target.is_file():
        content = target.read_bytes()
        executable = os.name != 'nt' and target.stat().st_mode & 0o111
        honors_mode = not executable or run_git(root, ['config', '--bool', 'core.filemode'], check=False) != 'false'
        mode = '100755' if executable and honors_mode else '100644'
        oid = blob_oid(root, content, path)
    else:
        raise ValueError(f'新文件无法取证：{path}')
    try:
        text = content.decode('utf-8')
        if '\0' in text:
            raise UnicodeError('binary')
        kind = 'symlink' if mode == '120000' else 'text'
        # Markdown is read with universal newlines; retain raw identity in the
        # content hash while rendering a stable LF-only textual patch.
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        patch = ''.join(difflib.unified_diff([], text.splitlines(True),
                                           fromfile='/dev/null', tofile='b/' + path))
        if not patch:
            patch = f'新增空文件：{path}\n'
    except UnicodeError:
        kind = 'binary'
        patch = f'新增二进制文件：{path}\n大小：{len(content)} bytes\nSHA256：{hashlib.sha256(content).hexdigest()}\n'
    return {'path': path, 'source': 'untracked', 'file_type': kind, 'mode': mode,
            'blob_oid': oid, 'size_bytes': len(content),
            'content_sha256': hashlib.sha256(content).hexdigest(),
            'patch': patch, 'patch_sha256': hashlib.sha256(patch.encode('utf-8')).hexdigest()}


def candidate_evidence(root: Path, head: str, include_untracked: bool) -> list[dict]:
    records = evidence(root, head, staged=True)
    index = {}
    raw = run_git(root, ['ls-files', '--stage', '-z'], strip=False)
    for entry in raw.split('\0'):
        if not entry:
            continue
        header, path = entry.split('\t', 1)
        mode, oid, stage = header.split()
        if stage != '0':
            raise ValueError('须先解决 Git 冲突')
        index[path] = (mode, oid)
    for item in records:
        item['source'] = 'staged'
        item['mode'], item['blob_oid'] = index.get(item['path'], ('000000', None))
    if include_untracked:
        records += [new_file_evidence(root, p) for p in untracked_paths(root) if p not in RELEASE_ONLY]
    records.sort(key=lambda item: item['path'])
    for index, item in enumerate(records, 1):
        item['evidence_id'] = f'E{index:04d}'
    return records


def assert_candidate(root: Path, packet: dict, *, staged_only: bool = False) -> None:
    """Bind frozen sources; allow an identical new blob to become staged."""
    validate_candidate_packet(packet)
    body = {k: v for k, v in packet.items() if k != 'packet_sha256'}
    if json_digest(body) != packet['packet_sha256']:
        raise ValueError('冻结证据指纹损坏')
    current = collect_packet(root, 'staged', include_untracked=True,
                             staged_only=staged_only)
    if any(packet[k] != current[k] for k in ('schema_version', 'mode', 'scope')) or len(packet['commits']) != 1:
        raise ValueError('候选证据格式不正确')
    if packet['head'] != current['head'] or any(packet['commits'][0][k] != current['commits'][0][k]
                                               for k in ('version', 'parent', 'commit')):
        raise ValueError('提交历史已变化')
    frozen = packet['commits'][0]['evidence']
    actual = current['commits'][0]['evidence']
    if [e['path'] for e in frozen] != [e['path'] for e in actual]:
        raise ValueError('候选文件范围已变化或新文件尚未暂存')
    for before, after in zip(frozen, actual):
        if before['source'] == 'untracked' and after['source'] == 'staged':
            if any(before[k] != after[k] for k in ('mode', 'blob_oid')):
                raise ValueError(f"新文件暂存内容已变化：{before['path']}")
        elif before['source'] == after['source'] == 'staged' and before['path'] in {'README.md', '.claude-plugin/marketplace.json'}:
            # Their normalized patches already bind every non-version change.
            # Publishing the derived version necessarily changes the raw blob.
            if ({k: v for k, v in before.items() if k != 'blob_oid'} !=
                    {k: v for k, v in after.items() if k != 'blob_oid'}):
                raise ValueError(f"版本展示文件的非版本内容或模式已变化：{before['path']}")
        elif before != after:
            raise ValueError(f"候选内容已变化：{before['path']}")


def validate_candidate_packet(packet: dict) -> None:
    schema = Path(__file__).resolve().parents[1]/'references/version-candidate-packet-v1.schema.json'
    validate_json_schema(packet, schema)
    records = packet['commits'][0]['evidence']
    paths = [item['path'] for item in records]
    if paths != sorted(set(paths)):
        raise ValueError('候选路径必须排序且不重复')
    for index, item in enumerate(records, 1):
        if item['evidence_id'] != f'E{index:04d}':
            raise ValueError('候选证据编号必须连续')
        if hashlib.sha256(item['patch'].encode('utf-8')).hexdigest() != item['patch_sha256']:
            raise ValueError('候选差异正文指纹不一致')


def collect_packet(root: Path, mode: str, *, include_untracked: bool = False,
                   staged_only: bool = False) -> dict:
    commits = first_parent_commits(root)
    if mode not in {'history', 'staged'}:
        raise ValueError('mode 必须为 history 或 staged')
    selected = commits if mode == 'history' else [commits[-1]]
    records = []
    for index, commit in enumerate(selected):
        parents = run_git(root, ['rev-list', '--parents', '-n', '1', commit]).split()[1:]
        records.append({'version': version_at(index if mode == 'history' else len(commits)),
                        'commit': commit if mode == 'history' else None,
                        'parent': parents[0] if parents and mode == 'history' else (commit if mode == 'staged' else None),
                        'evidence': (candidate_evidence(root, commit, not staged_only)
                                     if include_untracked and mode == 'staged' else
                                     evidence(root, commit, staged=mode == 'staged'))})
    packet = {'schema_version': '1.0', 'mode': mode, 'head': commits[-1], 'commits': records}
    if include_untracked and mode == 'staged':
        packet['schema_version'] = '1.1'
        packet['scope'] = 'staged_and_untracked'
    packet['packet_sha256'] = json_digest(packet)
    if include_untracked and mode == 'staged':
        validate_candidate_packet(packet)
    return packet
