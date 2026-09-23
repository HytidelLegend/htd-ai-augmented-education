"""Small recoverable filesystem transaction used by content-factory skills."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


def _process_is_alive(pid: int) -> bool:
    """Return whether a process is alive without sending a signal on Windows.

    Windows implements ``os.kill(pid, 0)`` with ``TerminateProcess`` for signal
    values other than console control events, so it cannot be used as the
    POSIX-style liveness probe here. Unknown or access-denied states are treated
    as alive so a lock is never reclaimed without confirming owner exit.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        error_invalid_parameter = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(
            process_query_limited_information | synchronize,
            False,
            pid,
        )
        if not handle:
            return ctypes.get_last_error() != error_invalid_parameter
        try:
            wait_result = wait_for_single_object(handle, 0)
            if wait_result == wait_object_0:
                return False
            if wait_result == wait_timeout:
                return True
            return True
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def inspect_lock(lock_path: Path) -> dict[str, object]:
    """Return structured lock state; legacy locks remain readable but not reclaimable."""
    if not lock_path.is_file():
        return {"exists": False, "path": lock_path.as_posix()}
    raw = lock_path.read_text(encoding="utf-8").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "exists": True,
            "path": lock_path.as_posix(),
            "legacy": True,
            "owner": raw,
            "reclaimable": False,
        }
    pid = int(payload.get("pid", 0) or 0)
    same_host = str(payload.get("host", "")) == socket.gethostname()
    alive = _process_is_alive(pid) if same_host else None
    return {
        "exists": True,
        "path": lock_path.as_posix(),
        **payload,
        "legacy": False,
        "process_alive": alive,
        "reclaimable": same_host and alive is False,
    }


def recover_stale_lock(lock_path: Path) -> bool:
    """Remove a structured lock only when its local owner process is confirmed dead."""
    state = inspect_lock(lock_path)
    if not state.get("reclaimable"):
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True


def retry_io(operation, *, attempts: int = 5, base_delay_seconds: float = 0.1) -> None:
    """Retry short-lived Windows sharing and invalid-handle errors, preserving the final exception."""
    for attempt in range(attempts):
        try:
            operation()
            return
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(base_delay_seconds * (attempt + 1))


@contextmanager
def project_lock(lock_path: Path, owner: str):
    """Acquire a fail-fast cross-process lock for a project transaction."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not recover_stale_lock(lock_path):
            state = inspect_lock(lock_path)
            raise ValueError(f"已有事务正在执行：{lock_path}；锁状态={state}")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        payload = {
            "owner": owner,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at_epoch": time.time(),
        }
        os.write(
            descriptor,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        retry_io(lambda: lock_path.unlink(missing_ok=True))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_files(root: Path, paths: Iterable[Path], snapshot_dir: Path) -> dict[str, str]:
    """Copy existing project files to a snapshot and persist their hashes."""
    root = root.resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for path in dict.fromkeys(item.resolve() for item in paths):
        relative = path.relative_to(root).as_posix()
        if not path.is_file():
            raise ValueError(f"事务快照目标不是文件：{relative}")
        destination = snapshot_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        manifest[relative] = file_sha256(path)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def verify_fingerprints(root: Path, fingerprints: dict[str, str]) -> None:
    changed = [relative for relative, digest in fingerprints.items() if not (root / relative).is_file() or file_sha256(root / relative) != digest]
    if changed:
        raise ValueError("预检后文件已变化：" + "、".join(changed))


def restore_files(root: Path, snapshot_dir: Path) -> None:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    for relative in manifest:
        source = snapshot_dir / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
