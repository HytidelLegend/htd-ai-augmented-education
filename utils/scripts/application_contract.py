"""Deterministic checks for application documentation contracts."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _add(findings: list[dict[str, Any]], kind: str, path: str, suggestion: str,
         current: Any = None, expected: Any = None, evidence: list[str] | None = None) -> None:
    findings.append({
        "kind": kind, "path": path, "section": "applications",
        "current": current, "expected": expected, "evidence": evidence or [],
        "suggestion": suggestion, "confidence": "deterministic", "status": "new",
    })


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_phase_states(text: str) -> list[str]:
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.DOTALL)
    match = re.search(r"type\s+Phase\s*=\s*(.*?);", text, re.DOTALL)
    if not match:
        return []
    return [value for _, value in re.findall(r"(['\"])([A-Za-z_][A-Za-z0-9_]*)\1", match.group(1))]


def _extract_project_fields(text: str) -> list[str]:
    interface = re.search(r"interface\s+Project\s*\{(.*?)\n\}", text, re.DOTALL)
    if interface:
        return re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\??\s*:", interface.group(1), re.MULTILINE)
    match = re.search(r'"schemaVersion"\s*:\s*1\s*,(.*?)\n\s*\}', text, re.DOTALL)
    if not match:
        return []
    return re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:', match.group(0))


def _doc_path_tokens(text: str, app_id: str) -> set[str]:
    """Compare the application's data root, excluding skill and migration paths."""
    root = f"outputs/{app_id}/"
    return {root} if root in text else set()


def audit_applications(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Audit applications described by per-application application-audit.json files."""
    findings: list[dict[str, Any]] = []
    snapshots: dict[str, Any] = {}
    base = root / "applications"
    if not base.is_dir():
        return findings, snapshots
    for app_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest_path = app_dir / "application-audit.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        app_id = str(manifest.get("application_id") or app_dir.name)
        package_path = app_dir / "package.json"
        package = _read_json(package_path)
        docs = manifest.get("documents") if isinstance(manifest.get("documents"), dict) else {}
        readme_path = root / str(docs.get("readme", ""))
        prd_path = root / str(docs.get("prd", ""))
        readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
        prd = prd_path.read_text(encoding="utf-8") if prd_path.is_file() else ""
        if not readme_path.is_file():
            _add(findings, "application_document_missing", _rel(root, manifest_path), "补充应用 README 路径", None, str(docs.get("readme")), [_rel(root, manifest_path)])
        if not prd_path.is_file():
            _add(findings, "application_document_missing", _rel(root, manifest_path), "补充应用 PRD 路径", None, str(docs.get("prd")), [_rel(root, manifest_path)])

        version_cfg = manifest.get("version") if isinstance(manifest.get("version"), dict) else {}
        version = package.get(str(version_cfg.get("field", "version")))
        documented_version = manifest.get("documented_version")
        if documented_version is not None and version != documented_version:
            _add(findings, "application_version_mismatch", _rel(root, package_path), "使应用文档声明版本与 package.json 一致", version, documented_version, [_rel(root, package_path), _rel(root, manifest_path)])

        source_cfg = manifest.get("implementation") if isinstance(manifest.get("implementation"), dict) else {}
        state_path = root / str(source_cfg.get("state_machine", ""))
        model_path = root / str(source_cfg.get("data_model", ""))
        states = _extract_phase_states(state_path.read_text(encoding="utf-8")) if state_path.is_file() else []
        model_fields = _extract_project_fields(model_path.read_text(encoding="utf-8")) if model_path.is_file() else []
        documented_states = {
            state for state in states
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(state) + r"(?![A-Za-z0-9_])", prd)
        }
        missing_states = sorted(set(states) - documented_states)
        if missing_states:
            _add(findings, "application_state_machine_mismatch", _rel(root, prd_path), "补充 PRD 中缺失的实现状态", missing_states, states, [_rel(root, state_path), _rel(root, prd_path)])

        documented_fields = set()
        for block in re.findall(r"```json\s*\n(.*?)\n```", prd, re.DOTALL | re.IGNORECASE):
            try:
                value = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schemaVersion") == 1:
                documented_fields = set(value)
                break
        if not documented_fields:
            documented_fields = set(_extract_project_fields(prd))
        missing_fields = sorted(set(model_fields) - documented_fields)
        if missing_fields:
            _add(findings, "application_data_contract_mismatch", _rel(root, prd_path), "补充 PRD JSON 契约中缺失的项目字段", missing_fields, model_fields, [_rel(root, model_path), _rel(root, prd_path)])

        readme_paths = _doc_path_tokens(readme, app_id)
        prd_paths = _doc_path_tokens(prd, app_id)
        if readme_paths and prd_paths and readme_paths != prd_paths:
            _add(findings, "application_readme_prd_mismatch", _rel(root, readme_path), "统一 README 与 PRD 的项目目录路径说明", sorted(readme_paths), sorted(prd_paths), [_rel(root, readme_path), _rel(root, prd_path)])

        snapshots[app_id] = {
            "manifest": _rel(root, manifest_path),
            "version": version,
            "documents": {"readme": _rel(root, readme_path), "prd": _rel(root, prd_path)},
            "states": states,
            "project_fields": model_fields,
            "readme_paths": sorted(readme_paths),
            "prd_paths": sorted(prd_paths),
        }
    return findings, snapshots
