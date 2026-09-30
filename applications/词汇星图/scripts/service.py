"""Loopback API for the local English dictionary application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from utils.scripts.dictionary_store import ConflictError, DictionaryStore, normalize_lemma
from utils.scripts.structured_io import write_text_atomic
from utils.scripts.timestamp import unique_filename_timestamp

RUN_ID = re.compile(r"^\d{8}T\d{6}(?:_\d+)?$")


class RunManager:
    def __init__(self, root: Path):
        self.root = root
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.skill = root / "skills" / "build-word-entry" / "scripts" / "cli.py"
        self.python = root / "runtime" / ".venv" / "Scripts" / "python.exe"

    def existing(self, lemma: str) -> str | None:
        run_root = self.root / "logs" / "build-word-entry" / "runs"
        for directory in sorted(run_root.glob("*"), reverse=True):
            request, state = directory / "request.json", directory / "state.json"
            if not request.is_file() or not state.is_file():
                continue
            try:
                req = json.loads(request.read_text(encoding="utf-8"))
                status = json.loads(state.read_text(encoding="utf-8"))["status"]
                if req.get("kind") == "word" and normalize_lemma(req.get("word", "")) == lemma and status != "completed":
                    return directory.name
            except (ValueError, KeyError):
                continue
        return None

    def _invoke(self, job_id: str, args: list[str]) -> None:
        try:
            process = subprocess.run([str(self.python), str(self.skill), *args, "--root", str(self.root)],
                                     cwd=self.root, capture_output=True, text=True, timeout=900)
            lines = [line for line in process.stdout.splitlines() if line.strip().startswith("{")]
            payload = json.loads(lines[-1]) if lines else {}
            run_id = payload.get("run_id") or self.jobs[job_id].get("runId")
            error = payload.get("error") or process.stderr[-500:] or None
            valid = process.returncode in (0, 3) and bool(run_id)
            if not run_id:
                error = error or "状态机未返回运行 ID"
            if process.returncode == 0 and run_id:
                verified = subprocess.run([str(self.python), str(self.skill), "verify", "--root", str(self.root),
                                           "--run-id", run_id], cwd=self.root, capture_output=True, text=True, timeout=90)
                if verified.returncode:
                    valid = False
                    error = verified.stderr[-500:] or "词条校验失败"
            with self.lock:
                self.jobs[job_id].update(status=("completed" if process.returncode == 0 and valid else
                                                 "paused" if process.returncode == 3 and valid else "error"),
                                         runId=run_id, skillStatus=payload.get("status"), error=error)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            with self.lock:
                self.jobs[job_id].update(status="error", error=str(exc))

    def start(self, lemma: str) -> dict:
        key = normalize_lemma(lemma)
        if not key or len(key) > 100:
            raise ValueError("单词无效")
        with self.lock:
            for job in self.jobs.values():
                if job["lemma"] == key and job["status"] == "running":
                    return dict(job)
            existing = self.existing(key)
            if existing:
                return {"jobId": None, "runId": existing, "status": "existing", "lemma": key}
            job_id = uuid.uuid4().hex
            job = {"jobId": job_id, "runId": None, "status": "running", "lemma": key}
            self.jobs[job_id] = job
            threading.Thread(target=self._invoke, args=(job_id, ["start-word", "--word", key]), daemon=True).start()
            return dict(job)

    def job(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise FileNotFoundError(job_id)
            return dict(self.jobs[job_id])

    def status(self, run_id: str) -> dict:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("runId 无效")
        process = subprocess.run([str(self.python), str(self.skill), "status", "--root", str(self.root),
                                  "--run-id", run_id], cwd=self.root, capture_output=True, text=True, timeout=20)
        if process.returncode:
            raise ValueError(process.stderr[-500:] or "无法读取运行状态")
        return json.loads(process.stdout)

    def resume(self, run_id: str, decision: dict | None = None) -> dict:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("runId 无效")
        with self.lock:
            for job in self.jobs.values():
                if job.get("runId") == run_id and job["status"] == "running":
                    return dict(job)
        args = ["resume", "--run-id", run_id]
        if decision is not None:
            run_dir = self.root / "logs" / "build-word-entry" / "runs" / run_id
            if not run_dir.is_dir():
                raise FileNotFoundError(run_id)
            existing = [file.stem.removeprefix("app-response_") for file in run_dir.glob("app-response_*.json")]
            path = run_dir / f"app-response_{unique_filename_timestamp(existing)}.json"
            write_text_atomic(path, json.dumps(decision, ensure_ascii=False, indent=2) + "\n")
            args += ["--input", str(path)]
        with self.lock:
            for job in self.jobs.values():
                if job.get("runId") == run_id and job["status"] == "running":
                    return dict(job)
            job_id = uuid.uuid4().hex
            job = {"jobId": job_id, "runId": run_id, "status": "running", "lemma": ""}
            self.jobs[job_id] = job
        threading.Thread(target=self._invoke, args=(job_id, args), daemon=True).start()
        return dict(job)


def playable_audio(data: bytes, content_type: str) -> bool:
    kind = content_type.lower().split(";")[0].strip()
    if kind not in ("audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "application/octet-stream"):
        return False
    return data.startswith((b"ID3", b"RIFF", b"OggS")) or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def fetch_audio(lemma: str, variety: str) -> tuple[bytes, str]:
    if variety not in ("uk", "us"):
        raise ValueError("无效口音")
    url = f"https://dict.youdao.com/dictvoice?audio={quote(lemma, safe='')}&type={'1' if variety == 'uk' else '2'}"
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as response:
        data = response.read(2_000_001)
        kind = response.headers.get("Content-Type", "")
    if len(data) > 2_000_000 or not playable_audio(data, kind):
        raise FileNotFoundError("当前口音暂无可播放音频")
    return data, kind


def cached_audio(store: DictionaryStore, lemma: str, variety: str) -> tuple[bytes, str]:
    if variety not in ("uk", "us"):
        raise ValueError("无效口音")
    directory = store.data / "cache" / "audio"
    key = hashlib.sha256(f"{normalize_lemma(lemma)}:{variety}".encode("utf-8")).hexdigest()
    formats = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}
    for extension, content_type in formats.items():
        path = directory / f"{key}.{extension}"
        if path.is_file():
            data = path.read_bytes()
            if playable_audio(data, content_type):
                return data, content_type
    data, content_type = fetch_audio(lemma, variety)
    extension = "wav" if data.startswith(b"RIFF") else "ogg" if data.startswith(b"OggS") else "mp3"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, directory / f"{key}.{extension}")
    finally:
        temporary_path.unlink(missing_ok=True)
    return data, formats[extension]


def make_handler(store: DictionaryStore, runs: RunManager):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, value: dict | list, content_type: str = "application/json; charset=utf-8") -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 2_000_000:
                raise ValueError("请求大小无效")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("请求必须为 JSON 对象")
            return value

        def _dispatch(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin and not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d+", origin):
                self._send(403, {"error": "不允许的请求来源"})
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            body = self._body() if self.command in ("PUT", "POST", "PATCH") else None
            if self.command == "GET" and path == "/health":
                result = {"status": "ok", "entryCount": len(store.entries())}
            elif self.command == "GET" and path == "/api/bootstrap":
                result = store.bootstrap()
            elif self.command == "GET" and path == "/api/projects":
                result = store.projects()
            elif self.command == "POST" and path == "/api/projects":
                result = store.create_project(body["name"], body.get("content", ""), body.get("format", "text"))
            elif self.command == "PUT" and path.startswith("/api/projects/"):
                result = store.rename_project(path.split("/")[-1], body["name"], body["expectedRevision"])
            elif self.command == "GET" and path == "/api/dictionary/search":
                result = store.search(query.get("q", [""])[0])
            elif self.command == "GET" and path.startswith("/api/projects/") and path.endswith("/learning"):
                result = store.state("learning", path.split("/")[3])
            elif self.command == "GET" and path == "/api/words":
                result = store.word_summaries(query.get("stage", ["all"])[0])
            elif self.command == "GET" and path.startswith("/api/words/"):
                result = store.word_page(path.split("/")[-1], query.get("stage", ["all"])[0])
            elif self.command == "GET" and path == "/api/candidates":
                lemma = query.get("lemma", [""])[0]
                result = store.candidate(lemma)
                result["existingRunId"] = runs.existing(normalize_lemma(lemma))
            elif self.command == "GET" and path == "/api/graph":
                visible = {key: query.get(key, ["1"])[0] == "1" for key in
                           ("family", "synonym", "near_synonym", "antonym", "spelling_similar")}
                result = store.graph(stage_id=query.get("stage", ["all"])[0],
                                     selected=query.get("selected", [None])[0], visible=visible,
                                     show_others=query.get("others", ["0"])[0] == "1",
                                     search=query.get("search", [""])[0],
                                     show_outside=query.get("outside", ["0"])[0] == "1",
                                     project_id=query.get("project", [None])[0])
            elif self.command == "POST" and path.startswith("/api/stages/") and path.endswith("/words"):
                stage_id = path.split("/")[3]
                result = store.add_to_stage(stage_id, body["wordId"], body["expectedRevision"])
            elif self.command == "PUT" and path == "/api/state/ui":
                result = store.save_state("ui", body["state"], body["expectedRevision"])
            elif self.command == "PATCH" and path.startswith("/api/learning/"):
                result = store.patch_learning(path.split("/")[-1], body["level"], body["expectedRevision"], body.get("projectId"))
            elif self.command == "POST" and path.startswith("/api/favorites/"):
                parts = path.split("/")
                word_id = parts[3]
                result = (store.opened(word_id, body["expectedRevision"]) if len(parts) == 5 and parts[4] == "opened" else
                          store.patch_favorite(word_id, body["favorited"], body["expectedRevision"]))
            elif self.command == "POST" and path == "/api/runs":
                result = runs.start(body["lemma"])
            elif self.command == "GET" and path.startswith("/api/jobs/"):
                result = runs.job(path.split("/")[-1])
            elif path.startswith("/api/runs/"):
                parts = path.split("/")
                result = runs.status(parts[3]) if self.command == "GET" and len(parts) == 4 else \
                    runs.resume(parts[3], body.get("decision")) if self.command == "POST" and len(parts) == 5 and parts[4] == "resume" else None
                if result is None:
                    raise FileNotFoundError(path)
            elif self.command == "GET" and path.startswith("/api/audio/"):
                parts = path.split("/")
                entry = store.entry(parts[3])
                data, content_type = cached_audio(store, entry["lemma"], parts[4])
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                check = query.get("check", ["0"])[0] == "1"
                self.send_header("Content-Length", str(0 if check else len(data)))
                self.send_header("Cache-Control", "private, max-age=600")
                self.end_headers()
                if not check:
                    self.wfile.write(data)
                return
            else:
                raise FileNotFoundError(path)
            self._send(200, result)

        def do_GET(self) -> None:
            self._safe_dispatch()

        def do_POST(self) -> None:
            self._safe_dispatch()

        def do_PATCH(self) -> None:
            self._safe_dispatch()

        def do_PUT(self) -> None:
            self._safe_dispatch()

        def _safe_dispatch(self) -> None:
            try:
                self._dispatch()
            except ConflictError as exc:
                self._send(409, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send(404, {"error": str(exc)})
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                self._send(500, {"error": f"服务错误：{exc}"})

    return Handler


def create_server(root: Path, port: int = 5186) -> ThreadingHTTPServer:
    store = DictionaryStore(root)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, RunManager(root)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "migrate"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=5186)
    args = parser.parse_args()
    store = DictionaryStore(args.root)
    if args.command == "migrate":
        print(json.dumps(store.migrate_entries(), ensure_ascii=False))
    else:
        server = create_server(args.root, args.port)
        print(f"词汇星图服务 http://127.0.0.1:{args.port}", flush=True)
        server.serve_forever()
