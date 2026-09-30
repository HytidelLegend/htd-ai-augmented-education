"""Build the dictionary graph from authoritative word entries."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Callable


def candidate_node_id(lemma: str, normalize: Callable[[str], str]) -> str:
    return "candidate:" + hashlib.sha256(normalize(lemma).encode("utf-8")).hexdigest()[:20]


def candidate_node_ids(entries: dict[str, dict], normalize: Callable[[str], str]) -> set[str]:
    lemmas = []
    for entry in entries.values():
        lemmas.extend(form["form"]["text"] for form in entry.get("inflections", []))
        lemmas.extend(derivative["word"] for derivative in entry.get("derivatives", []))
        lemmas.extend(relation["targetLemma"] for group in ("relationships", "pendingRelations")
                      for relation in entry.get(group, []) if relation.get("targetLemma"))
    return {candidate_node_id(lemma, normalize) for lemma in lemmas}


def canonical_family_ids(entries: dict[str, dict], normalize: Callable[[str], str]) -> dict[str, str]:
    """Resolve confirmed derivative links, including shared unbuilt targets."""
    parent = {wid: wid for wid in entries}

    def find(wid: str) -> str:
        if parent[wid] != wid:
            parent[wid] = find(parent[wid])
        return parent[wid]

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    by_family: dict[str, str] = {}
    by_lemma = {normalize(entry["lemma"]): wid for wid, entry in entries.items()}
    by_derivative: dict[str, str] = {}
    for wid, entry in entries.items():
        family_id = entry["familyId"]
        if family_id in by_family:
            union(wid, by_family[family_id])
        else:
            by_family[family_id] = wid
        for derivative in entry.get("derivatives", []):
            key = normalize(derivative["word"])
            if key in by_derivative:
                union(wid, by_derivative[key])
            else:
                by_derivative[key] = wid
            target = derivative.get("targetWordId") or by_lemma.get(key)
            if target in entries:
                union(wid, target)
    groups: dict[str, list[str]] = defaultdict(list)
    for wid in entries:
        groups[find(wid)].append(wid)
    return {wid: min(entries[member]["familyId"] for member in members)
            for members in groups.values() for wid in members}


def project_graph(entries: dict[str, dict], summaries: dict[str, dict], *,
                  normalize: Callable[[str], str], selected: str | None,
                  visible: dict[str, bool], show_others: bool, search: str,
                  limit: int, show_outside: bool, stage: dict | None,
                  project_words: list[dict] | None = None) -> dict[str, Any]:
    project_ids = {row["wordId"] for row in project_words} if project_words is not None else set(entries)
    if selected and selected not in entries and selected not in project_ids:
        raise FileNotFoundError(selected)

    nodes = {wid: {**summary, "kind": "entry", "status": "linked", "nodeId": wid}
             for wid, summary in summaries.items()}
    by_lemma = {normalize(entry["lemma"]): wid for wid, entry in entries.items()}
    for row in project_words or []:
        if row["wordId"] not in nodes:
            nodes[row["wordId"]] = {"nodeId": row["wordId"], "wordId": row["wordId"],
                                    "lemma": row["lemma"], "familyId": row["wordId"],
                                    "core": [], "more": [], "senseIds": [], "outsideStage": False,
                                    "kind": "relation_candidate", "status": "pending_collection"}
        by_lemma[normalize(row["lemma"])] = row["wordId"]
    parent: dict[str, str] = {wid: wid for wid in entries}
    family_nodes: set[str] = set(entries)
    edges: dict[tuple[str, str, str, str], dict] = {}

    def find(node_id: str) -> str:
        parent.setdefault(node_id, node_id)
        if parent[node_id] != node_id:
            parent[node_id] = find(parent[node_id])
        return parent[node_id]

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    def target_node(lemma: str, kind: str, status: str, source_id: str) -> str:
        key = normalize(lemma)
        if key in by_lemma:
            return by_lemma[key]
        node_id = candidate_node_id(lemma, normalize)
        if node_id not in nodes:
            nodes[node_id] = {"nodeId": node_id, "wordId": node_id, "lemma": lemma,
                              "familyId": node_id if kind == "relation_candidate" else source_id,
                              "core": [], "more": [], "senseIds": [],
                              "outsideStage": summaries[source_id]["outsideStage"], "kind": kind, "status": status}
        elif kind in ("inflection", "derivative"):
            nodes[node_id]["kind"] = kind
            nodes[node_id]["status"] = status
        if not summaries[source_id]["outsideStage"]:
            nodes[node_id]["outsideStage"] = False
        return node_id

    def add_edge(source: str, target: str, kind: str, relation_id: str,
                 status: str, source_sense: str | None = None,
                 target_sense: str | None = None, rule_classified: bool = False) -> None:
        if source == target:
            return
        key = (source, target, kind, relation_id)
        edges[key] = {"source": source, "target": target, "type": kind,
                      "relationshipId": relation_id, "status": status,
                      "sourceSenseId": source_sense, "targetSenseId": target_sense,
                      "wordLevel": kind in ("family", "spelling_similar"),
                      "ruleClassified": rule_classified,
                      "relations": [{"relationshipId": relation_id, "sourceWordId": source,
                                     "sourceSenseId": source_sense, "targetSenseId": target_sense}]}

    families_by_id: dict[str, list[str]] = defaultdict(list)
    for wid, entry in entries.items():
        families_by_id[entry["familyId"]].append(wid)
        for form in entry.get("inflections", []):
            lemma = form["form"]["text"]
            target = target_node(lemma, "inflection", "confirmed_unbuilt", wid)
            nodes[target].setdefault("sourceFormIds", []).append(form["formId"])
            family_nodes.add(target)
            union(wid, target)
            add_edge(wid, target, "family", form["formId"], form["form"]["verificationStatus"])
        for derivative in entry.get("derivatives", []):
            target = derivative.get("targetWordId") or target_node(derivative["word"], "derivative", "confirmed_unbuilt", wid)
            nodes[target].setdefault("sourceDerivativeIds", []).append(derivative["derivativeId"])
            family_nodes.add(target)
            union(wid, target)
            add_edge(wid, target, "family", derivative["derivativeId"], derivative.get("verificationStatus", "candidate"))
    for members in families_by_id.values():
        for wid in members[1:]:
            union(members[0], wid)

    for wid, entry in entries.items():
        selected_senses = set(summaries[wid]["senseIds"])
        if stage is not None and summaries[wid]["outsideStage"]:
            continue
        chosen_relations = entry.get("relationships", []) + entry.get("pendingRelations", [])
        for relation in chosen_relations:
            raw_kind = relation.get("type") or relation.get("proposedType")
            rule_classified = raw_kind == "synonym_or_near_synonym"
            kind = "near_synonym" if rule_classified else raw_kind
            if kind not in ("synonym", "near_synonym", "antonym", "spelling_similar") or not visible.get(kind):
                continue
            if kind != "spelling_similar" and relation.get("sourceSenseId") not in selected_senses:
                continue
            if stage is not None and "candidateId" not in relation and relation.get("relationshipId") not in stage["words"].get(wid, {}).get("relationshipIds", []):
                continue
            target = relation.get("targetWordId")
            if not target:
                lemma = relation.get("targetLemma", "")
                if not lemma:
                    continue
                target = target_node(lemma, "relation_candidate", "pending" if "candidateId" in relation else "confirmed_unbuilt", wid)
            if target not in nodes or (stage is not None and not show_outside and nodes[target]["outsideStage"]):
                continue
            status = "pending" if "candidateId" in relation or relation.get("verificationStatus") == "pending" else "confirmed"
            add_edge(wid, target, kind, relation.get("relationshipId") or relation["candidateId"],
                     status, relation.get("sourceSenseId"), relation.get("targetSenseId"), rule_classified)

    all_families: dict[str, list[str]] = defaultdict(list)
    for node_id in family_nodes:
        all_families[find(node_id)].append(node_id)
    for members in all_families.values():
        family_id = min((entries[wid]["familyId"] for wid in members if wid in entries), default=min(members))
        for node_id in members:
            nodes[node_id]["familyId"] = family_id
    def neighboring(source: str, maximum: int) -> tuple[set[str], int]:
        buckets: dict[str, list[str]] = defaultdict(list)
        for edge in edges.values():
            if source not in (edge["source"], edge["target"]) or edge["type"] == "family":
                continue
            neighbor = edge["target"] if edge["source"] == source else edge["source"]
            buckets[edge["type"]].append(neighbor)
        for kind in buckets:
            buckets[kind] = sorted(set(buckets[kind]), key=lambda node_id: (nodes[node_id]["status"] == "pending", normalize(nodes[node_id]["lemma"])))
        candidates = set().union(*map(set, buckets.values())) if buckets else set()
        picked: set[str] = set()
        while len(picked) < maximum and any(buckets.values()):
            for kind in ("synonym", "near_synonym", "antonym", "spelling_similar"):
                if buckets[kind]:
                    picked.add(buckets[kind].pop(0))
                if len(picked) >= maximum:
                    break
        return picked, len(candidates - picked)

    if selected and not show_others:
        chosen = {selected}
        if visible.get("family"):
            chosen.update(all_families.get(find(selected), []))
        picked, deferred_count = neighboring(selected, 12)
        chosen.update(picked)
    elif project_words is not None:
        deferred_count = 0
        chosen = set(project_ids)
        if selected:
            if visible.get("family"):
                chosen.update(all_families.get(find(selected), []))
            picked, deferred_count = neighboring(selected, 500)
            chosen.update(picked)
    else:
        deferred_count = 0
        chosen = set(family_nodes) if visible.get("family") else set()
        for edge in edges.values():
            if (edge["type"] != "family" or visible.get("family")) and (
                selected or all(nodes[node_id]["kind"] == "entry" for node_id in (edge["source"], edge["target"]))
            ):
                chosen.update((edge["source"], edge["target"]))
        if not selected:
            for wid in entries:
                if stage is not None and summaries[wid]["outsideStage"]:
                    continue
                picked, deferred = neighboring(wid, 4)
                chosen.update(picked)
                if picked:
                    chosen.add(wid)
                deferred_count += deferred
        if selected:
            chosen.add(selected)
    if search:
        term = normalize(search)
        if project_words is None:
            chosen.update(node_id for node_id, node in nodes.items() if term in normalize(node["lemma"]))
        elif not selected:
            chosen = {node_id for node_id in chosen if term in normalize(nodes[node_id]["lemma"])}
    if stage is not None and not show_outside:
        chosen = {node_id for node_id in chosen if not nodes[node_id]["outsideStage"] or node_id == selected}
    ordered = sorted(chosen, key=lambda node_id: (node_id != selected,
                     bool(search) and normalize(search) not in normalize(nodes[node_id]["lemma"]),
                     nodes[node_id]["kind"] != "entry",
                     normalize(nodes[node_id]["lemma"])))
    shown = set(ordered[:max(1, min(500, limit))])
    rendered_edges = [edge for edge in edges.values() if edge["source"] in shown and edge["target"] in shown and
                      (edge["type"] != "family" or visible.get("family"))]
    families = [{"familyId": nodes[members[0]]["familyId"], "nodeIds": sorted(set(members) & shown)}
                for members in all_families.values() if set(members) & shown] if visible.get("family") else []
    return {"nodes": [nodes[node_id] for node_id in ordered if node_id in shown],
            "edges": rendered_edges, "families": families,
            "entryCount": sum(node_id in entries for node_id in shown),
            "placeholderCount": sum(node_id not in entries for node_id in shown),
            "hiddenCount": len(ordered) - len(shown) + deferred_count, "selected": selected}
