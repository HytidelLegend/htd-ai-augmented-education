"""Discover and validate a beta-build-curriculum-navigation artifact bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.scripts.learning_navigation import render_navigation_markdown, validate_navigation
from utils.scripts.structured_io import read_json, validate_json_schema

CODE_ROOT = Path(__file__).resolve().parents[2]


class NavigationBundleError(ValueError):
    """Raised when a navigation bundle is incomplete or internally inconsistent."""


def render_source_fragment_markdown(fragment: dict[str, Any]) -> str:
    lines = [
        f"# {Path(fragment['source']['path']).stem}：学习导航片段",
        "",
        "> 本文件是单份资料贡献给总导航的学习要点与关系记录，不代表已经完成全局排序。",
        "",
        "## 学习要点",
        "",
    ]
    for item in fragment["learning_points"]:
        lines.extend([
            f"### {item['title']}", "", f"- ID：`{item['point_id']}`",
            f"- 难度：`{item['difficulty']}`", f"- 学习目的：{item['purpose']}", "",
        ])
    lines.extend(["## 在本资料中识别出的前后关系", ""])
    if not fragment["relationship_records"]:
        lines.append("- 暂无明确的前后关系。")
    for item in fragment["relationship_records"]:
        lines.append(f"- `{item['predecessor_id']}` 应先于 `{item['successor_id']}`：{item['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def load_navigation_bundle(root: Path, navigation_json: Path, material_project: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    navigation_json = navigation_json.resolve()
    navigation = read_json(navigation_json)
    schema = CODE_ROOT / "utils/references/learning-navigation-v2.schema.json"
    try:
        from .learning_material_backup import verify_backup
        paths = verify_backup(root, material_project, navigation) if material_project is not None else None
        validate_navigation(navigation, schema_path=schema, root=root, verify_sources=True, material_paths=paths)
    except Exception as exc:
        raise NavigationBundleError(str(exc)) from exc

    navigation_markdown = navigation_json.with_suffix(".md")
    if not navigation_markdown.is_file():
        raise NavigationBundleError(f"总导航缺少同名 Markdown 视图：{navigation_markdown}")
    if navigation_markdown.read_text(encoding="utf-8-sig") != render_navigation_markdown(navigation):
        raise NavigationBundleError("总导航 Markdown 与权威 JSON 不一致")

    source_dir = navigation_json.with_suffix(".sources")
    if not source_dir.is_dir():
        raise NavigationBundleError(f"总导航缺少单资料片段目录：{source_dir}")
    fragment_schema = CODE_ROOT / "utils/references/learning-navigation-source-fragment-v2.schema.json"
    fragments: list[dict[str, Any]] = []
    sources = {item["source_id"]: item for item in navigation["sources"]}
    units = {item["unit_id"]: item for item in navigation["units"]}
    units_by_source: dict[str, set[str]] = {source_id: set() for source_id in sources}
    for unit in navigation["units"]:
        if unit["source"]:
            units_by_source[unit["source"]["source_id"]].add(unit["unit_id"])
    global_edges = {
        (prerequisite, unit["unit_id"])
        for unit in navigation["units"] for prerequisite in unit["prerequisites"]
    }
    source_unit_ids = {unit_id for unit_id, unit in units.items() if unit["source"] is not None}
    expected_fragment_edges = {
        pair for pair in global_edges if pair[0] in source_unit_ids and pair[1] in source_unit_ids
    }
    seen_fragment_edges: set[tuple[str, str]] = set()
    extra_json = {path.stem for path in source_dir.glob("*.json")} - set(sources)
    if extra_json:
        raise NavigationBundleError(f"来源片段目录存在总导航未登记的 JSON：{', '.join(sorted(extra_json))}")
    for source_id, source in sources.items():
        fragment_json = source_dir / f"{source_id}.json"
        fragment_markdown = source_dir / f"{source_id}.md"
        if not fragment_json.is_file() or not fragment_markdown.is_file():
            raise NavigationBundleError(f"导航缺少来源片段双产物：{source_id}")
        fragment = read_json(fragment_json)
        validate_json_schema(fragment, fragment_schema)
        if fragment["source"] != {"source_id": source_id, "path": source["path"], "sha256": source["sha256"]}:
            raise NavigationBundleError(f"来源片段与总导航来源不一致：{source_id}")
        points = {item["point_id"]: item for item in fragment["learning_points"]}
        if set(points) != units_by_source[source_id]:
            raise NavigationBundleError(f"来源片段学习要点与总导航单元不一致：{source_id}")
        for point_id, point in points.items():
            unit = units[point_id]
            for field in ("title", "difficulty", "purpose"):
                if point.get(field) != unit[field]:
                    raise NavigationBundleError(f"来源片段字段与总导航单元不一致：{point_id}.{field}")
            if point.get("source_locator") != unit["source"]:
                raise NavigationBundleError(f"来源片段定位与总导航单元不一致：{point_id}")
        for relation in fragment["relationship_records"]:
            pair = (relation["predecessor_id"], relation["successor_id"])
            if pair not in global_edges:
                raise NavigationBundleError(f"来源片段关系未出现在总导航先修关系中：{pair[0]} → {pair[1]}")
            seen_fragment_edges.add(pair)
        if fragment_markdown.read_text(encoding="utf-8-sig") != render_source_fragment_markdown(fragment):
            raise NavigationBundleError(f"来源片段 Markdown 与 JSON 不一致：{source_id}")
        fragments.append({"source_id": source_id, "json": str(fragment_json), "markdown": str(fragment_markdown)})
    missing_fragment_edges = expected_fragment_edges - seen_fragment_edges
    if missing_fragment_edges:
        rendered = "、".join(f"{left} → {right}" for left, right in sorted(missing_fragment_edges))
        raise NavigationBundleError(f"总导航关系未记录在任何来源片段中：{rendered}")

    return {
        "navigation": navigation,
        "navigation_json": str(navigation_json),
        "navigation_markdown": str(navigation_markdown),
        "source_dir": str(source_dir),
        "fragments": fragments,
    }


def learning_queue(navigation: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    units = sorted(navigation["units"], key=lambda item: item["sequence"])
    expected = list(range(1, len(units) + 1))
    if [item["sequence"] for item in units] != expected:
        raise NavigationBundleError("导航单元 sequence 必须从 1 开始连续递增")
    supported = {"required", "recommended", "optional", "deferred", "critical_reading", "not_recommended_now"}
    unknown = sorted({item["importance"] for item in units} - supported)
    if unknown:
        raise NavigationBundleError(f"导航存在未知 importance：{', '.join(unknown)}")
    by_id = {item["unit_id"]: item for item in units}
    for unit in units:
        if unit["importance"] in {"deferred", "not_recommended_now"}:
            continue
        deferred_dependencies = [item for item in unit["prerequisites"] if by_id[item]["importance"] in {"deferred", "not_recommended_now"}]
        if deferred_dependencies:
            raise NavigationBundleError(f"当前学习单元依赖延后单元：{unit['unit_id']}")
    active = [item for item in units if item["importance"] not in {"deferred", "not_recommended_now"}]
    return {
        "active": active,
        "required": [item for item in active if item["importance"] in {"required", "critical_reading"}],
        "recommended": [item for item in active if item["importance"] in {"recommended", "optional"}],
        "deferred": [item for item in units if item["importance"] in {"deferred", "not_recommended_now"}],
    }
