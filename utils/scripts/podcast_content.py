"""Locate podcast exclusions without treating document text as instructions."""
from __future__ import annotations

import re
from pathlib import Path

from .file_transaction import file_sha256
from .markdown_structure import parse_atx_heading
from .structured_io import read_json


def require_body(text: str) -> None:
    """A surviving title or control marker does not constitute podcast content."""
    body = '\n'.join(line for line in text.splitlines() if parse_atx_heading(line) is None)
    body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    body = re.sub(r'\{\{(?:display|pause):\d+ms\}\}', '', body)
    if not any(character.isalnum() for character in body):
        raise ValueError('筛选后没有正文，不能生成空播客')


def tutor_provenance(path: Path | None) -> dict | None:
    if path is None or path.parent.name != '课程':
        return None
    project = path.parent.parent
    record = project / 'artifacts/lessons' / path.with_suffix('.json').name
    route = project / 'artifacts/学习路线.json'
    if not record.is_file() or not route.is_file():
        return None
    lesson, model = read_json(record), read_json(route)
    if not any(item.get('lesson_id') == lesson.get('lesson_id') and item.get('filename') == path.name
               for item in model.get('lessons', [])):
        return None
    template = lesson.get('markdown_template', '')
    # Only editable answer regions may differ from the generated lesson.
    def clean(text):
        return re.sub(r'<!-- answer:([^:]+):start -->.*?<!-- answer:\1:end -->', '',
                      text.replace('\r\n', '\n'), flags=re.S).strip()
    if not template or clean(template) != clean(path.read_text(encoding='utf-8-sig')):
        return None
    return {'producer_skill': 'interactive-tutor', 'record_path': str(record.resolve()),
            'record_sha256': file_sha256(record), 'lesson_id': lesson['lesson_id']}


def inspect_content(text: str, *, tutor: bool = False) -> list[dict]:
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    headings = []
    fence = None
    for i, line in enumerate(lines):
        offsets.append(offset)
        offset += len(line)
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence is None:
            heading = parse_atx_heading(line.rstrip('\r\n'))
            if heading:
                headings.append((i, *heading))
    offsets.append(len(text))
    candidates = []
    if tutor:
        for i, line in enumerate(lines):
            if re.fullmatch(r'\[🎧 收听本课播客\]\([^\n]*\.mp3\)\s*', line):
                candidates.append({'candidate_id': f'c{len(candidates)+1:03d}', 'kind': 'tutor_audio',
                    'title': '本课播客引用', 'start': offsets[i], 'end': offsets[i+1],
                    'line_start': i+1, 'line_end': i+1, 'reason': '播放器引用不参与朗读', 'automatic_skip': True})
    for pos, (i, level, title) in enumerate(headings):
        kind = None
        if title in ('前置知识', '前置知识点', '前置知识点列表'):
            kind = 'prerequisites'
        elif title in ('本课学习的知识点', '本节知识点', '本节知识点列表'):
            kind = 'knowledge_list'
        elif title in ('正式习题', '习题', '练习', '练习题'):
            kind = 'exercises'
        elif tutor and title in ('本课学习反馈', '本课批改总结', '学习反馈', '课程播客'):
            kind = 'tutor_feedback'
        elif title in ('参考文献', '版权声明', '目录', '致谢', '双语术语'):
            kind = 'ancillary'
        if kind is None:
            continue
        end = next((j for j, lev, _ in headings[pos + 1:] if lev <= level), len(lines))
        if kind == 'knowledge_list':
            # Tutor puts overview/body under this heading after ONE list paragraph.
            j = i + 1
            while j < end and not lines[j].strip():
                j += 1
            while j < end and lines[j].strip():
                j += 1
            end = j
        # A parent exclusion already contains its nested headings; ask only once.
        if any(c['start'] <= offsets[i] and c['end'] >= offsets[end] for c in candidates):
            continue
        candidates.append({'candidate_id': f'c{len(candidates) + 1:03d}', 'kind': kind,
                           'title': title, 'start': offsets[i], 'end': offsets[end],
                           'line_start': i + 1, 'line_end': end,
                           'reason': '学习清单、作答材料或附属信息可能无需朗读',
                           'automatic_skip': tutor and kind in ('prerequisites', 'knowledge_list', 'exercises', 'tutor_feedback')})
    return candidates


def selected_content(text: str, candidates: list[dict], decisions: list[dict]) -> str:
    by_id = {item['candidate_id']: item for item in candidates}
    supplied = {item['candidate_id']: item['skip'] for item in decisions}
    expected = {key for key, item in by_id.items() if not item['automatic_skip']}
    if set(supplied) != expected or len(supplied) != len(decisions) or any(type(v) is not bool for v in supplied.values()):
        raise ValueError('必须逐项决定非自动候选，不能重复、遗漏或加入未知 ID')
    spans = sorted((item['start'], item['end']) for key, item in by_id.items()
                   if item['automatic_skip'] or supplied.get(key))
    chunks, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            raise ValueError('筛选区间重叠')
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    result = ''.join(chunks).strip()
    if not result:
        raise ValueError('筛选后没有正文，不能生成空播客')
    require_body(result)
    return result + '\n'


def source_blocks(text: str, limit: int = 1600) -> list[dict]:
    """Bound generation packets; preserve source text exactly, including long paragraphs."""
    require_body(text)
    blocks = []
    for start in range(0, len(text), limit):
        chunk = text[start:start + limit]
        if chunk.strip():
            blocks.append({'block_id': f'b{len(blocks) + 1:03d}', 'start': start,
                           'end': start + len(chunk), 'text': chunk})
    if not blocks:
        raise ValueError('正文不能为空')
    return blocks
