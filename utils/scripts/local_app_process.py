"""Inspect and restart verified Windows application processes without extra dependencies."""
from __future__ import annotations

import base64
from contextlib import contextmanager, ExitStack
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


def windows_snapshot(ports: tuple[int, ...]) -> dict:
    # PowerShell is used only for Windows' process/connection inventory.
    script = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate)
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(%s) } | Select-Object LocalPort,OwningProcess)
@{processes=$processes; listeners=$listeners} | ConvertTo-Json -Depth 4 -Compress
""" % ",".join(str(port) for port in ports)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=20,
    )
    return json.loads(result.stdout.lstrip("\ufeff"))


def command_arguments(command: str) -> list[str]:
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def owns_process(process: dict, scripts: tuple[Path, ...]) -> bool:
    """Match the interpreter's script argument, never a command-line substring."""
    name = str(process.get("Name", "")).lower()
    if name not in {"python.exe", "pythonw.exe", "node.exe", "powershell.exe", "pwsh.exe"}:
        return False
    args = command_arguments(process.get("CommandLine") or "")
    index = 1
    while index < len(args):
        argument = args[index]
        index += 1
        if name in {"powershell.exe", "pwsh.exe"}:
            if argument.lower() in {"-noprofile", "-noexit", "-noninteractive", "-file"}:
                continue
            if argument.lower() == "-executionpolicy":
                index += 1
                continue
        elif argument in {"-X", "-W"}:
            index += 1
            continue
        elif argument in {"-B", "-u", "-I", "-E", "-s", "-S", "--"}:
            continue
        if argument.startswith("-"):
            return False
        path = Path(argument)
        return path.is_absolute() and path.resolve() in {item.resolve() for item in scripts}
    return False


def restart_plan(snapshot: dict, scripts: tuple[Path, ...], current_pid: int,
                 launcher_scripts: tuple[Path, ...] = ()) -> list[dict]:
    processes = {int(item["ProcessId"]): item for item in snapshot["processes"]}
    protected = set()
    pid = current_pid
    while pid in processes and pid not in protected:
        protected.add(pid)
        pid = int(processes[pid]["ParentProcessId"])
    if launcher_scripts:
        def creation_order(item):
            match = re.fullmatch(r"/Date\((-?\d+)\)/", str(item.get("CreationDate", "")))
            if not match:
                raise RuntimeError("无法核对启动器的进程创建顺序")
            return int(match.group(1))
        if current_pid not in processes:
            raise RuntimeError("无法核对当前启动器身份")
        cutoff = creation_order(processes[current_pid])
        queued = {pid for pid, item in processes.items()
                  if pid not in protected and owns_process(item, launcher_scripts)
                  and creation_order(item) >= cutoff}
        # A second click may already be waiting on the startup lock while the
        # first click inventories old processes. Never stop that queued tree.
        while True:
            descendants = {pid for pid, item in processes.items()
                           if int(item["ParentProcessId"]) in queued}
            expanded = queued | descendants
            if expanded == queued:
                break
            queued = expanded
        protected.update(queued)
    owned = {pid: item for pid, item in processes.items()
             if pid not in protected and owns_process(item, scripts)}
    for listener in snapshot["listeners"]:
        pid = int(listener["OwningProcess"])
        if pid not in owned:
            raise RuntimeError(f"端口 {listener['LocalPort']} 被其他程序（PID {pid}）占用，请关闭该程序或指定其他端口")
    # Include venv interpreter wrappers and old launchers; kill each tree once.
    roots = []
    for pid, item in owned.items():
        parent = int(item["ParentProcessId"])
        visited = {pid}
        while parent in processes and parent not in visited and parent not in owned:
            visited.add(parent)
            parent = int(processes[parent]["ParentProcessId"])
        if parent not in owned:
            roots.append(item)
    return roots


def stop_tree(pid: int) -> None:
    result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        # A process may exit between inventory and taskkill.
        from utils.scripts.file_transaction import _process_is_alive
        if _process_is_alive(pid):
            raise RuntimeError(f"无法关闭旧进程 PID {pid}，请检查权限")


