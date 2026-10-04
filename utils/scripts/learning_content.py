"""Source point division, objective coverage and deterministic teaching views."""
from pathlib import Path
import re
from .structured_io import json_digest, validate_json_schema
from .markdown_report import markdown_table

BLOCK_LABELS = {'explanation': '核心解释', 'steps': '推理或操作步骤',
                'example': '完整示例', 'boundary': '边界或反例', 'application': '应用提示'}


def grouping_context(model, config):
    """Suggest opportunities, never merge solely to increase character count."""
    pending = [l for l in model['lessons'] if not l['archived'] and not l['skip']
               and l['status'] in ('pending', 'retry') and not l.get('content')]
    candidates = []
    for left, right in zip(pending, pending[1:]):
        if (left['chapter_id'] == right['chapter_id'] and left['track'] == right['track']
                and len(set(left['teaches_unit_ids'] + right['teaches_unit_ids'])) <= config['lesson']['max_new_units']):
            candidates.append({'lesson_ids': [left['lesson_id'], right['lesson_id']],
                               'unit_ids': list(dict.fromkeys(left['teaches_unit_ids'] + right['teaches_unit_ids']))})
    return {'candidates': candidates,
            'policy': '按主题、前置关系和学习负担判断是否合并；候选仅满足初步数量与路线约束，合并仍须图校验；单知识点可保留，不为凑字数合并。'}


def fragments(path, source_id, source_hash):
    """Inventory every nonblank body line, including title-free transcripts."""
    result = []
    for number, text in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        if not text.strip():
            continue
        result.append({'fragment_id': 'FR-' + json_digest([source_id, source_hash, number])[:16],
                       'start_line': number, 'end_line': number, 'text': text})
    return result


def validate_division(source, packet, decision):
    validate_json_schema(decision, Path(__file__).resolve().parents[1] / 'references/learning-point-division-v1.schema.json')
    if decision['source_id'] != source['source_id'] or decision['source_sha256'] != source['sha256']:
        raise ValueError('要点划分对应的来源或哈希已变化')
    by_id = {x['fragment_id']: x for x in packet['fragments']}
    referenced = set(); point_ids = set(); points = []
    for point in decision['points']:
        ids = point['fragment_ids']
        if not ids or not set(ids) <= set(by_id): raise ValueError('要点引用无效正文片段')
        if not point['summary'].strip() or not point['reason'].strip(): raise ValueError('要点摘要和安排理由不能为空')
        pid = 'POINT-' + json_digest([source['source_id'], ids, point['summary']])[:16]
        if pid in point_ids: raise ValueError('原文要点重复')
        point_ids.add(pid); referenced.update(ids)
        points.append({**point, 'point_id': pid, 'source_id': source['source_id'],
                       'start_line': min(by_id[x]['start_line'] for x in ids),
                       'end_line': max(by_id[x]['end_line'] for x in ids)})
    ignored = set()
    for item in decision['ignored_fragments']:
        if item['fragment_id'] not in by_id or item['fragment_id'] in ignored:
            raise ValueError('忽略的正文片段无效或重复')
        if not item['reason'].strip(): raise ValueError('忽略正文片段须说明非知识内容理由')
        ignored.add(item['fragment_id'])
    if ignored & referenced or ignored | referenced != set(by_id):
        raise ValueError('全部正文片段须归入要点或注明非实质内容理由，不能重复忽略')
    if not points: raise ValueError('来源必须包含实质要点')
    return points


def coverage_rows(model):
    rows = []
    for point in model.get('material_points', []):
        units = set(point['unit_ids']); records = []
        for lesson in model['lessons']:
            if lesson['archived'] or not units.intersection(lesson['teaches_unit_ids']): continue
            blocks = [b for p in lesson.get('content', {}).get('teaching_points', [])
                      for b in p.get('blocks', []) if point['point_id'] in b.get('point_ids', [])]
            records.append({'lesson_id': lesson['lesson_id'], 'number': lesson['number'],
                            'block_ids': [b['block_id'] for b in blocks],
                            'degree': '充分讲解' if any(b['coverage'] == 'explained' for b in blocks) else '仅提及' if blocks else '尚未讲解'})
        if not records: raise ValueError('原文要点缺少有效课程：' + point['summary'])
        rows.append({**point, 'lessons': records})
    return rows


def coverage_markdown(model):
    titles = {u['unit_id']: u['title'] for u in model['units']}
    rows = [[p['summary'], p['point_id'], p['source_id'] + '：' + '、'.join(p['fragment_ids']), '主线' if p['track'] == 'main' else '支线',
             '、'.join(titles[x] for x in p['unit_ids']),
             '、'.join(x['number'] for x in p['lessons']),
             '；'.join(x['number'] + '：' + ('、'.join(x['block_ids']) or '待发布') for x in p['lessons']),
             '；'.join(x['number'] + '：' + x['degree'] for x in p['lessons']), p['reason']]
            for p in coverage_rows(model)]
    return '\n\n## 材料覆盖\n\n' + markdown_table(['原文要点', '要点 ID', '来源与片段', '安排', '对应知识点', '课程', '教学块', '覆盖程度', '理由'], rows) if rows else ''


