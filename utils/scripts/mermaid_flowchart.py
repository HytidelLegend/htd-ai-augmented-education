"""Render and check dependency flowcharts from shared structured graph data."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .dependency_graph import analyze_dependency_graph
from .structured_io import validate_json_schema

UTILS = Path(__file__).resolve().parents[1]
SCHEMA = UTILS / 'references/dependency-flowchart-v1.schema.json'
TEMPLATE = UTILS / 'templates/dependency-flowchart.template.md'


def node_id(value: str) -> str:
    return 'n_' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def escape_label(value: str) -> str:
    # Encode Mermaid/HTML/Markdown syntax; newline characters stay inside a label.
    value = ' '.join(value.split())
    special = set('#"&<>`\\[]{}|')
    return ''.join(f'#{ord(char)};' if char in special else char for char in value)


def _parts(graph: dict) -> tuple[dict[str, str], set[tuple[str, str]]]:
    validate_json_schema(graph, SCHEMA)
    ids = [node['id'] for node in graph['nodes']]
    edges = [{'predecessor_id': edge['from'], 'successor_id': edge['to']} for edge in graph['edges']]
    if not analyze_dependency_graph(ids, edges)['is_dag']:
        raise ValueError('思维导图依赖关系包含环')
    labels = {}
    for node in graph['nodes']:
        fields = [node['label']]
        if node.get('track'):
            fields.append(node['track'])
        fields.append(node['status'])
        labels[node_id(node['id'])] = escape_label('｜'.join(fields))
    links = {(node_id(edge['from']), node_id(edge['to'])) for edge in graph['edges']}
    return labels, links


def render_flowchart(graph: dict) -> str:
    labels, edges = _parts(graph)
    lines = [f'    {key}["{labels[key]}"]' for key in sorted(labels)]
    lines += [f'    {before} --> {after}' for before, after in sorted(edges)]
    if not lines:
        lines = ['    %% 暂无节点']
    return TEMPLATE.read_text(encoding='utf-8').replace('{{body}}', '\n'.join(lines)).rstrip('\n')


def verify_flowchart(graph: dict, markdown: str) -> None:
    """Check complete node/edge coverage and labels, including isolated nodes."""
    expected_labels, expected_edges = _parts(graph)
    lines = markdown.splitlines()
    if len(lines) < 4 or lines[:2] != ['```mermaid', 'flowchart LR'] or lines[-1] != '```':
        raise ValueError('思维导图格式无效')
    labels = {}; edges = set()
    for line in lines[2:-1]:
        node = re.fullmatch(r'    (n_[0-9a-f]{64})\["([^"\r\n]*)"\]', line)
        edge = re.fullmatch(r'    (n_[0-9a-f]{64}) --> (n_[0-9a-f]{64})', line)
        if node:
            if node[1] in labels:
                raise ValueError('思维导图节点重复')
            labels[node[1]] = node[2]
        elif edge:
            pair = (edge[1], edge[2])
            if pair in edges:
                raise ValueError('思维导图依赖边重复')
            edges.add(pair)
        elif line != '    %% 暂无节点' or expected_labels:
            raise ValueError('思维导图含未知语法')
    if labels != expected_labels or edges != expected_edges:
        raise ValueError('思维导图与依赖图的节点、边或状态不一致')
