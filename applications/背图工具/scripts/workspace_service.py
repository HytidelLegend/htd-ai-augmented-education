"""Native JSON workspace service for 背图工具.

The browser is used for the editor and image handles. Project JSON is written
by this localhost process so the commit and verification happen in a native
filesystem context rather than inside a browser File System Access transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import uuid
import sys
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory

SAVE_TRANSITIONS = {
    "prepared": ("validating_revision", "failed"),
    "validating_revision": ("writing_pending", "committed", "conflict", "failed"),
    "writing_pending": ("verifying_pending", "failed"),
    "verifying_pending": ("backing_up", "failed"),
    "backing_up": ("replacing_project", "failed"),
    "replacing_project": ("verifying_final", "failed"),
    "verifying_final": ("committed", "failed"),
}


class RevisionConflict(RuntimeError):
    def __init__(self, details: dict):
        super().__init__("PROJECT_REVISION_CONFLICT")
        self.details = details

PROJECT_ID = re.compile(r"^[a-f0-9-]{36}$", re.IGNORECASE)
DIRECTORY_NAME = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]{1,100}$")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_project(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict) or not PROJECT_ID.fullmatch(str(value.get("projectId", ""))):
        raise ValueError("项目 JSON 无效")
    if not isinstance(value.get("rectangles"), list):
        raise ValueError("项目矩形数据无效")
    return value, data


def fsync_directory(path: Path) -> None:
    # Windows does not allow opening a directory this way; the file fsync and
    # os.replace still provide the atomic native commit there.
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json_write(path: Path, value: dict, operation_id: str | None = None, workflow: WorkflowCheckpoint | None = None) -> tuple[str, int, int, str]:
    payload = json_bytes(value)
    operation_id = operation_id or str(uuid.uuid4())
    pending = path.with_name(f".{path.name}.{operation_id}.pending")
    backup = path.with_name(f".{path.name}.{operation_id}.bak")
    previous = path.read_bytes() if path.exists() else None
    try:
        if workflow:
            workflow.move("writing_pending")
        with pending.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if workflow:
            workflow.move("verifying_pending")
        if pending.read_bytes() != payload:
            raise IOError("临时文件校验失败")
        if workflow:
            workflow.move("backing_up")
        if previous is not None:
            backup.write_bytes(previous)
            with backup.open("rb+") as stream:
                os.fsync(stream.fileno())
        if workflow:
            workflow.move("replacing_project")
        os.replace(pending, path)
        fsync_directory(path.parent)
        if workflow:
            workflow.move("verifying_final")
        if path.read_bytes() != payload:
            raise IOError("原子替换后文件校验失败")
        stat = path.stat()
        backup.unlink(missing_ok=True)
        return digest(payload), stat.st_size, stat.st_mtime_ns // 1_000_000, operation_id
    except Exception:
        if previous is not None and backup.exists():
            os.replace(backup, path)
            fsync_directory(path.parent)
        pending.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise


class WorkspaceService:
    def __init__(self, root: Path, log_dir: Path | None = None, instance_id: str | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.save_lock = threading.Lock()
        self.log_dir = log_dir
        self.instance_id = instance_id or str(uuid.uuid4())
        self.operations: OrderedDict[str, tuple[str, dict]] = OrderedDict()

    def health(self) -> dict:
        return {"status": "ok", "workspace": str(self.root), "servicePid": os.getpid(),
                "instanceId": self.instance_id, "protocolVersion": 2}

    def project_path(self, directory_name: str) -> Path:
        if not DIRECTORY_NAME.fullmatch(directory_name) or directory_name in {".", ".."}:
            raise ValueError("项目目录名无效")
        path = (self.root / directory_name).resolve()
        if path.parent != self.root:
            raise ValueError("项目目录必须位于工作区根目录")
        path.mkdir(parents=True, exist_ok=True)
        return path / "project.json"

    def find_project(self, project_id: str) -> tuple[dict, Path, bytes]:
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("项目 ID 无效")
        for project_file in self.root.glob("*/project.json"):
            if project_file.parent.name.startswith("."):
                continue
            try:
                project, data = read_project(project_file)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if project.get("projectId") == project_id:
                return project, project_file, data
        raise FileNotFoundError("项目不存在")

    def list_projects(self) -> list[dict]:
        result: list[dict] = []
        for project_file in self.root.glob("*/project.json"):
            if project_file.parent.name.startswith("."):
                continue
            try:
                project, data = read_project(project_file)
                stat = project_file.stat()
                result.append({"project": project, "fileSize": stat.st_size, "fileLastModified": int(stat.st_mtime_ns // 1_000_000), "hash": digest(data)})
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda item: (item["project"].get("createdAt", ""), item["project"].get("projectId", "")))

    def save_project(self, directory_name: str, requested: dict, operation_id: str | None = None) -> dict:
        with self.save_lock:
            operation_id = operation_id or str(uuid.uuid4())
            if not PROJECT_ID.fullmatch(operation_id):
                raise ValueError("操作 ID 无效")
            run_dir = create_run_directory(self.log_dir / "operations") if self.log_dir else None
            workflow = WorkflowCheckpoint(SAVE_TRANSITIONS, run_dir)
            try:
                result = self._save_project(directory_name, requested, operation_id, workflow)
                workflow.move("committed", operationId=operation_id, revision=result["project"]["revision"])
                return result
            except RevisionConflict as error:
                workflow.move("conflict", operationId=operation_id, **error.details)
                raise
            except Exception as error:
                workflow.move("failed", operationId=operation_id, errorCode=str(error))
                raise

    def _save_project(self, directory_name: str, requested: dict, operation_id: str, workflow: WorkflowCheckpoint) -> dict:
        if not isinstance(requested, dict) or not PROJECT_ID.fullmatch(str(requested.get("projectId", ""))):
            raise ValueError("项目 JSON 无效")
        expected_revision = requested.get("revision", 0)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("项目修订信息无效")
        if not isinstance(requested.get("rectangles"), list):
            raise ValueError("项目矩形数据无效")
        workflow.move("validating_revision", operationId=operation_id,
                      expectedRevision=expected_revision, directoryName=directory_name,
                      serviceWorkspace=str(self.root), servicePid=os.getpid())
        fingerprint = digest(json_bytes({"directoryName": directory_name, "project": requested}))
        if operation_id in self.operations:
            previous_fingerprint, result = self.operations[operation_id]
            if fingerprint != previous_fingerprint:
                raise ValueError("OPERATION_ID_REUSED")
            return result
        path = self.project_path(directory_name)
        current = None
        if path.exists():
            current, _ = read_project(path)
            if current.get("projectId") != requested.get("projectId"):
                raise ValueError("项目目录已被其他项目占用")
        actual_revision = int(current.get("revision") or 0) if current else 0
        if expected_revision != actual_revision:
            raise RevisionConflict({"expectedRevision": expected_revision,
                "actualRevision": actual_revision, "directoryName": directory_name,
                "serviceWorkspace": str(self.root), "servicePid": os.getpid(),
                "requestSource": "native_service", "conflictStage": "validating_revision",
                "lastWriterId": current.get("lastWriterId") if current else None,
                "currentHash": digest(path.read_bytes()) if current else None})
        saved = dict(requested)
        saved["directoryName"] = directory_name
        saved["revision"] = actual_revision + 1
        saved.setdefault("rectangles", [])
        file_hash, file_size, file_last_modified, commit_id = atomic_json_write(path, saved, operation_id, workflow)
        verified, verified_bytes = read_project(path)
        if verified != saved:
            raise IOError("原生文件校验失败")
        result = {"project": verified, "hash": file_hash, "fileSize": file_size, "fileLastModified": file_last_modified, "verifiedHash": digest(verified_bytes), "operationId": commit_id}
        self.operations[operation_id] = (fingerprint, result)
        if len(self.operations) > 256:
            self.operations.popitem(last=False)
        return result


class Handler(BaseHTTPRequestHandler):
    service: WorkspaceService

    def log_message(self, format: str, *args: object) -> None:
        print(f"{iso_timestamp()} [workspace-service] {format % args}", flush=True)

    def send_json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if status != 204:
            self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        self.send_json(204, {})

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10 * 1024 * 1024:
            raise ValueError("请求过大")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求格式无效")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self.send_json(200, self.service.health())
                return
            if path == "/api/projects":
                self.send_json(200, self.service.list_projects())
                return
            if path.startswith("/api/projects/"):
                project_id = unquote(path.rsplit("/", 1)[-1])
                project, file_path, data = self.service.find_project(project_id)
                stat = file_path.stat()
                self.send_json(200, {"project": project, "hash": digest(data), "fileSize": stat.st_size, "fileLastModified": int(stat.st_mtime_ns // 1_000_000)})
                return
            self.send_json(404, {"error": "NOT_FOUND"})
        except FileNotFoundError:
            self.send_json(404, {"error": "PROJECT_NOT_FOUND"})
        except Exception as error:
            self.send_json(400, {"error": str(error)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path != "/api/projects/save":
                self.send_json(404, {"error": "NOT_FOUND"})
                return
            request = self.body()
            result = self.service.save_project(str(request.get("directoryName", "")), request.get("project"), str(request.get("operationId") or "") or None)
            self.send_json(200, result)
        except RevisionConflict as error:
            self.send_json(409, {"error": str(error), "operationId": request.get("operationId"), **error.details})
        except RuntimeError as error:
            self.send_json(409, {"error": str(error)})
        except Exception as error:
            self.send_json(400, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5174)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--instance-id")
    args = parser.parse_args()
    log_dir = args.log_dir or create_run_directory(ROOT / "logs" / "背图工具" / "runs")
    Handler.service = WorkspaceService(Path(args.workspace), log_dir, args.instance_id)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[workspace-service] listening on http://{args.host}:{args.port} workspace={Handler.service.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
