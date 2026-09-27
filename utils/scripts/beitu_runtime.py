"""Start the local editor and its matching native service as one workflow."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory

APP = ROOT / "applications" / "背图工具"
TRANSITIONS = {
    "prepared": ("checking_port", "failed"),
    "checking_port": ("starting_service", "failed"),
    "starting_service": ("verifying_workspace", "failed"),
    "verifying_workspace": ("building", "failed"),
    "building": ("starting_gui", "failed"),
    "starting_gui": ("running", "failed"),
    "running": ("cleanup", "failed"),
    "failed": ("cleanup",),
    "cleanup": ("completed", "failed"),
}


def check_port(port: int) -> None:
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"端口 {port} 已被占用，请先关闭原来的启动终端；不会复用已有服务") from error


def verify_health(health: dict, workspace: Path, instance_id: str) -> int:
    service_pid = health.get("servicePid")
    if (health.get("protocolVersion") != 2
            or not isinstance(service_pid, int) or service_pid <= 0
            or health.get("instanceId") != instance_id
            or Path(health.get("workspace", "")).resolve() != workspace.resolve()):
        raise RuntimeError("本地服务实例、协议或工作区不匹配")
    return service_pid


def stop_process(process: subprocess.Popen | None) -> None:
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--service-port", type=int, default=5174)
    parser.add_argument("--workspace", type=Path, default=ROOT / "outputs" / "背图工具")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    run_dir = create_run_directory(ROOT / "logs" / "背图工具" / "runs")
    flow = WorkflowCheckpoint(TRANSITIONS, run_dir)
    service = gui = None
    service_log = None
    exit_code = 0
    try:
        flow.move("checking_port")
        if args.port == args.service_port:
            raise RuntimeError("GUI 和服务端口不能相同")
        check_port(args.port)
        check_port(args.service_port)
        node, npm = shutil.which("node"), shutil.which("npm")
        if not node or not npm:
            raise RuntimeError("需要安装 Node.js 和 npm")
        workspace = args.workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        instance = str(uuid.uuid4())
        env = {**os.environ, "VITE_BEITU_API_URL": f"http://127.0.0.1:{args.service_port}", "PYTHONUTF8": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        flow.move("starting_service")
        service_log = (run_dir / "service.log").open("w", encoding="utf-8")
        service = subprocess.Popen([
            sys.executable, "-B", "-u", str(APP / "scripts" / "workspace_service.py"),
            "--workspace", str(workspace), "--port", str(args.service_port),
            "--instance-id", instance, "--log-dir", str(run_dir),
        ], stdout=service_log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        flow.move("verifying_workspace", servicePid=service.pid, serviceWorkspace=str(workspace))
        deadline = time.monotonic() + 10
        while True:
            if service.poll() is not None:
                raise RuntimeError("本地服务启动失败，详情见 service.log")
            try:
                with urlopen(env["VITE_BEITU_API_URL"] + "/health", timeout=1) as response:
                    health = json.load(response)
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("本地服务健康检查超时")
                time.sleep(.1)
                continue
            actual_service_pid = verify_health(health, workspace, instance)
            flow.update(servicePid=actual_service_pid)
            break
        flow.move("building")
        if not (APP / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=APP, env=env, check=True)
        subprocess.run([npm, "run", "build"], cwd=APP, env=env, check=True)
        flow.move("starting_gui")
        command = [node, str(APP / "node_modules" / "vite" / "bin" / "vite.js")]
        if args.preview:
            command.append("preview")
        gui = subprocess.Popen(command + ["--host", "127.0.0.1", "--port", str(args.port), "--strictPort"],
                               cwd=APP, env=env)
        flow.move("running", guiPid=gui.pid)
        print(f"打开 http://127.0.0.1:{args.port}；按 Ctrl+C 关闭。日志：{run_dir}", flush=True)
        while gui.poll() is None:
            if service.poll() is not None:
                raise RuntimeError("本地写入服务意外退出")
            time.sleep(.25)
        if gui.returncode:
            raise RuntimeError(f"GUI 退出码：{gui.returncode}")
    except KeyboardInterrupt:
        if flow.state != "running":
            exit_code = 5
            flow.move("failed", errorCode="启动已取消")
    except Exception as error:
        exit_code = 5
        flow.move("failed", errorCode=str(error))
        print(str(error), file=sys.stderr)
    finally:
        flow.move("cleanup")
        stop_process(gui)
        stop_process(service)
        if service_log:
            service_log.close()
        flow.move("completed" if exit_code == 0 else "failed")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
