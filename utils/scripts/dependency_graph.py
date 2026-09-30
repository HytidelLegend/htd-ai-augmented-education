"""Deterministic dependency-graph analysis for learning navigation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class DependencyGraphError(ValueError):
    pass


def edges_from_units(units: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return normalized predecessor -> successor edges from unit prerequisites."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for unit in units:
        successor = unit["unit_id"]
        for predecessor in unit.get("prerequisites", []):
            key = (predecessor, successor)
            if key not in seen:
                seen.add(key)
                result.append({"predecessor_id": predecessor, "successor_id": successor})
    return result


def _validate(nodes: list[str], edges: list[dict[str, Any]]) -> None:
    if len(nodes) != len(set(nodes)):
        raise DependencyGraphError("学习要点 ID 重复")
    known = set(nodes)
    for edge in edges:
        before, after = edge["predecessor_id"], edge["successor_id"]
        if before not in known or after not in known:
            raise DependencyGraphError(f"学习关系引用未知要点：{before} -> {after}")
        if before == after:
            raise DependencyGraphError(f"学习要点不能要求先学习自身：{before}")


def _blocked_groups(nodes: list[str], outgoing: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan SCC; implementation detail, never exposed with graph-theory wording."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    groups: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for child in outgoing[node]:
            if child not in indices:
                visit(child)
                lowlinks[node] = min(lowlinks[node], lowlinks[child])
            elif child in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[child])
        if lowlinks[node] == indices[node]:
            group: list[str] = []
            while True:
                current = stack.pop()
                on_stack.remove(current)
                group.append(current)
                if current == node:
                    break
            if len(group) > 1:
                groups.append(group)

    for node in nodes:
        if node not in indices:
            visit(node)
    position = {node: idx for idx, node in enumerate(nodes)}
    return [sorted(group, key=position.get) for group in groups]


def analyze_dependency_graph(
    nodes: list[str], edges: list[dict[str, Any]], *, preferred_order: list[str] | None = None
) -> dict[str, Any]:
    """Analyze orderability and expose compact choice points for Agent judgment."""
    _validate(nodes, edges)
    base = preferred_order or nodes
    if set(base) != set(nodes) or len(base) != len(nodes):
        raise DependencyGraphError("参考顺序必须完整覆盖所有学习要点")
    rank = {node: idx for idx, node in enumerate(base)}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node: 0 for node in nodes}
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        key = (edge["predecessor_id"], edge["successor_id"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(dict(edge))
        outgoing[key[0]].append(key[1])
        indegree[key[1]] += 1
    ready = sorted((node for node, count in indegree.items() if count == 0), key=rank.get)
    order: list[str] = []
    choices: list[dict[str, Any]] = []
    while ready:
        if len(ready) > 1:
            choices.append({
                "position": len(order) + 1,
                "candidate_ids": list(ready),
                "recommended_id": ready[0],
            })
        current = ready.pop(0)
        order.append(current)
        for child in sorted(outgoing[current], key=rank.get):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=rank.get)
    blocked = _blocked_groups(nodes, outgoing) if len(order) != len(nodes) else []
    return {
        "is_dag": len(order) == len(nodes),
        "recommended_order": order,
        "choice_points": choices,
        "blocked_groups": blocked,
        "edge_count": len(normalized),
        "edges": normalized,
    }


def validate_selected_order(nodes: list[str], edges: list[dict[str, Any]], selected: list[str]) -> None:
    _validate(nodes, edges)
    if len(selected) != len(nodes) or set(selected) != set(nodes):
        raise DependencyGraphError("选择的学习顺序必须完整覆盖全部学习要点，且不得重复")
    position = {node: idx for idx, node in enumerate(selected)}
    violations = [
        edge for edge in edges
        if position[edge["predecessor_id"]] >= position[edge["successor_id"]]
    ]
    if violations:
        first = violations[0]
        raise DependencyGraphError(
            f"选择的顺序违反前置要求：{first['predecessor_id']} 必须早于 {first['successor_id']}"
        )


def build_plain_language_loop_report(
    analysis: dict[str, Any], units: list[dict[str, Any]], relationship_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a user-facing report without graph-theory terminology."""
    by_id = {unit["unit_id"]: unit for unit in units}
    reasons = {
        (item["predecessor_id"], item["successor_id"]): item
        for item in relationship_records
    }
    groups = []
    for number, ids in enumerate(analysis["blocked_groups"], start=1):
        id_set = set(ids)
        requirements = []
        for edge in analysis["edges"]:
            before, after = edge["predecessor_id"], edge["successor_id"]
            if before in id_set and after in id_set:
                detail = reasons.get((before, after), {})
                requirements.append({
                    "before_id": before,
                    "before_title": by_id[before]["title"],
                    "after_id": after,
                    "after_title": by_id[after]["title"],
                    "explanation": detail.get(
                        "reason", f"“{by_id[after]['title']}”把“{by_id[before]['title']}”作为需要先掌握的内容。"
                    ),
                    "evidence_refs": detail.get("evidence_refs", []),
                    "confidence": detail.get("confidence", "unknown"),
                })
        confidence_rank = {"low": 0, "unknown": 1, "medium": 2, "high": 3}
        weakest = min(requirements, key=lambda item: confidence_rank.get(item["confidence"], 1))
        concrete_suggestions = [
            f"优先复核“{weakest['after_title']}必须先学{weakest['before_title']}”这一要求，因为它目前的依据相对较弱。",
            f"如果这里只是建议提前了解，可以取消“{weakest['before_title']}必须早于{weakest['after_title']}”的硬性要求。",
            f"如果两项确实互相解释，可以把“{weakest['before_title']}”拆成入门部分和进阶部分，先学入门部分，再学“{weakest['after_title']}”，最后回到进阶部分。",
        ]
        groups.append({
            "group_number": number,
            "summary": "这些内容都要求先学组内的另一项，因此现在无法选出合理的起点。",
            "learning_points": [{"point_id": item, "title": by_id[item]["title"]} for item in ids],
            "requirements": requirements,
            "suggestions": concrete_suggestions,
            "suggested_resolution": {
                "action": "remove_requirement",
                "predecessor_id": weakest["before_id"],
                "successor_id": weakest["after_id"],
                "plain_explanation": concrete_suggestions[1],
            },
        })
    return {
        "status": "learning_order_blocked",
        "title": "这些内容的学习顺序互相卡住了",
        "plain_summary": "目前无论从哪一项开始，都会遇到“还需要先学另一项”的问题，需要先调整一处学习要求。",
        "groups": groups,
    }


def render_plain_language_loop_report(report: dict[str, Any]) -> str:
    lines = [f"# {report['title']}", "", report["plain_summary"], ""]
    for group in report["groups"]:
        lines.extend([f"## 第 {group['group_number']} 处卡点", "", group["summary"], "", "### 具体卡在哪里", ""])
        for item in group["requirements"]:
            lines.append(f"- 学习“{item['after_title']}”之前要求先理解“{item['before_title']}”：{item['explanation']}")
        lines.extend(["", "### 可以怎样调整", ""])
        lines.extend(f"- {item}" for item in group["suggestions"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
