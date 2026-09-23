"""UTF-8 subprocess execution with argument arrays, timeouts, logs, and tree termination."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence, TextIO

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


class SubprocessRunError(RuntimeError):
    """Raised when a checked subprocess fails or times out."""


@dataclass(frozen=True)
class RunResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


OutputMode = Literal["capture", "tee", "inherit"]
StartedCallback = Callable[[int], None]
OutputCallback = Callable[[str, str], bool | None]


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: deque[str] = deque()
        self.length = 0
        self.truncated = False

    def append(self, value: str) -> None:
        self.parts.append(value)
        self.length += len(value)
        while self.length > self.limit and self.parts:
            self.length -= len(self.parts.popleft())
            self.truncated = True

    def render(self) -> str:
        prefix = "[... earlier output truncated ...]\n" if self.truncated else ""
        return prefix + "".join(self.parts)


def run_command(
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 120,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
    check: bool = True,
    output_mode: OutputMode = "capture",
    on_started: StartedCallback | None = None,
    on_output: OutputCallback | None = None,
    capture_limit_chars: int = 256 * 1024,
) -> RunResult:
    """Run one command without a shell and persist a UTF-8 diagnostic log."""
    command = tuple(str(item) for item in args)
    if not command:
        raise ValueError("子进程参数不能为空")
    if output_mode not in {"capture", "tee", "inherit"}:
        raise ValueError(f"未知输出模式：{output_mode}")
    if capture_limit_chars <= 0:
        raise ValueError("capture_limit_chars 必须大于 0")
    merged_env = os.environ.copy()
    merged_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if output_mode == "tee":
        merged_env["PYTHONUNBUFFERED"] = "1"
    if env:
        merged_env.update(env)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    piped = output_mode in {"capture", "tee"}
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if piped else None,
        stderr=subprocess.PIPE if piped else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        creationflags=creationflags,
    )
    if on_started:
        try:
            on_started(process.pid)
        except Exception:
            _terminate_tree(process)
            process.wait()
            raise
    if output_mode == "tee":
        return _communicate_tee(
            process,
            command=command,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            check=check,
            on_output=on_output,
            capture_limit_chars=capture_limit_chars,
        )
    if output_mode == "inherit":
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_tree(process)
            process.wait()
            _write_log(log_path, command, -1, "", "", f"timeout={timeout_seconds}s; output_mode=inherit")
            raise SubprocessRunError(f"子进程超时（{timeout_seconds} 秒）：{command[0]}") from exc
        result = RunResult(command, returncode, "", "")
        _write_log(log_path, command, returncode, "", "", "output_mode=inherit")
        if check and returncode != 0:
            raise SubprocessRunError(f"子进程失败（exit {returncode}）：请查看控制台输出。")
        return result
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(process)
        stdout, stderr = process.communicate()
        _write_log(log_path, command, -1, stdout, stderr, f"timeout={timeout_seconds}s")
        raise SubprocessRunError(f"子进程超时（{timeout_seconds} 秒）：{command[0]}") from exc
    result = RunResult(command, process.returncode, stdout, stderr)
    _write_log(log_path, command, result.returncode, stdout, stderr, None)
    if check and result.returncode != 0:
        detail = stderr.strip() or stdout.strip() or "无输出"
        raise SubprocessRunError(f"子进程失败（exit {result.returncode}）：{detail}")
    return result


def _communicate_tee(
    process: subprocess.Popen[str],
    *,
    command: tuple[str, ...],
    timeout_seconds: int,
    log_path: Path | None,
    check: bool,
    on_output: OutputCallback | None,
    capture_limit_chars: int,
) -> RunResult:
    stdout_tail = _BoundedOutput(capture_limit_chars)
    stderr_tail = _BoundedOutput(capture_limit_chars)
    write_lock = threading.Lock()
    callback_errors: list[Exception] = []
    log_handle: TextIO | None = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("w", encoding="utf-8", newline="\n")
        log_handle.write(f"command={command!r}\n")
        log_handle.flush()

    def pump(name: str, source: TextIO, destination: TextIO, captured: _BoundedOutput) -> None:
        for line in iter(source.readline, ""):
            captured.append(line)
            visible = True
            if on_output:
                try:
                    visible = on_output(name, line) is not False
                except Exception as exc:
                    callback_errors.append(exc)
            with write_lock:
                if visible:
                    destination.write(line)
                    destination.flush()
                if log_handle:
                    suffix = "" if line.endswith("\n") else "\n"
                    log_handle.write(f"[{name}] {line}{suffix}")
                    log_handle.flush()
        source.close()

    assert process.stdout is not None and process.stderr is not None
    threads = (
        threading.Thread(target=pump, args=("stdout", process.stdout, sys.stdout, stdout_tail), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr, sys.stderr, stderr_tail), daemon=True),
    )
    for thread in threads:
        thread.start()
    note: str | None = None
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(process)
        returncode = process.wait()
        note = f"timeout={timeout_seconds}s"
        timeout_error: Exception | None = exc
        interrupt_error: BaseException | None = None
    except KeyboardInterrupt as exc:
        _terminate_tree(process)
        returncode = process.wait()
        note = "interrupted"
        timeout_error = None
        interrupt_error = exc
    else:
        timeout_error = None
        interrupt_error = None
    for thread in threads:
        thread.join()
    stdout = stdout_tail.render()
    stderr = stderr_tail.render()
    if log_handle:
        if note:
            log_handle.write(f"note={note}\n")
        log_handle.write(f"returncode={returncode}\n")
        log_handle.close()
    if timeout_error:
        raise SubprocessRunError(f"子进程超时（{timeout_seconds} 秒）：{command[0]}") from timeout_error
    if interrupt_error:
        raise interrupt_error
    if callback_errors:
        raise SubprocessRunError(f"子进程输出回调失败：{callback_errors[0]}") from callback_errors[0]
    result = RunResult(command, returncode, stdout, stderr)
    if check and returncode != 0:
        detail = stderr.strip() or stdout.strip() or "无输出"
        raise SubprocessRunError(f"子进程失败（exit {returncode}）：{detail}")
    return result


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    else:
        process.kill()


def _write_log(path: Path | None, args: tuple[str, ...], returncode: int, stdout: str, stderr: str, note: str | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"command={args!r}", f"returncode={returncode}"]
    if note:
        body.append(note)
    body.extend(["stdout:", stdout, "stderr:", stderr])
    path.write_text("\n".join(body), encoding="utf-8", newline="\n")
