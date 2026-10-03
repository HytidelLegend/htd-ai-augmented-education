"""Shared workspace layout and input checks for transcript and TTS workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .file_transaction import file_sha256, project_lock
from .structured_io import read_json
from .run_state import write_json
from .timestamp import iso_timestamp, unique_filename_timestamp


WORKSPACE_PREFIX = "tts_"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TtsWorkspaceError(ValueError):
    """Raised when a shared transcript/TTS workspace is invalid or ambiguous."""


@dataclass(frozen=True)
class TtsWorkspace:
    root: Path
    run_id: str
    layout_profile: str = "standalone"

    @property
    def inputs_dir(self) -> Path:
        if self.layout_profile == "education":
            return self.logs_dir / "inputs"
        return self.root / "inputs"

    @property
    def logs_dir(self) -> Path:
        if self.layout_profile == "education":
            project = self.root.parents[3]
            return project / "logs" / self.root.parents[1].name / "runs" / self.run_id
        return self.root

    @property
    def transcript_dir(self) -> Path:
        if self.layout_profile == "education" and self.pipeline_manifest.is_file():
            reference = read_json(self.pipeline_manifest)["components"]["transcript"].get("producer_dir")
            if reference:
                return Path(reference) / "transcript"
        return self.root / "transcript"

    @property
    def speech_dir(self) -> Path:
        return self.root / ("tts" if self.layout_profile == "slides-video" else "speech")

    @property
    def pipeline_manifest(self) -> Path:
        return self.root / "pipeline-manifest.json"


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalized_text_sha256(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def _workspace_run_ids(output_root: Path, root: Path) -> set[str]:
    existing = {
        path.name.removeprefix(WORKSPACE_PREFIX)
        for path in output_root.glob("*")
        if path.is_dir()
    }
    for workflow in ("convert-copy-to-transcript", "run-text-to-speech"):
        log_root = root / "logs" / workflow / "runs"
        if log_root.is_dir():
            existing.update(path.name for path in log_root.iterdir() if path.is_dir())
    return existing


def _create_layout(workspace: TtsWorkspace) -> None:
    for path in (
        workspace.inputs_dir,
        workspace.transcript_dir / "generated",
        workspace.transcript_dir / "approved",
        workspace.speech_dir / "generated",
        workspace.speech_dir / "batches",
    ):
        path.mkdir(parents=True, exist_ok=True)


def _validate_pipeline_manifest(value: dict[str, Any]) -> None:
    schema = read_json(PROJECT_ROOT / "utils" / "references" / "tts-pipeline-manifest-v1.schema.json")
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise TtsWorkspaceError(
            "pipeline manifest Schema 校验失败：" + "；".join(error.message for error in errors)
        )


def create_workspace(
    root: Path,
    output_root: Path | None = None,
    *,
    workspace_dir: Path | None = None,
    run_id: str | None = None,
    layout_profile: str = "standalone",
    workflow: str = "convert-copy-to-transcript",
) -> TtsWorkspace:
    root = root.resolve()
    if output_root is not None and workspace_dir is not None:
        raise TtsWorkspaceError("output_root 与 workspace_dir 不能同时使用")
    if layout_profile not in {"standalone", "slides-video", "education"}:
        raise TtsWorkspaceError(f"未知 workspace 布局：{layout_profile}")
    if workspace_dir is not None and not run_id:
        raise TtsWorkspaceError("精确 workspace 必须提供 run_id")
    if workspace_dir is None:
        layout_profile = "education"
    parent = (output_root or root / "outputs" / workflow / "runs").resolve()
    expected_parent = (root / "outputs" / workflow / "runs").resolve()
    if parent != expected_parent:
        raise TtsWorkspaceError("output_root 必须是 outputs/<skill>/runs")
    if workspace_dir is not None:
        if workspace_dir.resolve().parent != expected_parent:
            raise TtsWorkspaceError("workspace_dir 必须位于 outputs/<skill>/runs")
        layout_profile = "education"
    if workspace_dir is None:
        parent.mkdir(parents=True, exist_ok=True)
    else:
        workspace_dir = workspace_dir.resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)
    lock_path = root / "logs" / "tts-workspace" / "initialize.lock"
    with project_lock(lock_path, "tts-workspace:initialize"):
        resolved_run_id = run_id or unique_filename_timestamp(_workspace_run_ids(parent, root))
        workspace = TtsWorkspace(
            workspace_dir or parent / resolved_run_id,
            resolved_run_id,
            layout_profile,
        )
        if workspace.pipeline_manifest.exists():
            raise TtsWorkspaceError(f"共享输出目录已存在：{workspace.root}")
        _create_layout(workspace)
        now = iso_timestamp()
        manifest = {
                "schema_version": "2.0" if layout_profile == "education" else "1.0",
                "pipeline_run_id": resolved_run_id,
                "workspace_dir": str(workspace.root),
                "layout_profile": layout_profile,
                "created_at": now,
                "updated_at": now,
                "components": {
                    "transcript": {"status": "not_started", "manifest_path": None},
                    "speech": {"status": "not_started", "manifest_path": None},
                },
            }
        _validate_pipeline_manifest(manifest)
        write_json(workspace.pipeline_manifest, manifest)
        return workspace


def open_workspace(path: Path) -> TtsWorkspace:
    workspace_path = path.resolve()
    if not workspace_path.is_dir():
        raise TtsWorkspaceError(f"继续任务时 workspace 必须是已有目录：{workspace_path}")
    manifest_path = workspace_path / "pipeline-manifest.json"
    if not manifest_path.is_file():
        raise TtsWorkspaceError(f"共享输出目录缺少 pipeline-manifest.json：{workspace_path}")
    manifest = read_json(manifest_path)
    _validate_pipeline_manifest(manifest)
    run_id = str(manifest.get("pipeline_run_id", ""))
    if workspace_path.name.startswith(WORKSPACE_PREFIX) and workspace_path.name.removeprefix(WORKSPACE_PREFIX) != run_id:
        raise TtsWorkspaceError("pipeline manifest 的 run ID 与目录名不一致")
    workspace = TtsWorkspace(workspace_path, run_id, str(manifest.get("layout_profile", "standalone")))
    return workspace


def update_component(
    workspace: TtsWorkspace,
    component: str,
    *,
    status: str,
    manifest_path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if component not in {"transcript", "speech"}:
        raise TtsWorkspaceError(f"未知组件：{component}")
    lock_path = workspace.logs_dir / ".pipeline.lock"
    with project_lock(lock_path, f"tts-workspace:{workspace.run_id}"):
        manifest = read_json(workspace.pipeline_manifest)
        entry: dict[str, Any] = {
            "status": status,
            "manifest_path": str(manifest_path.resolve()) if manifest_path else None,
        }
        if details:
            entry.update(details)
        manifest["components"][component] = entry
        manifest["updated_at"] = iso_timestamp()
        _validate_pipeline_manifest(manifest)
        write_json(workspace.pipeline_manifest, manifest)
        return manifest


def input_metadata(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TtsWorkspaceError("输入文件必须是 UTF-8 编码") from exc
    return {
        "path": str(path.resolve()),
        "file_sha256": file_sha256(path),
        "normalized_text_sha256": normalized_text_sha256(text),
    }


def inputs_match(candidate_text: str, stored_input: Path) -> tuple[bool, dict[str, str]]:
    stored = input_metadata(stored_input)
    candidate_hash = normalized_text_sha256(candidate_text)
    return candidate_hash == stored["normalized_text_sha256"], {
        "candidate_normalized_text_sha256": candidate_hash,
        "stored_normalized_text_sha256": str(stored["normalized_text_sha256"]),
        "stored_file_sha256": str(stored["file_sha256"]),
    }
