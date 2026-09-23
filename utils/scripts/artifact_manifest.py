"""Schema-validated artifact manifests with stable project-relative paths and hashes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

try:
    from .file_transaction import file_sha256
    from .structured_io import read_json, write_json
    from .timestamp import iso_timestamp
except ImportError:
    from file_transaction import file_sha256
    from structured_io import read_json, write_json
    from timestamp import iso_timestamp


class ArtifactManifestError(ValueError):
    """Raised when a manifest or one of its artifacts is invalid."""


def relative_path(root: Path, path: Path) -> str:
    """Return a project-relative POSIX path and reject paths outside the project."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ArtifactManifestError(f"产物不在项目目录内：{path}") from exc


def artifact_entry(
    root: Path,
    path: Path,
    *,
    role: str,
    schema_version: str | int | None = None,
    sources: Iterable[str] = (),
) -> dict[str, Any]:
    """Describe one existing file without mutating it."""
    if not path.is_file():
        raise ArtifactManifestError(f"产物文件不存在：{path}")
    entry: dict[str, Any] = {
        "role": role,
        "path": relative_path(root, path),
        "sha256": file_sha256(path),
        "size_bytes": path.stat().st_size,
        "sources": list(sources),
    }
    if schema_version is not None:
        entry["schema_version"] = schema_version
    return entry


class ArtifactManifestStore:
    """Create, update, and verify one workflow artifact manifest."""

    def __init__(self, *, root: Path, path: Path, schema_path: Path | None = None) -> None:
        self.root = root.resolve()
        self.path = path.resolve()
        default_schema = self.root / "utils" / "references" / "artifact-manifest-v1.schema.json"
        self.schema_path = schema_path or default_schema
        self.validator = Draft202012Validator(read_json(self.schema_path))

    def validate(self, manifest: dict[str, Any]) -> None:
        errors = sorted(self.validator.iter_errors(manifest), key=lambda item: list(item.path))
        if errors:
            raise ArtifactManifestError(
                "产物清单 Schema 校验失败：" + "；".join(error.message for error in errors)
            )
        paths = [item["path"] for item in manifest["artifacts"]]
        roles = [item["role"] for item in manifest["artifacts"]]
        if len(paths) != len(set(paths)):
            raise ArtifactManifestError("产物清单中存在重复路径")
        if len(roles) != len(set(roles)):
            raise ArtifactManifestError("产物清单中存在重复角色")

    def create(self, *, workflow: str, run_id: str, status: str, lineage: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.path.exists():
            raise ArtifactManifestError(f"产物清单已存在，不覆盖：{self.path}")
        now = iso_timestamp()
        manifest = {
            "schema_version": "1.0",
            "workflow": workflow,
            "run_id": run_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "lineage": lineage or {},
            "artifacts": [],
        }
        return self.save(manifest)

    def load(self) -> dict[str, Any]:
        value = read_json(self.path)
        if not isinstance(value, dict):
            raise ArtifactManifestError("产物清单必须是 JSON 对象")
        self.validate(value)
        return value

    def save(self, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["updated_at"] = iso_timestamp()
        self.validate(manifest)
        write_json(self.path, manifest)
        return manifest

    def upsert(self, manifest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        manifest["artifacts"] = [
            item for item in manifest["artifacts"] if item["role"] != entry["role"]
        ] + [entry]
        return self.save(manifest)

    def set_status(self, manifest: dict[str, Any], status: str) -> dict[str, Any]:
        manifest["status"] = status
        return self.save(manifest)

    def verify_files(self, manifest: dict[str, Any]) -> None:
        self.validate(manifest)
        errors: list[str] = []
        for item in manifest["artifacts"]:
            path = self.root / item["path"]
            if not path.is_file():
                errors.append(f"缺少 {item['role']}：{item['path']}")
            elif file_sha256(path) != item["sha256"]:
                errors.append(f"指纹变化 {item['role']}：{item['path']}")
        if errors:
            raise ArtifactManifestError("；".join(errors))