def validate_teaching(decision, lesson, model):
    units = {u['unit_id']: u for u in model['units']}
    required = set(lesson['teaches_unit_ids']) - {u for u, p in model['unit_progress'].items() if p['skip']}
    seen = set(); objectives = {u: set() for u in required}; covered = set(); explained_units = set()
    for point in decision['teaching_points']:
        uid = point['unit_id']
        if uid not in units or uid not in lesson['teaches_unit_ids']: raise ValueError('教学块知识点不属于本课')
        for block in point.get('blocks', []):
            if 'parts' in block:
                from .learning_teaching_layout import render_parts
                block['text'] = render_parts(block['parts'])
            if not block['text'].strip(): raise ValueError('教学块正文不能为空')
            if block['block_id'] in seen: raise ValueError('教学块 ID 重复')
            seen.add(block['block_id'])
            valid_objectives = set(range(len(units[uid]['learning_objectives'])))
            if not set(block['objective_indices']) <= valid_objectives: raise ValueError('教学块引用未知学习目标')
            allowed = {p['point_id'] for p in model.get('material_points', []) if uid in p['unit_ids']}
            if not set(block['point_ids']) <= allowed: raise ValueError('教学块引用未知原文要点')
            if block['coverage'] == 'explained':
                if decision.get('schema_version') == '7.0' and not any(
                    p['type'] not in ('quote', 'material_quote') for p in block['parts']):
                    raise ValueError('引用块不能单独算作充分讲解，须补充观点解释')
                explained_units.add(uid)
                objectives.setdefault(uid, set()).update(block['objective_indices']); covered.update(block['point_ids'])
        point['text'] = render_teaching_blocks(point)
    for uid in required:
        if uid not in explained_units: raise ValueError('本课知识点须有充分讲解，不能仅提及')
        if objectives[uid] != set(range(len(units[uid]['learning_objectives']))):
            raise ValueError('本课学习目标缺少充分讲解的教学块')
    needed = {p['point_id'] for p in model.get('material_points', []) if required.intersection(p['unit_ids'])}
    if not needed <= covered: raise ValueError('本课原文要点未充分讲解')


def render_teaching_blocks(point):
    """One canonical v5 body renderer for publication and verification."""
    from .learning_teaching_layout import render_parts
    return '\n\n'.join('### ' + BLOCK_LABELS[b['kind']] + '\n\n' + (render_parts(b['parts']) if 'parts' in b else b['text']) for b in point.get('blocks', []))


def feedback_markdown(feedback=None):
    feedback = feedback or {}
    difficulty = feedback.get('difficulty', '') if feedback.get('difficulty_filled') else ''
    comment = feedback.get('comment', '')
    return ('\n## 学习反馈\n\n### 1. 本节的认知难度（可选；留空默认 B）\n\n'
            '- A：过于简单\n- B：难度适中\n- C：过于困难\n\n'
            '<!-- answer:LEARNER_DIFFICULTY:start -->\n> **我的选择：**\n>\n> ' + difficulty +
            '\n<!-- answer:LEARNER_DIFFICULTY:end -->\n\n'
            '### 2. 疑问或其他反馈（可选；没有则留空）\n\n'
            '<!-- answer:LEARNER_COMMENT:start -->\n> **我的反馈：**\n>\n> ' + comment.replace('\n', '\n> ') +
            '\n<!-- answer:LEARNER_COMMENT:end -->\n')


def normalize_feedback(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'difficulty', 'comment'}: raise ValueError('学习反馈字段无效')
    d, c = value.get('difficulty', ''), value.get('comment', '')
    if not isinstance(d, str) or not isinstance(c, str): raise ValueError('学习反馈应为文本')
    d = d.strip().upper(); c = c.strip()
    if d not in ('', 'A', 'B', 'C'): raise ValueError('认知难度须为 A/B/C 或留空')
    if re.search(r'<!--\s*(?:answer|feedback):', c): raise ValueError('学习反馈不能包含保留标记')
    return {'difficulty': d or 'B', 'comment': c, 'difficulty_filled': bool(d), 'comment_filled': bool(c)}


def teaching_template(uid, objectives, material_points, evidence_ids):
    """Suggest separate targets; only the agent may mark actual explanation."""
    blocks = [{'block_id': f'BLOCK-{uid}-OBJ-{i}', 'kind': 'explanation', 'text': '',
               'objective_indices': [i], 'point_ids': [], 'coverage': 'mentioned'}
              for i in range(len(objectives))]
    blocks.extend({'block_id': f'BLOCK-{uid}-POINT-{i}', 'kind': 'explanation', 'text': '',
                   'objective_indices': [], 'point_ids': [p['point_id']], 'coverage': 'mentioned'}
                  for i, p in enumerate(material_points) if uid in p['unit_ids'])
    blocks = blocks or [{'block_id': 'BLOCK-' + uid, 'kind': 'explanation', 'text': '',
                         'objective_indices': [], 'point_ids': [], 'coverage': 'mentioned'}]
    for block in blocks: block['parts'] = [{'type': 'paragraph', 'text': ''}]
    return {'unit_id': uid, 'text': '', 'external_explanation': False, 'evidence_ids': evidence_ids, 'blocks': blocks}
