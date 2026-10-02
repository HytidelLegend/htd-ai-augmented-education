"""Validate ZIP members before extracting into a fresh private directory."""
from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


def extract_zip(source: Path, destination: Path, *, max_bytes: int = 1024**3, max_files: int = 10000) -> None:
    if destination.exists():
        raise FileExistsError('解压目标已存在')
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > max_files or sum(x.file_size for x in members) > max_bytes:
            raise ValueError('ZIP 超出文件数量或解压大小上限')
        seen = set()
        for member in members:
            # ZipInfo normalizes backslashes on Windows and truncates at NUL;
            # validate the original archive name before using the normalized name.
            name = member.orig_filename
            path = PurePosixPath(name)
            key = str(path).casefold().rstrip('/')
            reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
            if (not name or '\x00' in name or '\\' in name or path.is_absolute() or '..' in path.parts
                    or any(':' in p or p.endswith((' ', '.')) for p in path.parts)
                    or any(p.split('.')[0].casefold() in reserved for p in path.parts)
                    or stat.S_ISLNK(member.external_attr >> 16) or key in seen):
                raise ValueError('ZIP 包含不安全或重复路径')
            target = (destination / str(path)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError('ZIP 路径越界')
            seen.add(key)
        destination.mkdir(parents=True)
        try:
            for member in members:
                target = destination / member.filename
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as inp, target.open('xb') as out:
                        shutil.copyfileobj(inp, out)
        except Exception:
            shutil.rmtree(destination)
            raise
