"""Small, reusable Git command helpers for project skills."""
from __future__ import annotations

import subprocess
import re
from pathlib import Path
from typing import Sequence


class GitCommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr.strip()


def run_git(root: Path, args: Sequence[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if check and proc.returncode:
        raise GitCommandError(args, proc.returncode, proc.stderr)
    return proc.stdout.strip()


def is_repository(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def status_entries(root: Path) -> list[dict[str, str]]:
    raw = run_git(root, ["status", "--short", "-z"])
    entries: list[dict[str, str]] = []
    for item in raw.split("\0"):
        if not item:
            continue
        code = item[:2]
        path = item[3:] if len(item) >= 3 and item[2] == " " else item[2:]
        code = code or "??"
        entries.append({"status": code or "??", "path": path, "old_path": None})
    return entries


def redact_remote_url(url: str) -> str:
    """Remove embedded HTTP credentials while preserving SSH-style URLs."""
    return re.sub(r"(https?://)([^/@]+):([^/@]+)@", r"\1<redacted>@", url)
