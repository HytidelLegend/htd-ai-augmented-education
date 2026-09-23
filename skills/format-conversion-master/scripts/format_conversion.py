"""Recoverable EPUB to PDF or directly parsed Markdown conversion."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.scripts.artifact_manifest import ArtifactManifestStore, artifact_entry
from utils.scripts.file_publish import publish_file_without_overwrite
from utils.scripts.file_transaction import file_sha256
from utils.scripts.structured_io import read_json, validate_json_schema, write_json
from utils.scripts.subprocess_runner import run_command
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_state import WorkflowDefinition, WorkflowStateStore
from utils.scripts.epub_package import EpubPackage, EpubPackageError
from utils.scripts.html_to_markdown import HtmlParseError, render_xhtml

WORKFLOW = "format-conversion-master"
SUPPORTED_CONVERSIONS = ({"source": "epub", "target": "pdf"}, {"source": "epub", "target": "md"})
REQUEST_SCHEMA = PROJECT_ROOT / "skills" / WORKFLOW / "references" / "request.schema.json"
DEFINITION = WorkflowDefinition.build(
    name=WORKFLOW,
    transitions={
        "prepared": {"validating_input", "paused_configuration", "paused_input"},
        "validating_input": {"staging_input", "paused_input", "paused_configuration"},
        "staging_input": {"planning", "reading_package", "paused_input"},
        "planning": {"converting", "paused_output_conflict", "paused_input"},
        "reading_package": {"resolving_spine", "paused_package"},
        "resolving_spine": {"extracting_content", "paused_package"},
        "extracting_content": {"rendering", "paused_parse", "paused_scanned_content"},
        "rendering": {"verifying", "paused_conversion"},
        "converting": {"verifying", "paused_conversion"},
        "verifying": {"publishing", "paused_verification"},
        "publishing": {"completed", "paused_output_conflict", "paused_verification"},
        "paused_configuration": {"prepared", "validating_input", "planning"},
        "paused_input": {"validating_input", "staging_input"},
        "paused_output_conflict": {"planning"},
        "paused_conversion": {"converting"},
        "paused_package": {"reading_package", "resolving_spine"},
        "paused_parse": {"extracting_content"},
        "paused_scanned_content": {"extracting_content"},
        "paused_verification": {"verifying"},
        "paused_error": {"prepared", "validating_input", "staging_input", "planning", "converting", "verifying", "publishing"},
    }
)


class ConversionError(RuntimeError):
    """A recoverable conversion failure."""


def _run_dir(root: Path, run_id: str) -> Path:
    return root / "logs" / WORKFLOW / "runs" / run_id


def _store(root: Path, run_id: str) -> WorkflowStateStore:
    return WorkflowStateStore(
        root=root,
        workflow=WORKFLOW,
        run_id=run_id,
        definition=DEFINITION,
        run_dir=_run_dir(root, run_id),
        schema_path=PROJECT_ROOT / "utils" / "references" / "workflow-state-v1.schema.json",
    )


def _paths(run_dir: Path, target_format: str = "pdf") -> dict[str, Path]:
    # Keep state and text logs under logs/, while formal conversion outputs go to outputs/.
    root = run_dir.parents[3]
    output_dir = root / "outputs" / WORKFLOW / "runs" / run_dir.name
    generated = output_dir / ("output.md" if target_format == "md" else "output.pdf")
    return {
        "request": run_dir / "request.json",
        "plan": run_dir / "plan.json",
        "manifest": run_dir / "manifest.json",
        "input": run_dir / "inputs" / "source.epub",
        "generated": generated,
        "assets": output_dir / "assets",
        "verification": run_dir / "qa" / "verification.json",
        "publication": run_dir / "publication.json",
    }


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _validate_epub(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ConversionError(f"输入文件不存在或为空：{path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" not in names or archive.read("mimetype") != b"application/epub+zip":
                raise ConversionError("文件不是有效 EPUB：mimetype 缺失或不正确")
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                (item.attrib.get("full-path") for item in container.iter() if item.tag.endswith("rootfile")),
                None,
            )
            if not rootfile or rootfile not in names:
                raise ConversionError("EPUB 缺少有效 OPF package")
            opf = ElementTree.fromstring(archive.read(rootfile))
            manifest = [item for item in opf.iter() if item.tag.endswith("item")]
            spine = [item for item in opf.iter() if item.tag.endswith("itemref")]
            if not spine:
                raise ConversionError("EPUB spine 为空")
            return {"opf": rootfile, "manifest_items": len(manifest), "spine_items": len(spine)}
    except zipfile.BadZipFile as exc:
        raise ConversionError("输入文件不是有效 ZIP/EPUB") from exc
    except KeyError as exc:
        raise ConversionError(f"EPUB 缺少必要文件：{exc}") from exc
    except ElementTree.ParseError as exc:
        raise ConversionError(f"EPUB XML 无法解析：{exc}") from exc


def _render_markdown(root: Path, run_dir: Path, source_sha256: str, paths: dict[str, Path]) -> dict[str, Any]:
    try:
        with EpubPackage(paths["input"]) as package:
            paths["assets"].mkdir(parents=True, exist_ok=True)
            resource_map: dict[str, str] = {}
            for item in package.manifest.values():
                href = item["href"]
                media = item.get("media_type", "")
                if media.startswith("image/"):
                    target = paths["assets"] / f"{file_sha256_bytes(package.read(package.resolve(href)))}{Path(href).suffix.lower()}"
                    target.write_bytes(package.read(package.resolve(href)))
                    resource_map[href] = f"assets/{target.name}"
                    resource_map[package.resolve(href)] = f"assets/{target.name}"
            chunks: list[str] = []
            totals = {"spine_items": len(package.spine), "processed_items": 0, "scanned_items": [], "heading_count": 0, "paragraph_count": 0, "table_count": 0, "footnote_count": 0, "toc_entry_count": 0, "link_count": 0, "image_count": 0}
            toc_lines: list[str] = []
            nav_path = package.nav_path()
            if nav_path:
                nav = package.read(nav_path).decode("utf-8")
                if nav_path.lower().endswith(".ncx"):
                    nav_root = ElementTree.fromstring(nav.encode("utf-8"))
                    for point in nav_root.iter():
                        if point.tag.endswith("navPoint"):
                            label = next((n.text.strip() for n in point.iter() if n.tag.endswith("text") and n.text), "")
                            content = next((n.get("src") for n in point.iter() if n.tag.endswith("content") and n.get("src")), "")
                            if label and content:
                                toc_lines.append(f"- [{label}]({content})")
                else:
                    for href, label in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", nav, flags=re.IGNORECASE | re.DOTALL):
                        clean = re.sub(r"<[^>]+>", "", label).strip()
                        if clean:
                            toc_lines.append(f"- [{clean}]({href})")
                totals["toc_entry_count"] = len(toc_lines)
            for item in package.spine:
                data = package.read(item.href)
                try:
                    ElementTree.fromstring(data)
                except ElementTree.ParseError as exc:
                    raise HtmlParseError(f"XHTML 无法解析：{item.href}：{exc}") from exc
                decoded = data.decode("utf-8", errors="ignore")
                text_probe = re.sub(r"<[^>]+>", "", decoded).strip()
                if not text_probe and re.search(r"<img\b", decoded, re.IGNORECASE):
                    totals["scanned_items"].append(item.href)
                    raise ConversionError(f"检测到疑似扫描内容，无法提取文本：{item.href}")
                rendered, stats = render_xhtml(data, base_path=item.href, resource_map=resource_map)
                chunks.append(rendered)
                totals["processed_items"] += 1
                for key in ("heading_count", "paragraph_count", "table_count", "footnote_count", "link_count", "image_count"):
                    totals[key] += stats.get(key, 0)
            title = package.metadata().get("title", "")
            header = f"---\nsource_format: epub\nsource_sha256: {source_sha256}\nconverter: format-conversion-master\n---\n\n"
            if title:
                header += f"# {title}\n\n"
            if toc_lines:
                header += "## 目录\n\n" + "\n".join(toc_lines) + "\n\n"
            paths["generated"].parent.mkdir(parents=True, exist_ok=True)
            paths["generated"].write_text(header + "\n".join(chunks), encoding="utf-8", newline="\n")
            return totals
    except (EpubPackageError, HtmlParseError, UnicodeDecodeError) as exc:
        raise ConversionError(str(exc)) from exc


def file_sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _check_calibre(run_dir: Path) -> dict[str, str]:
    executable = shutil.which("ebook-convert")
    log = run_dir / "attempts" / "calibre-version.log"
    if not executable:
        raise ConversionError(
            "找不到 ebook-convert。请从 https://www.calibre-ebook.com/download_windows64 安装 Calibre，"
            "将 C:\\Program Files\\Calibre2 加入 PATH，重新打开终端后执行 ebook-convert --version。"
        )
    result = run_command(["ebook-convert", "--version"], log_path=log, check=False, timeout_seconds=30)
    version_text = (result.stdout or result.stderr).strip()
    match = re.search(r"\d+(?:\.\d+)+", version_text)
    if result.returncode != 0 or not match:
        raise ConversionError(f"ebook-convert --version 检查失败：{version_text or '无版本输出'}")
    return {"path": str(Path(executable).resolve()), "version": match.group(0)}


def _verify_pdf(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ConversionError("PDF 产物不存在或为空")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
        raise ConversionError("PDF 结构校验失败：缺少有效 PDF 头或 EOF")
    pages = len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", data))
    if pages <= 0:
        raise ConversionError("PDF 结构校验失败：未发现页面")
    return {"size_bytes": len(data), "sha256": file_sha256(path), "page_count": pages}


def _verify_markdown(path: Path, extraction: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ConversionError("Markdown 产物不存在或为空")
    text = path.read_text(encoding="utf-8")
    if "<html" in text.lower() or "</body>" in text.lower():
        raise ConversionError("Markdown 验证失败：仍包含 HTML 文档壳")
    result = {"size_bytes": path.stat().st_size, "sha256": file_sha256(path), "line_count": len(text.splitlines())}
    result.update(extraction)
    return result


def _initial_state(run_id: str, now: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "workflow": WORKFLOW, "run_id": run_id, "status": "prepared",
        "current_stage": "prepared", "resume_stage": None, "current_object_id": None,
        "current_batch_id": None, "completed_steps": [], "pending_decisions": [], "error": None,
        "created_at": now, "updated_at": now, "last_heartbeat_at": now, "event_sequence": 0,
    }


def _write_manifest(root: Path, run_dir: Path, request: dict[str, Any], run_id: str) -> None:
    store = ArtifactManifestStore(
        root=root,
        path=_paths(run_dir)["manifest"],
        schema_path=PROJECT_ROOT / "utils" / "references" / "artifact-manifest-v1.schema.json",
    )
    manifest = store.create(workflow=WORKFLOW, run_id=run_id, status="running", lineage={"input_file": request["input_file"], "target_format": request["target_format"]})
    store.save(manifest)


def _manifest(root: Path, run_dir: Path) -> ArtifactManifestStore:
    return ArtifactManifestStore(
        root=root,
        path=_paths(run_dir)["manifest"],
        schema_path=PROJECT_ROOT / "utils" / "references" / "artifact-manifest-v1.schema.json",
    )


def _read_request(path: Path) -> dict[str, Any]:
    request = read_json(path)
    if not isinstance(request, dict):
        raise ConversionError("请求必须是 JSON 对象")
    validate_json_schema(request, REQUEST_SCHEMA)
    return request


def _advance(root: Path, state: dict[str, Any], *, output_override: str | None = None) -> dict[str, Any]:
    run_id = str(state["run_id"])
    run_dir = _run_dir(root, run_id)
    target_format = str(read_json(_paths(run_dir)["request"]).get("target_format", "pdf"))
    paths = _paths(run_dir, target_format)
    store = _store(root, run_id)
    request = _read_request(paths["request"])
    if output_override:
        request["output_file"] = output_override
        write_json(paths["request"], request)
    while state["status"] not in {"completed"} and not str(state["status"]).startswith("paused_"):
        try:
            if state["status"] == "prepared":
                if target_format == "pdf":
                    _check_calibre(run_dir)
                state = store.transition(state, "validating_input", stage="validating_input")
            elif state["status"] == "validating_input":
                source = _resolve(root, request["input_file"])
                epub = _validate_epub(source)
                state = store.transition(state, "staging_input", stage="staging_input", updates={"source_path": str(source), "source_sha256": file_sha256(source), "epub": epub})
            elif state["status"] == "staging_input":
                source = Path(state["source_path"])
                paths["input"].parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, paths["input"])
                if file_sha256(paths["input"]) != state["source_sha256"]:
                    raise ConversionError("输入副本哈希不一致")
                manifest_store = _manifest(root, run_dir)
                manifest = manifest_store.load()
                manifest_store.upsert(manifest, artifact_entry(root, paths["input"], role="staged_input", sources=[state["source_sha256"]]))
                next_stage = "planning" if target_format == "pdf" else "reading_package"
                state = store.transition(state, next_stage, stage=next_stage, completed_step="input_staged")
            elif state["status"] == "reading_package":
                try:
                    with EpubPackage(paths["input"]) as package:
                        info = {"opf": package.opf_path, "manifest_items": len(package.manifest), "spine_items": len(package.spine), "metadata": package.metadata()}
                    state = store.transition(state, "resolving_spine", stage="resolving_spine", updates={"package": info})
                except EpubPackageError as exc:
                    state = store.pause(state, status="paused_package", error_code="package_error", message=str(exc), resume_stage="reading_package")
            elif state["status"] == "resolving_spine":
                try:
                    with EpubPackage(paths["input"]) as package:
                        spine = [item.__dict__ for item in package.spine]
                    state = store.transition(state, "extracting_content", stage="extracting_content", updates={"spine": spine})
                except EpubPackageError as exc:
                    state = store.pause(state, status="paused_package", error_code="spine_error", message=str(exc), resume_stage="resolving_spine")
            elif state["status"] == "extracting_content":
                try:
                    totals = _render_markdown(root, run_dir, state["source_sha256"], paths)
                    if totals["scanned_items"]:
                        raise ConversionError("检测到疑似扫描内容，已暂停：" + ", ".join(totals["scanned_items"]))
                    state = store.transition(state, "rendering", stage="rendering", updates={"extraction": totals}, completed_step="content_extracted")
                except ConversionError as exc:
                    status = "paused_scanned_content" if "扫描" in str(exc) or "疑似扫描" in str(exc) else "paused_parse"
                    state = store.pause(state, status=status, error_code="content_extraction_error", message=str(exc), resume_stage="extracting_content")
            elif state["status"] == "rendering":
                manifest_store = _manifest(root, run_dir)
                manifest = manifest_store.load()
                manifest_store.upsert(manifest, artifact_entry(root, paths["generated"], role="generated_markdown", sources=[state["source_sha256"]]))
                output = _resolve(root, request.get("output_file") or str(paths["generated"]))
                if output != paths["generated"] and output.exists():
                    raise FileExistsError(f"输出目标已存在，拒绝覆盖：{output}")
                state = store.transition(state, "verifying", stage="verifying", completed_step="conversion_finished", updates={"output_path": str(output)})
            elif state["status"] == "planning":
                output = _resolve(root, request.get("output_file") or str(Path(state["source_path"]).with_suffix(".pdf")))
                if output.exists():
                    raise FileExistsError(f"输出目标已存在，拒绝覆盖：{output}")
                calibre = _check_calibre(run_dir)
                plan = {"input": str(paths["input"]), "output": str(output), "paper_size": "a4", "calibre": calibre, "command": ["ebook-convert", str(paths["input"]), str(paths["generated"]), "--paper-size", "a4"]}
                write_json(paths["plan"], plan)
                state = store.transition(state, "converting", stage="converting", updates={"output_path": str(output), "calibre": calibre})
            elif state["status"] == "converting":
                plan = read_json(paths["plan"])
                paths["generated"].parent.mkdir(parents=True, exist_ok=True)
                run_command(plan["command"], log_path=run_dir / "attempts" / "ebook-convert.log", timeout_seconds=600)
                manifest_store = _manifest(root, run_dir)
                manifest = manifest_store.load()
                manifest_store.upsert(manifest, artifact_entry(root, paths["generated"], role="generated_pdf", sources=[state["source_sha256"]]))
                state = store.transition(state, "verifying", stage="verifying", completed_step="conversion_finished")
            elif state["status"] == "verifying":
                verification = _verify_pdf(paths["generated"]) if target_format == "pdf" else _verify_markdown(paths["generated"], state.get("extraction", {}))
                write_json(paths["verification"], verification)
                manifest_store = _manifest(root, run_dir)
                manifest = manifest_store.load()
                manifest_store.upsert(manifest, artifact_entry(root, paths["verification"], role="verification", sources=[verification["sha256"]]))
                state = store.transition(state, "publishing", stage="publishing", updates={"verification": verification})
            elif state["status"] == "publishing":
                target = Path(state["output_path"])
                if target.resolve() != paths["generated"].resolve():
                    publish_file_without_overwrite(paths["generated"], target)
                receipt = {"status": "published", "path": str(target), "sha256": file_sha256(target), "size_bytes": target.stat().st_size, "published_at": iso_timestamp()}
                write_json(paths["publication"], receipt)
                manifest_store = _manifest(root, run_dir)
                manifest = manifest_store.load()
                manifest_store.set_status(manifest, "completed")
                state = store.transition(state, "completed", stage="completed", completed_step="published", updates={"publication": receipt})
        except FileExistsError as exc:
            resume_stage = "planning" if target_format == "pdf" else "rendering"
            state = store.pause(state, status="paused_output_conflict", error_code="output_exists", message=str(exc), resume_stage=resume_stage)
        except ConversionError as exc:
            status = "paused_configuration" if "ebook-convert" in str(exc) else "paused_input" if state["status"] in {"prepared", "validating_input", "staging_input"} else "paused_verification" if state["status"] == "verifying" else "paused_conversion"
            state = store.pause(state, status=status, error_code="conversion_error", message=str(exc), resume_stage=state["status"])
        except Exception as exc:
            state = store.pause(state, status="paused_error", error_code="runtime_error", message=str(exc), resume_stage=state["status"])
    return state


def run(
    root: Path,
    input_file: str | None,
    target_format: str,
    output_file: str | None = None,
    request_file: Path | None = None,
) -> dict[str, Any]:
    if request_file:
        request = _read_request(request_file)
    else:
        if not input_file:
            raise ConversionError("run 必须提供 --input 或 --request-file")
        request = {"input_file": input_file, "target_format": target_format.lstrip("."), "output_file": output_file, "paper_size": "a4"}
        validate_json_schema(request, REQUEST_SCHEMA)
    if request["target_format"] not in {"pdf", "md"}:
        raise ConversionError("不支持的目标格式：" + str(request["target_format"]))
    root = root.resolve()
    runs = root / "logs" / WORKFLOW / "runs"
    used = [item.name for item in runs.iterdir()] if runs.exists() else []
    run_id = unique_filename_timestamp(used) + "-" + uuid.uuid4().hex[:8]
    run_dir = _run_dir(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(_paths(run_dir)["request"], request)
    _write_manifest(root, run_dir, request, run_id)
    now = iso_timestamp()
    state = _store(root, run_id).create(_initial_state(run_id, now))
    return _advance(root, state)


def resume(root: Path, run_id: str, output_file: str | None = None) -> dict[str, Any]:
    store = _store(root.resolve(), run_id)
    state = store.load()
    if state["status"] == "completed":
        return state
    if str(state["status"]).startswith("paused_"):
        state = store.resume(state)
    return _advance(root.resolve(), state, output_override=output_file)


def status(root: Path, run_id: str) -> dict[str, Any]:
    return _store(root.resolve(), run_id).load()


def verify(root: Path, run_id: str) -> dict[str, Any]:
    root = root.resolve()
    state = status(root, run_id)
    if state["status"] != "completed":
        raise ConversionError(f"运行尚未完成：{state['status']}")
    manifest_store = _manifest(root, _run_dir(root, run_id))
    manifest = manifest_store.load()
    manifest_store.verify_files(manifest)
    target = Path(state["publication"]["path"])
    target_format = str(manifest.get("lineage", {}).get("target_format", "pdf"))
    verification = _verify_pdf(target) if target_format == "pdf" else _verify_markdown(target, state.get("extraction", {}))
    if verification["sha256"] != state["publication"]["sha256"]:
        raise ConversionError("已发布产物哈希已变化")
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="可恢复的 EPUB → PDF/Markdown 格式转换")
    parser.add_argument("command", choices=["run", "resume", "status", "verify", "list-formats", "init-request"])
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--input")
    parser.add_argument("--to", dest="target_format", default="pdf")
    parser.add_argument("--output")
    parser.add_argument("--run-id")
    parser.add_argument("--request-file")
    args = parser.parse_args(argv)
    try:
        if args.command == "list-formats":
            result = {"status": "ok", "conversions": list(SUPPORTED_CONVERSIONS)}
        elif args.command == "init-request":
            if not args.request_file or not args.input:
                raise ConversionError("init-request 必须提供 --request-file 和 --input")
            request = {"input_file": args.input, "target_format": args.target_format.lstrip("."), "output_file": args.output, "paper_size": "a4"}
            validate_json_schema(request, REQUEST_SCHEMA)
            request_path = Path(args.request_file).resolve()
            if request_path.exists():
                raise ConversionError(f"请求文件已存在，拒绝覆盖：{request_path}")
            write_json(request_path, request)
            result = {"status": "initialized", "request_file": str(request_path)}
        elif args.command == "run":
            if not args.input and not args.request_file:
                raise ConversionError("run 必须提供 --input 或 --request-file")
            result = run(args.root, args.input, args.target_format, args.output, Path(args.request_file).resolve() if args.request_file else None)
        elif not args.run_id:
            raise ConversionError(f"{args.command} 必须提供 --run-id")
        elif args.command == "resume":
            result = resume(args.root, args.run_id, args.output)
        elif args.command == "status":
            result = status(args.root, args.run_id)
        else:
            result = verify(args.root, args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
