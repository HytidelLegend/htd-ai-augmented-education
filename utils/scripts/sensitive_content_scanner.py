"""Reusable deterministic scanner for secrets, personal data, and history reuse."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any, Iterable

import yaml


POLICY_PATH = Path(__file__).resolve().parents[1] / "references" / "sensitive-scan-policy.yaml"

PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "private_key": (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        "high",
    ),
    "api_key": (
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*['\"]?(?P<value>[A-Za-z0-9_\-/.+=]{8,})"
        ),
        "high",
    ),
    "password": (
        re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*['\"]?(?P<value>[^\s'\"]{8,})"),
        "high",
    ),
    "jwt": (
        re.compile(r"(?P<value>\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"),
        "high",
    ),
    "email": (
        re.compile(r"(?P<value>\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)", re.I),
        "medium",
    ),
    "cn_mobile": (
        re.compile(r"(?P<value>(?<!\d)1[3-9]\d{9}(?!\d))"),
        "medium",
    ),
    "cn_identity_number": (
        re.compile(r"(?P<value>(?<!\d)\d{17}[0-9Xx](?!\d))"),
        "high",
    ),
    "cookie": (
        re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]\s*['\"]?(?P<value>[^\s'\"]{12,})"),
        "high",
    ),
    "bearer_token": (
        re.compile(r"(?i)\b(?:authorization|x-api-key)\s*[:=]\s*(?:bearer\s+)?['\"]?(?P<value>[A-Za-z0-9_\-/.+=]{12,})"),
        "high",
    ),
    "connection_string": (
        re.compile(r"(?i)(?P<value>(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"]+)"),
        "high",
    ),
}


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("敏感扫描策略必须是 YAML 对象")
    return payload


def content_digest(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _is_synthetic(value: str, policy: dict[str, Any]) -> bool:
    folded = value.casefold()
    return any(str(marker).casefold() in folded for marker in policy.get("synthetic_markers", []))


def discover_skill_regression_files(root: Path, policy: dict[str, Any]) -> list[Path]:
    skills_root = root / ".agents" / "skills"
    if not skills_root.is_dir():
        return []
    directory_names = set(policy.get("regression_directory_names", []))
    file_markers = tuple(str(item).casefold() for item in policy.get("regression_file_markers", []))
    discovered: set[Path] = set()
    for path in skills_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative_parts = path.relative_to(skills_root).parts
        if any(part in directory_names for part in relative_parts[:-1]) or any(
            marker in path.name.casefold() for marker in file_markers
        ):
            discovered.add(path)
    return sorted(discovered, key=lambda item: item.as_posix().casefold())


def discover_history_files(root: Path, policy: dict[str, Any]) -> list[Path]:
    discovered: set[Path] = set()
    excluded_prefixes = tuple(
        (root / str(prefix)).resolve() for prefix in policy.get("history_exclude_prefixes", [])
    )
    inspectable_suffixes = set(policy.get("text_extensions", [])) | set(
        policy.get("binary_or_office_extensions", [])
    )
    for configured in policy.get("history_roots", []):
        history_root = root / str(configured)
        if history_root.is_dir():
            discovered.update(
                path for path in history_root.rglob("*")
                if path.is_file()
                and path.suffix.casefold() in inspectable_suffixes
                and not any(path.resolve().is_relative_to(prefix) for prefix in excluded_prefixes)
            )
    skills_root = root / ".agents" / "skills"
    if skills_root.is_dir():
        for directory_name in ("artifacts", "attempts", "requests"):
            for candidate in skills_root.glob(f"*/{directory_name}"):
                if candidate.is_dir():
                    discovered.update(
                        path for path in candidate.rglob("*")
                        if path.is_file() and path.suffix.casefold() in inspectable_suffixes
                    )
    return sorted(discovered, key=lambda item: item.as_posix().casefold())


def _text_for_scan(path: Path, policy: dict[str, Any]) -> tuple[str | None, str | None]:
    suffix = path.suffix.casefold()
    if suffix in set(policy.get("binary_or_office_extensions", [])):
        return None, "binary_or_office"
    if suffix not in set(policy.get("text_extensions", [])):
        return None, "unsupported_extension"
    if path.stat().st_size > int(policy.get("max_text_bytes", 5 * 1024 * 1024)):
        return None, "oversized_text"
    try:
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            return None, "binary_content"
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "non_utf8"


def scan_path(
    path: Path,
    root: Path,
    policy: dict[str, Any],
    source_scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    relative = _relative(path, root)
    text, skip_reason = _text_for_scan(path, policy)
    file_record = {
        "path": relative,
        "source_scope": source_scope,
        "size": path.stat().st_size,
        "scan_status": "scanned" if text is not None else "skipped",
        "skip_reason": skip_reason,
    }
    if text is None:
        if skip_reason in {"binary_or_office", "binary_content", "non_utf8", "oversized_text"}:
            return [file_record], [{
                "file": relative,
                "source_scope": source_scope,
                "risk_level": "medium",
                "category": "binary_or_office_confirmation" if skip_reason == "binary_or_office" else "content_not_inspected",
                "evidence": f"文件内容未检查：{skip_reason}",
                "recommendation": "confirm",
                "confidence": "high",
                "fingerprint": None,
                "synthetic_fixture": False,
            }]
        return [file_record], []

    findings: list[dict[str, Any]] = []
    newline_offsets = [index for index, character in enumerate(text) if character == "\n"]
    lines = text.splitlines()

    def line_number(offset: int) -> int:
        return bisect_right(newline_offsets, offset) + 1

    for category, (pattern, default_risk) in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.groupdict().get("value") or match.group(0)
            synthetic = _is_synthetic(value, policy)
            line = line_number(match.start())
            risk = "low" if synthetic else default_risk
            findings.append({
                "file": relative,
                "source_scope": source_scope,
                "risk_level": risk,
                "category": "synthetic_fixture" if synthetic else category,
                "matched_category": category,
                "evidence": f"第 {line} 行，内容指纹 {content_digest(value)[:12]}",
                "recommendation": "allow" if synthetic else ("block" if risk == "high" else "confirm"),
                "confidence": "high",
                "fingerprint": content_digest(value),
                "synthetic_fixture": synthetic,
            })

    semantic_markers = policy.get("semantic_markers", {})
    for category, markers in semantic_markers.items():
        for marker in markers:
            for match in re.finditer(re.escape(str(marker)), text):
                line = line_number(match.start())
                line_text = lines[line - 1] if lines else str(marker)
                findings.append({
                    "file": relative,
                    "source_scope": source_scope,
                    "risk_level": "medium",
                    "category": category,
                    "evidence": f"第 {line} 行包含语义标记“{marker}”，行指纹 {content_digest(line_text)[:12]}",
                    "recommendation": "confirm",
                    "confidence": "medium",
                    "fingerprint": content_digest(line_text),
                    "synthetic_fixture": False,
                })
    return [file_record], findings


def scan_files(
    paths: Iterable[Path],
    root: Path,
    policy: dict[str, Any],
    source_scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        file_records, file_findings = scan_path(path, root, policy, source_scope)
        records.extend(file_records)
        findings.extend(file_findings)
    return records, findings


def build_history_index(findings: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = {}
    for finding in findings:
        fingerprint = finding.get("fingerprint")
        if not fingerprint or finding.get("synthetic_fixture"):
            continue
        index.setdefault(str(fingerprint), []).append({
            "file": str(finding.get("file", "")),
            "category": str(finding.get("category", "")),
        })
    return index


def cross_scope_matches(
    skill_findings: Iterable[dict[str, Any]],
    history_index: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for finding in skill_findings:
        fingerprint = finding.get("fingerprint")
        if not fingerprint or fingerprint not in history_index:
            continue
        sources = history_index[fingerprint]
        matches.append({
            "file": finding["file"],
            "source_scope": "cross_scope",
            "risk_level": "high",
            "category": "historical_match",
            "evidence": f"与 {len(sources)} 个历史运行来源的脱敏指纹一致",
            "recommendation": "replace_or_confirm",
            "confidence": "high",
            "fingerprint": fingerprint,
            "historical_sources": sources,
            "synthetic_fixture": False,
        })
    return matches