def check_ports(ports: tuple[int, ...]) -> None:
    for port in ports:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise RuntimeError(f"端口 {port} 尚未释放或无法使用") from error


def restart_application(ports: tuple[int, ...], scripts: tuple[Path, ...], *,
                        launcher_scripts: tuple[Path, ...] = ()) -> None:
    if os.name == "nt":
        plan = restart_plan(windows_snapshot(ports), scripts, os.getpid(), launcher_scripts)
        if plan:
            print("正在关闭已有应用并释放端口…", flush=True)
            # Recheck identities before stopping; a recycled PID must not be killed.
            fresh = {int(item["ProcessId"]): item for item in windows_snapshot(ports)["processes"]}
            for process in plan:
                pid = int(process["ProcessId"])
                current = fresh.get(pid)
                if current is None:
                    continue
                if (current["CreationDate"] != process["CreationDate"] or
                        not owns_process(current, scripts)):
                    raise RuntimeError("旧进程身份发生变化，请重新启动")
                stop_tree(pid)
    deadline = time.monotonic() + 10
    while True:
        try:
            check_ports(ports)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        stop_tree(process.pid)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def startup_lock(path: Path, timeout: float = 120):
    """Serialize repeat launches, including a second click during a build."""
    from utils.scripts.file_transaction import inspect_lock, project_lock
    deadline = time.monotonic() + timeout
    announced = False
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(project_lock(path, "start-application"))
                break
            except (ValueError, FileExistsError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("已有启动仍未完成，请稍后重试")
                if not inspect_lock(path).get("exists"):
                    continue
                if not announced:
                    print("已有启动正在进行，等待完成后重新启动…", flush=True)
                    announced = True
                time.sleep(.25)
        yield


def run_logged_command(args: list[str], *, cwd: Path, env: dict, log) -> None:
    """Own the command tree so interrupted installs/builds cannot leave children."""
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=log,
                               stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        returncode = process.wait()
        if returncode:
            raise subprocess.CalledProcessError(returncode, args)
    finally:
        stop_process(process)


def configure_powershell_open(log_root: Path) -> int:
    """Explicit, repeatable current-user setup; preserves the previous command in a log."""
    from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory
    run = create_run_directory(log_root)
    workflow = WorkflowCheckpoint({"prepared": ("checking", "failed"),
                                   "checking": ("configuring", "failed"),
                                   "configuring": ("completed", "failed"),
                                   "completed": (), "failed": ()}, run)
    log_path = run / "startup-entry.log"
    try:
        workflow.move("checking")
        if os.name != "nt":
            raise RuntimeError("双击关联配置只适用于 Windows")
        import winreg
        import re
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            raise RuntimeError("未找到 Windows PowerShell")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.ps1\UserChoice") as key:
                progid = winreg.QueryValueEx(key, "ProgId")[0]
        except FileNotFoundError:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".ps1") as key:
                progid = winreg.QueryValueEx(key, "")[0]
        if not isinstance(progid, str) or not re.fullmatch(r"[\w.-]+", progid):
            raise RuntimeError("无法安全识别 .ps1 文件关联")
        relative = progid + r"\shell\open\command"
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, relative) as key:
                previous = winreg.QueryValueEx(key, "")[0]
        except FileNotFoundError:
            previous = "(not configured)"
        command = f'"{powershell}" -NoProfile -NoExit -File "%1"'
        log_path.write_text("Previous .ps1 open command:\n" + previous +
                            "\nApplied current-user open command:\n" + command + "\n", encoding="utf-8")
        workflow.move("configuring")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\" + relative) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        workflow.move("completed")
        print(f"已配置当前用户的 .ps1 双击关联；原命令记录：{log_path}", flush=True)
        return 0
    except Exception as error:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"Configuration failed: {error}\n")
        workflow.move("failed")
        print(f"关联配置失败：{error}；日志：{log_path}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    parser = argparse.ArgumentParser(description="Configure the current user's PowerShell double-click entry")
    parser.add_argument("--configure-ps1-open", action="store_true", required=True)
    parser.add_argument("--log-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "logs/local-app-process/runs")
    args = parser.parse_args()
    raise SystemExit(configure_powershell_open(args.log_root))
