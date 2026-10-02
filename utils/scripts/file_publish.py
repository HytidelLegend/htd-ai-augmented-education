"""Atomic publication helpers for verified generated files."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable


def publish_directory_without_overwrite(source: Path, target: Path, *, verifier: Callable[[Path], None] | None = None) -> None:
    """Stage and verify a complete directory beside its destination, then rename."""
    source = source.resolve()
    target = target.absolute()
    if not source.is_dir():
        raise ValueError("待发布目录不存在")
    if target.exists() or target.is_symlink():
        raise FileExistsError("发布目标已存在，拒绝覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".publishing-", dir=target.parent) as temporary:
        pending = Path(temporary) / "package"
        shutil.copytree(source, pending)
        if verifier:
            verifier(pending)
        if target.exists() or target.is_symlink():
            raise FileExistsError("发布目标已存在，拒绝覆盖")
        pending.rename(target)


def publish_file_without_overwrite(source: Path, target: Path) -> None:
    """Copy a verified file into place atomically and refuse overwrite."""
    source = source.resolve()
    target = target.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"待发布文件不存在或为空：{source}")
    if target.exists():
        raise FileExistsError(f"发布目标已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(raw_temp)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temp_path)
        if target.exists():
            raise FileExistsError(f"发布目标已存在，拒绝覆盖：{target}")
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
