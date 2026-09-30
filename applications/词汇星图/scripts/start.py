"""Start the dictionary API and Vite frontend in one terminal."""
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP = Path(__file__).resolve().parents[1]


def wait_for(url: str, process: subprocess.Popen, seconds: int = 25) -> None:
    for _ in range(seconds * 5):
        if process.poll() is not None:
            raise RuntimeError(f"服务提前退出：{process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            time.sleep(.2)
    raise TimeoutError(f"启动超时：{url}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    python = ROOT / "runtime" / ".venv" / "Scripts" / "python.exe"
    node = shutil.which("node.exe") or shutil.which("node")
    vite = APP / "node_modules" / "vite" / "bin" / "vite.js"
    if not python.is_file() or not node:
        raise RuntimeError("需要项目隔离 Python 环境及 Node.js/npm")
    if not vite.is_file():
        raise RuntimeError("请先在 applications/词汇星图/ 运行 npm ci")
    for port in (5185, 5186):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"端口 {port} 已被占用，请先停止旧的词汇星图服务")
    api = subprocess.Popen([str(python), str(APP / "scripts" / "service.py"), "serve", "--root", str(ROOT)], cwd=ROOT)
    frontend = None
    try:
        wait_for("http://127.0.0.1:5186/health", api)
        frontend = subprocess.Popen([node, str(vite), "--host", "127.0.0.1", "--port", "5185", "--strictPort"], cwd=APP)
        wait_for("http://127.0.0.1:5185/", frontend)
        print("词汇星图：http://127.0.0.1:5185/", flush=True)
        if not args.no_browser:
            webbrowser.open("http://127.0.0.1:5185/")
        while api.poll() is None and frontend.poll() is None:
            time.sleep(.5)
        raise RuntimeError("本地服务已退出")
    except KeyboardInterrupt:
        return 0
    finally:
        for process in (frontend, api):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
