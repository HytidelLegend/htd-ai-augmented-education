"""Small, reusable Git command helpers for project skills."""
from __future__ import annotations

import subprocess
import re
import hashlib
import json
import os
import ast
from pathlib import Path
from typing import Sequence


class GitCommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr.strip()


def run_git(root: Path, args: Sequence[str], *, check: bool = True, strip: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if check and proc.returncode:
        raise GitCommandError(args, proc.returncode, proc.stderr)
    return proc.stdout.strip() if strip else proc.stdout


def is_repository(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def untracked_paths(root: Path) -> list[str]:
    """Enumerate individual unignored files without stripping path whitespace."""
    return sorted(set(run_git(root, ['ls-files', '--others', '--exclude-standard', '-z'],
                              strip=False).split('\0')) - {''})


def blob_oid(root: Path, content: bytes, path: str | None = None) -> str:
    """Hash a captured blob with Git clean filters, without writing objects."""
    args = ['hash-object', '--stdin']
    if path is not None:
        args.append('--path=' + path)
    proc = subprocess.run(['git', *args], cwd=root, input=content, capture_output=True)
    if proc.returncode:
        raise GitCommandError(args, proc.returncode, proc.stderr.decode('utf-8', 'replace'))
    return proc.stdout.decode('ascii').strip()


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


def change_snapshot(root: Path, scope: str) -> tuple[list[dict], str]:
    """Freeze changed content, without scanning old lines or following symlinks.

    The returned digest binds HEAD, patches and selected blobs. Callers must not
    persist the transient content: it may contain secrets.
    """
    if scope not in {"worktree", "staged"}:
        raise ValueError("scope must be worktree or staged")

    def git_bytes(args: list[str], *, data: bytes | None = None) -> bytes:
        proc = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True)
        if proc.returncode:
            raise GitCommandError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    if git_bytes(["ls-files", "--unmerged", "-z"]):
        raise RuntimeError("Resolve Git conflicts before scanning")

    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True)
    base = head.stdout.strip().decode("ascii") if head.returncode == 0 else git_bytes(
        ["hash-object", "-t", "tree", "--stdin"], data=b""
    ).strip().decode("ascii")
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--ignore-submodules=none"]
    if scope == "staged":
        diff_args.append("--cached")
    diff_args.append(base)
    names = iter(git_bytes(diff_args + ["--name-status", "-z", "--"]).decode("utf-8", "strict").split("\0"))
    paths, old_paths = [], {}
    for status in names:
        if not status:
            continue
        path = next(names)
        if status.startswith("R"):
            old_path, path = path, next(names)
            old_paths[path] = old_path
        paths.append(path)
    untracked = set()
    if scope == "worktree":
        untracked = set(untracked_paths(root))
    records = []
    digest = hashlib.sha256()
    digest.update(json.dumps([scope, base]).encode())
    for path in sorted((set(paths) | untracked) - {""}):
        selected = [old_paths[path], path] if path in old_paths else [path]
        patch = b"" if path in untracked else git_bytes(diff_args + ["--unified=0", "--src-prefix=a/", "--dst-prefix=b/", "--", *[f":(literal){name}" for name in selected]])
        content = b""
        exists = True
        special = None
        if scope == "staged":
            entries = git_bytes(["ls-files", "--stage", "-z", "--", f":(literal){path}"]).split(b"\0")
            entry = next((item for item in entries if item), b"")
            if not entry:
                exists = False
            else:
                mode, oid, stage = entry.split(b"\t", 1)[0].split()
                if stage != b"0":
                    raise RuntimeError("Resolve Git conflicts before scanning")
                if mode == b"160000":
                    special = "submodule"
                    content = oid
                else:
                    content = git_bytes(["cat-file", "blob", oid.decode("ascii")])
        else:
            target = root / path
            if target.is_symlink():
                content = os.readlink(target).encode("utf-8")
            elif target.is_file():
                content = target.read_bytes()
            elif target.is_dir():
                special = "submodule"
                content = patch
            else:
                exists = False
        for value in (path.encode("utf-8"), patch, content, str(exists).encode()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        records.append({"path": path, "content": content, "patch": patch,
                        "exists": exists, "untracked": path in untracked,
                        "special": special})
    return records, digest.hexdigest()


def added_line_blocks(record: dict) -> list[tuple[int, str]]:
    """Return contiguous added lines with their line numbers in the new file."""
    if not record["exists"]:
        return []
    if record["untracked"]:
        content = record["content"].decode("utf-8", "strict")
        return [(1, content)] if content else []
    patch = record["patch"]
    blocks = []
    lines = []
    start = current = 0
    in_hunk = False
    selected = False

    def flush():
        if lines:
            blocks.append((start, (b"\n".join(lines) + b"\n").decode("utf-8", "strict")))
            lines.clear()

    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git "):
            flush()
            in_hunk = selected = False
            continue
        if not in_hunk and line.startswith(b"+++ "):
            path = line[4:]
            if path.startswith(b'"'):
                # Git C-quotes whitespace and UTF-8 bytes. Evaluate only a
                # bytes literal, never code or a shell expression.
                literal = "b" + "".join(chr(byte) if byte < 128 else f"\\x{byte:02x}" for byte in path)
                path = ast.literal_eval(literal)
            else:
                # An unquoted header can end in Git's tab separator. Literal
                # tabs in filenames are always C-quoted, even with quotePath=false.
                path = path.split(b"\t", 1)[0]
            selected = path == b"b/" + record["path"].encode("utf-8")
            continue
        match = re.match(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match and selected:
            flush()
            current = int(match.group(1))
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            if not lines:
                start = current
            lines.append(line[1:])
            current += 1
        elif in_hunk and line.startswith(b" "):
            flush()
            current += 1
        elif in_hunk and line.startswith(b"-"):
            flush()
    flush()
    return blocks
