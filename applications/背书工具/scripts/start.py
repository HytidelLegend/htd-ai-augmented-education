"""Launch the local service and Vite GUI as one checked workflow."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory

APP = ROOT / "applications" / "背书工具"
TRANSITIONS = {
    "prepared": ("checking", "failed"),
    "checking": ("attaching_existing", "starting_service", "failed"),
    "attaching_existing": ("running", "failed"),
    "starting_service": ("verifying_service", "failed"),
    "verifying_service": ("building", "failed"),
    "building": ("starting_gui", "failed"),
    "starting_gui": ("running", "failed"),
    "running": ("cleanup", "failed"),
    "failed": ("cleanup",),
    "cleanup": ("completed", "failed"),
}


def check_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口超出范围")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def existing_app_mode(port: int, service_port: int, workspace: Path) -> str | None:
    try:
        with urlopen(f"http://127.0.0.1:{service_port}/health", timeout=1) as response:
            health = json.load(response)
        with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            page = response.read(65536).decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        return None
    if (health.get("status") != "ok" or health.get("protocolVersion") != 1
            or Path(health.get("workspace", "")).resolve() != workspace.resolve()
            or "<title>背书工具 · 记忆练习</title>" not in page):
        return None
    return "dev" if "/src/main.tsx" in page else "preview"


def stop(process: subprocess.Popen | None) -> None:
    if process and process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, check=False)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5175)
    parser.add_argument("--service-port", type=int, default=5176)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    run_dir = create_run_directory(ROOT / "logs" / "背书工具" / "runs")
    flow = WorkflowCheckpoint(TRANSITIONS, run_dir)
    service = gui = None
    service_log = gui_log = None
    try:
        flow.move("checking")
        if args.port == args.service_port:
            raise ValueError("GUI 与服务端口不能相同")
        occupied = []
        for port in (args.port, args.service_port):
            try:
                check_port(port)
            except OSError as error:
                if getattr(error, "winerror", None) != 10048:
                    raise
                occupied.append(port)
        workspace = ROOT / "outputs" / "背书工具"
        if occupied:
            existing_mode = existing_app_mode(args.port, args.service_port, workspace) if len(occupied) == 2 else None
            if existing_mode:
                flow.move("attaching_existing")
                flow.move("running")
                mode_name = "预览" if existing_mode == "preview" else "开发"
                print(f"背书工具已在运行（沿用现有{mode_name}模式实例）：http://127.0.0.1:{args.port}", flush=True)
                print(f"运行日志：{run_dir}", flush=True)
                while existing_app_mode(args.port, args.service_port, workspace):
                    time.sleep(1)
                raise RuntimeError("已有背书工具实例已退出或健康检查失败")
            raise RuntimeError(f"端口 {', '.join(map(str, occupied))} 已被占用；请关闭占用进程，或用 -Port 和 -ServicePort 指定空闲端口")
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("未找到 npm")
        workspace.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "VITE_BEISHU_API_URL": f"http://127.0.0.1:{args.service_port}", "PYTHONUTF8": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        flow.move("starting_service")
        service_log = (run_dir / "service.log").open("w", encoding="utf-8")
        service = subprocess.Popen([sys.executable, "-B", "-u", str(APP / "scripts" / "workspace_service.py"),
                                    "--workspace", str(workspace), "--port", str(args.service_port), "--log-root", str(run_dir)],
                                   cwd=ROOT, env=env, stdout=service_log, stderr=subprocess.STDOUT, creationflags=flags)
        flow.move("verifying_service")
        deadline = time.monotonic() + 10
        while True:
            if service.poll() is not None:
                raise RuntimeError("本地服务启动失败，请查看 service.log")
            try:
                with urlopen(env["VITE_BEISHU_API_URL"] + "/health", timeout=1) as response:
                    health = json.load(response)
                if health.get("protocolVersion") != 1 or Path(health.get("workspace", "")).resolve() != workspace.resolve():
                    raise RuntimeError("服务工作区或协议不匹配")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("本地服务健康检查超时")
                time.sleep(.1)
        flow.move("building")
        if not (APP / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=APP, env=env, check=True)
        subprocess.run([npm, "run", "build"], cwd=APP, env=env, check=True)
        flow.move("starting_gui")
        gui_log = (run_dir / "gui.log").open("w", encoding="utf-8")
        command = [npm, "run", "preview" if args.preview else "dev", "--", "--port", str(args.port)]
        gui = subprocess.Popen(command, cwd=APP, env=env, stdout=gui_log, stderr=subprocess.STDOUT, creationflags=flags)
        flow.move("running")
        print(f"背书工具：http://127.0.0.1:{args.port}", flush=True)
        print(f"运行日志：{run_dir}", flush=True)
        while gui.poll() is None and service.poll() is None:
            time.sleep(.25)
        if service.poll() is not None:
            raise RuntimeError("本地服务意外退出，请查看 service.log")
        return gui.returncode or 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if flow.state != "failed":
            flow.move("failed", error=str(error))
        print(f"启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        flow.move("cleanup")
        stop(gui)
        stop(service)
        if gui_log:
            gui_log.close()
        if service_log:
            service_log.close()
        flow.move("completed")


if __name__ == "__main__":
    raise SystemExit(main())
