"""Restart the local service and UI through a checkpointed launch workflow."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.local_app_process import restart_application, run_logged_command, startup_lock, stop_process
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory

APP = ROOT / "applications/交互式学习"
STAGES = ("prepared", "checking", "restarting", "service_starting", "building", "gui_starting", "running")
TRANSITIONS = {stage: (STAGES[index + 1], "cleaning") if index + 1 < len(STAGES)
               else ("cleaning",) for index, stage in enumerate(STAGES)}
TRANSITIONS.update(cleaning=("completed", "failed"), completed=(), failed=())


class LaunchParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"启动参数无效：{message}")


def wait_ready(url: str, processes: tuple[subprocess.Popen, ...], timeout: float = 15,
               *, expected_json: dict | None = None, marker: bytes | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("应用进程提前退出，请查看 service.log、gui.log")
        try:
            with urlopen(url, timeout=1) as response:
                body = response.read(4096)
                if expected_json is not None:
                    payload = json.loads(body)
                    valid = isinstance(payload, dict) and all(payload.get(key) == value
                                                               for key, value in expected_json.items())
                else:
                    valid = marker is None or marker in body
                if response.status == 200 and valid and all(process.poll() is None for process in processes):
                    return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(.1)
    raise RuntimeError(f"应用就绪检查超时：{url}")


def main() -> int:
    run_dir = create_run_directory(ROOT / "logs/交互式学习/runs")
    print(f"运行日志：{run_dir}", flush=True)
    workflow = WorkflowCheckpoint(TRANSITIONS, run_dir)
    service = gui = None
    failure = None
    try:
        workflow.move("checking")
        parser = LaunchParser()
        parser.add_argument("--port", type=int, default=5177)
        parser.add_argument("--service-port", type=int, default=5178)
        args = parser.parse_args()
        if not all(1 <= port <= 65535 for port in (args.port, args.service_port)):
            raise ValueError("端口必须在 1～65535 之间")
        if args.port == args.service_port:
            raise ValueError("GUI 与服务端口不能相同")
        npm, node = shutil.which("npm"), shutil.which("node")
        if not npm or not node:
            raise RuntimeError("未找到 Node.js 或 npm，请安装后重试")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "VITE_LEARNING_API_URL": f"http://127.0.0.1:{args.service_port}"}
        with (run_dir / "service.log").open("w", encoding="utf-8") as service_log, \
             (run_dir / "build.log").open("w", encoding="utf-8") as build_log, \
             (run_dir / "gui.log").open("w", encoding="utf-8") as gui_log:
            # Serialize only startup; a later invocation may restart the running app.
            with startup_lock(ROOT / "logs/交互式学习/startup.lock"):
                workflow.move("restarting")
                restart_application((args.port, args.service_port), (
                    APP / "run.ps1", Path(__file__).resolve(), APP / "scripts/workspace_service.py",
                    APP / "node_modules/vite/bin/vite.js",
                ), launcher_scripts=(APP / "run.ps1", Path(__file__).resolve()))
                workflow.move("service_starting")
                service = subprocess.Popen(
                    [sys.executable, "-B", "-u", str(APP / "scripts/workspace_service.py"),
                     "--port", str(args.service_port)], cwd=ROOT, env=env,
                    stdout=service_log, stderr=subprocess.STDOUT, creationflags=flags,
                )
                wait_ready(env["VITE_LEARNING_API_URL"] + "/health", (service,),
                           expected_json={"status": "ok", "protocolVersion": 3})
                workflow.move("building")
                if not (APP / "node_modules").is_dir():
                    print("正在安装前端依赖…", flush=True)
                    run_logged_command([npm, "ci"], cwd=APP, env=env, log=build_log)
                print("正在检查并构建前端…", flush=True)
                run_logged_command([npm, "run", "build"], cwd=APP, env=env, log=build_log)
                workflow.move("gui_starting")
                gui = subprocess.Popen(
                    [node, str(APP / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1",
                     "--port", str(args.port), "--strictPort"], cwd=APP, env=env,
                    stdout=gui_log, stderr=subprocess.STDOUT, creationflags=flags,
                )
                wait_ready(f"http://127.0.0.1:{args.port}/", (service, gui), marker=b'id="root"')
                workflow.move("running")
                print(f"交互式学习：http://127.0.0.1:{args.port}", flush=True)
            while gui.poll() is None and service.poll() is None:
                time.sleep(.25)
            raise RuntimeError("应用进程已退出，请查看 service.log、gui.log")
    except KeyboardInterrupt:
        print("正在关闭应用…", flush=True)
    except Exception as error:
        failure = str(error)
        (run_dir / "startup.log").write_text(traceback.format_exc(), encoding="utf-8")
        print(f"启动失败：{failure}\n完整错误：{run_dir / 'startup.log'}", file=sys.stderr, flush=True)
    except SystemExit as error:
        if error.code:
            failure = "启动参数无效"
    finally:
        workflow.move("cleaning")
        for process in (gui, service):
            try:
                stop_process(process)
            except Exception as error:
                failure = f"{failure or ''} 清理失败：{error}".strip()
                print(failure, file=sys.stderr, flush=True)
        workflow.move("failed" if failure else "completed")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
