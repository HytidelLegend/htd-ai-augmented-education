"""Deterministic teaching-body metrics and resumable, fingerprint-bound review."""
from pathlib import Path
import copy
import html
import json
import re

from .markdown_structure import FENCE, parse_atx_heading
from .structured_io import json_digest, read_json, validate_json_schema, write_text_transaction
from .workflow_checkpoint import WorkflowCheckpoint
from .learning_content import render_teaching_blocks

ROOT = Path(__file__).resolve().parents[2]
TRANSITIONS = {
    'prepared': ('measuring',),
    'measuring': ('review_required', 'completed'),
    'review_required': ('validating_review', 'measuring'),
    'validating_review': ('completed', 'revision_required', 'review_required', 'measuring'),
    'revision_required': ('measuring', 'validating_review'),  # Old checkpoints remain readable; new decisions require changed body.
    'completed': ('measuring',),
}


def teaching_body(decision):
    if decision.get('schema_version') in ('5.0', '6.0', '7.0'):
        for point in decision['teaching_points']:
            from .learning_teaching_layout import render_parts
            for block in point.get('blocks', []):
                if 'parts' in block and block['text'] != render_parts(block['parts']):
                    raise ValueError('教学块派生正文不一致')
            if point['text'] != render_teaching_blocks(point):
                raise ValueError('课程讲解正文哈希或教学块派生正文不一致')
    text = '\n\n'.join(p['text'] for p in decision['teaching_points'])
    if decision.get('schema_version') in ('6.0', '7.0'): return text
    # Preserve historical terminology conversion without rewriting v6 source content.
    return re.sub(r'\bunit(?:s)?\b', '知识点', re.sub(r'\blesson(?:s)?\b', '课程', text, flags=re.I), flags=re.I)


def section(markdown):
    """Read the exact level-two teaching section; code headings are ordinary text."""
    active = False; lines = []; fence = None
    for line in markdown.splitlines():
        marker = FENCE.match(line)
        if marker:
            token = marker[1]
            if fence is None: fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip(): fence = None
        heading = parse_atx_heading(line) if fence is None and not marker else None
        if heading and heading[0] <= 2:
            if active: break
            active = heading == (2, '课程讲解')
            continue
        if active: lines.append(line)
    if not active: raise ValueError('讲义缺少课程讲解小节')
    return '\n'.join(lines)


def metrics(text, *, version=2):
    """Count visible non-whitespace characters, including English, code and math."""
    count = lambda value: len(re.sub(r'\s+', '', value))
    protected = {}; code_chars = 0; formula_chars = 0
    kinds = {}
    def protect(value, kind=None):
        token = f'\x00Q{len(protected)}\x00'
        protected[token] = value
        kinds[token] = kind
        return token
    lines = []; fence = None; setext_end = -1; indented = False
    raw_lines = text.splitlines()
    reference_labels = set()
    for index, line in enumerate(raw_lines):
        marker = FENCE.match(line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token; continue
            if token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None; continue
            if version == 1: continue
        if fence is not None:
            lines.append(protect(line, 'code')); code_chars += count(line); continue
        if version >= 2:
            is_indented = line.startswith(('    ', '\t'))
            if is_indented and (indented or index == 0 or not raw_lines[index-1].strip()):
                value = line[1:] if line.startswith('\t') else line[4:]
                lines.append(protect(value, 'code')); indented = True; continue
            if line.strip(): indented = False
            if index == setext_end: continue
            if line.strip() and index+1 < len(raw_lines) and re.fullmatch(r' {0,3}(?:=+|-+)\s*', raw_lines[index+1]):
                setext_end = index+1; continue
        heading_line = re.sub(r'^ {0,3}(?:>\s*)+', '', line) if version >= 2 else line
        if parse_atx_heading(heading_line): continue
        reference = re.match(r'^\s*\[([^\]]+)\]:\s*\S+', line)
        if reference:
            reference_labels.add(reference[1].casefold()); continue
        if re.fullmatch(r'\s*(?:\|?\s*:?-+:?\s*)+\|?\s*', line): continue
        if re.fullmatch(r'\s*(?:[-*_]\s*){3,}', line): continue
        lines.append(line)
    visible = '\n'.join(lines)
    def code_match(match):
        nonlocal code_chars
        value = match[2]; code_chars += count(value)
        return protect(value, 'code')
    visible = re.sub(r'(`+)(.*?)\1', code_match, visible, flags=re.S)
    def math_match(match):
        nonlocal formula_chars
        value = next((x for x in match.groups() if x is not None), '')
        formula_chars += count(value)
        return protect(value, 'formula')
    visible = re.sub(r'\$\$(.*?)\$\$|(?<!\$)\$([^$\n]+)\$|\\\[(.*?)\\\]|\\\((.*?)\\\)',
                     math_match, visible, flags=re.S)
    if version >= 2:
        visible = re.sub(r'\\([\\`*_{}\[\]()#+.!>~-])', lambda m: protect(m[1]), visible)
    # Strip link destinations with balanced parentheses, retaining the label.
    start = re.compile(r'!?\[([^\]\n]*)\]\(')
    cursor = 0; chunks = []
    while match := start.search(visible, cursor):
        depth = 1; end = match.end()
        while end < len(visible) and depth:
            char = visible[end]
            if char == '\\': end += 2; continue
            if char == '(': depth += 1
            if char == ')': depth -= 1
            end += 1
        if depth: break
        chunks.extend([visible[cursor:match.start()], match[1]]); cursor = end
    visible = ''.join(chunks) + visible[cursor:]
    visible = re.sub(r'!?\[([^\]]*)\]\[[^\]]*\]', r'\1', visible)
    if version >= 2:
        visible = re.sub(r'!?\[([^\]\n]+)\]', lambda m: m[1] if m[1].casefold() in reference_labels else m[0], visible)
    visible = re.sub(r'<(?:https?://|mailto:)[^>]+>', '', visible)
    visible = re.sub(r'<!--.*?-->', '', visible, flags=re.S)
    if version >= 2:
        visible = re.sub(r'<h([1-6])\b[^>]*>.*?</h\1\s*>', '', visible, flags=re.S | re.I)
    visible = re.sub(r'</?[A-Za-z][\w:-]*(?:\s+[^<>]*)?\s*/?>', '', visible)
    visible = re.sub(r'^\s*(?:>\s*)*(?:(?:[-+*]|\d+[.)])\s+)?', '', visible, flags=re.M)
    visible = visible.replace('**', '').replace('__', '').replace('~~', '').replace('|', '')
    visible = re.sub(r'(?<!\w)[*_]|[*_](?!\w)', '', visible)
    visible = re.sub(r'\\([\\`*_{}\[\]()#+.!>~-])', r'\1', visible)
    visible = html.unescape(visible)
    if version >= 2:
        code_chars = sum(count(value) for token, value in protected.items() if token in visible and kinds[token] == 'code')
        formula_chars = sum(count(value) for token, value in protected.items() if token in visible and kinds[token] == 'formula')
    for token, value in protected.items(): visible = visible.replace(token, value)
    return {'body_chars': count(visible), 'code_chars': code_chars, 'formula_chars': formula_chars}


def validate_block_headings(decision):
    for point in decision['teaching_points']:
        for block in point.get('blocks', []):
            fence = None
            for line in block['text'].splitlines():
                marker = FENCE.match(line)
                if marker:
                    token = marker[1]
                    if fence is None: fence = token
                    elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip(): fence = None
                    continue
                heading = parse_atx_heading(line) if fence is None else None
                if heading and heading[0] <= 2: raise ValueError('教学块内标题须使用三级或更深层级，不能结束课程讲解小节')
            if fence is not None: raise ValueError('教学块代码围栏未闭合')


def packet(decision, lesson, model, config):
    validate_block_headings(decision)
    body_metrics = metrics(teaching_body(decision))
    if not body_metrics['body_chars']: raise ValueError('课程讲解可见正文不能为空')
    low = config['lesson'].get('reference_chars_min', 800)
    high = config['lesson'].get('reference_chars_max', 1500)
    units = [u for u in model['units'] if u['unit_id'] in lesson['teaches_unit_ids']]
    points = [p for p in model.get('material_points', []) if set(p['unit_ids']) & set(lesson['teaches_unit_ids'])]
    items = []
    for point in decision['teaching_points']:
        for block in point['blocks']:
            refs = len(block['objective_indices']) + len(block['point_ids'])
            chars = metrics(block['text'])['body_chars']
            if decision.get('schema_version') == '7.0':
                from .learning_teaching_layout import render_parts
                prose = [p for p in block['parts'] if p['type'] not in ('quote', 'material_quote')]
                chars = metrics(render_parts(prose))['body_chars'] if prose else 0
            if block['coverage'] == 'explained' and not chars: raise ValueError('充分讲解的教学块须包含可见正文')
            if block['coverage'] == 'explained' and (chars < 80 or refs > 1):
                items.append({'block_id': block['block_id'], 'unit_id': point['unit_id'],
                              'objective_indices': block['objective_indices'], 'point_ids': block['point_ids'],
                              'reason': '讲解较短或同块关联多个目标/原文要点，需核对实质覆盖'})
    plan_keys = ('lesson_id', 'title', 'chapter_id', 'section_number', 'teaches_unit_ids',
                 'internal_unit_order', 'track', 'prerequisite_providers', 'prerequisite_unit_ids', 'archived', 'skip')
    basis = {'policy_version': 2, 'teaching_points': decision['teaching_points'],
             'lesson': {k: lesson.get(k) for k in plan_keys},
             'course_plan': [{k: l.get(k) for k in plan_keys} for l in model['lessons']],
             'chapters': model.get('chapters', []),
             'units': units, 'material_points': points, 'navigation_hash': model.get('navigation_hash'),
             'config': config, 'skipped_units': sorted(u for u, p in model['unit_progress'].items() if p['skip'])}
    if decision.get('schema_version') == '7.0': basis['decision_version'] = '7.0'
    return copy.deepcopy({'schema_version': '1.0', 'lesson_id': lesson['lesson_id'], 'fingerprint': json_digest(basis),
            'scope': '课程讲解', 'metrics_version': 2, 'basis': basis,
            'body_sha256': json_digest(teaching_body(decision)),
            **body_metrics, 'reference_min': low, 'reference_max': high,
            'short': body_metrics['body_chars'] < low, 'over_reference': body_metrics['body_chars'] > high,
            'learning_objectives': {u['unit_id']: u['learning_objectives'] for u in units},
            'material_points': points, 'review_items': items,
            'teaching_blocks': [{'unit_id': p['unit_id'], **b} for p in decision['teaching_points'] for b in p['blocks']]})


def receipt(run, flow):
    message = ('本次讲解已判定需补充；请修改教学正文后重新调用 publish-lesson，不能仅改复核判断。'
               if flow.state == 'revision_required' else
               '请补充讲解，或填写偏短完整性理由及待复核教学块判断，再用 publish-lesson --quality-review 提交。')
    return {'status': 'teaching_' + flow.state, 'quality_template': str(run/'review.template.json'),
            'quality_packet': str(run/'packet.json'), 'message': message}


def unresolved_body_review(run, data):
    """A config/plan change cannot erase an accepted needs-expansion decision."""
    if not run.parent.exists(): return None
    for prior in run.parent.iterdir():
        if prior == run or not (prior/'state.json').is_file() or not (prior/'review.json').is_file(): continue
        flow = WorkflowCheckpoint(TRANSITIONS, prior, resume=True, restart_completed=False)
        if flow.state != 'revision_required': continue
        previous = read_json(prior/'packet.json')
        if previous.get('body_sha256') == data['body_sha256']:
            validate_json_schema(read_json(prior/'review.json'), ROOT/'utils/references/learning-teaching-review-v1.schema.json')
            result = receipt(prior, flow)
            result['message'] += ' 改变配置或课程计划不能替代正文补充。'
            return result
    return None


def evaluate(decision, lesson, model, config, review=None):
    data = packet(decision, lesson, model, config)
    # Per-fingerprint directories retain old reviews and prevent stale reuse.
    run = Path(model['run_dir'])/'teaching-quality'/lesson['lesson_id']/data['fingerprint']
    template = {'schema_version': '1.0', 'lesson_id': data['lesson_id'], 'fingerprint': data['fingerprint'],
                'short_complete_reason': '', 'items': [{'block_id': x['block_id'], 'judgment': 'needs_expansion', 'reason': ''} for x in data['review_items']]}
    if review is not None:
        validate_json_schema(review, ROOT/'utils/references/learning-teaching-review-v1.schema.json')
        if review['lesson_id'] != data['lesson_id'] or review['fingerprint'] != data['fingerprint']:
            raise ValueError('讲解复核对应的正文、课程计划或配置已变化，请使用新模板')
        expected = {x['block_id'] for x in data['review_items']}
        ids = [x['block_id'] for x in review['items']]
        if set(ids) != expected or len(ids) != len(set(ids)): raise ValueError('讲解复核须逐项对应待复核教学块，不能重复或遗漏')
        if any(not x['reason'].strip() for x in review['items']): raise ValueError('讲解复核理由不能为空')
        if data['short'] and not review['short_complete_reason'].strip(): raise ValueError('偏短讲解须说明学习目标如何充分覆盖，不能只说明知识点数量')
    unresolved = unresolved_body_review(run, data)
    if unresolved: return unresolved, None
    flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
    if flow.state == 'prepared': flow.move('measuring')
    if flow.state == 'validating_review': flow.move('review_required')
    updates = {run/'packet.json': json.dumps(data, ensure_ascii=False, indent=2)+'\n'}
    if not (run/'review.template.json').exists():
        updates[run/'review.template.json'] = json.dumps(template, ensure_ascii=False, indent=2)+'\n'
    write_text_transaction(updates)
    if flow.state == 'revision_required':
        return receipt(run, flow), None
    if flow.state == 'completed' and review is None:
        if (run/'result.json').is_file():
            verify_record(run, data)
            return None, {'packet': data, 'review': read_json(run/'result.json').get('review')}
        flow.move('measuring')  # Interrupted before the automatic result write.
    if flow.state == 'completed': flow.move('measuring')
    required = data['short'] or bool(data['review_items'])
    if flow.state == 'measuring': flow.move('review_required' if required else 'completed')
    if flow.state == 'completed':
        result = {'packet': data, 'review': None}
    elif review is None:
        return receipt(run, flow), None
    else:
        write_text_transaction({run/'review.json': json.dumps(review, ensure_ascii=False, indent=2)+'\n'})
        flow.move('validating_review')
        if any(x['judgment'] == 'needs_expansion' for x in review['items']):
            flow.move('revision_required')
            return receipt(run, flow), None
        result = {'packet': data, 'review': copy.deepcopy(review)}
        write_text_transaction({run/'result.json': json.dumps(result, ensure_ascii=False, indent=2)+'\n'})
        flow.move('completed')
    write_text_transaction({run/'result.json': json.dumps(result, ensure_ascii=False, indent=2)+'\n'})
    return None, result


def verify_record(run, data):
    result = read_json(run/'result.json')
    if result['packet'] != data: raise ValueError('讲解复核统计或指纹不一致')
    validate_result(result)


def validate_result(result):
    data = result['packet']
    if data.get('basis'):
        basis = data['basis']
        if json_digest(basis) != data['fingerprint']: raise ValueError('讲解复核冻结依据或指纹不一致')
        frozen_model = {'units': basis['units'], 'lessons': basis['course_plan'], 'chapters': basis['chapters'],
                        'material_points': basis['material_points'], 'navigation_hash': basis['navigation_hash'],
                        'unit_progress': {uid: {'skip': True} for uid in basis['skipped_units']}}
        version = '6.0' if any('parts' in b for p in basis['teaching_points'] for b in p.get('blocks', [])) else '5.0'
        version = basis.get('decision_version', version)
        frozen_decision = {'schema_version': version, 'teaching_points': basis['teaching_points']}
        if packet(frozen_decision, basis['lesson'], frozen_model, basis['config']) != data:
            raise ValueError('讲解复核统计、目标引用或待复核项与冻结依据不一致')
    if data['short'] or data['review_items']:
        review = result.get('review')
        validate_json_schema(review, ROOT/'utils/references/learning-teaching-review-v1.schema.json')
        if review['fingerprint'] != data['fingerprint'] or review['lesson_id'] != data['lesson_id']:
            raise ValueError('讲解复核指纹不一致')
        if data['short'] and not review['short_complete_reason'].strip(): raise ValueError('偏短完整性理由缺失')
        if {x['block_id'] for x in review['items']} != {x['block_id'] for x in data['review_items']} or len(review['items']) != len(data['review_items']):
            raise ValueError('讲解复核项目不一致')
        if any(x['judgment'] != 'sufficient' or not x['reason'].strip() for x in review['items']): raise ValueError('讲解复核未完成')


def verify_published(lesson, markdown):
    record = lesson.get('teaching_quality')
    if not record: return  # Historical lessons remain readable without migration.
    validate_result(record)
    data = record['packet']
    if data.get('basis') and json_digest(lesson['content']['teaching_points']) != json_digest(data['basis']['teaching_points']):
        raise ValueError('课程教学块或覆盖引用与已验证快照不一致')
    if json_digest(teaching_body(lesson['content'])) != data['body_sha256']:
        raise ValueError('课程讲解正文哈希不一致')
    if section(markdown).strip() != teaching_body(lesson['content']).strip():
        raise ValueError('课程讲解 Markdown 与 JSON 不一致')
    if metrics(section(markdown), version=data.get('metrics_version', 1)) != {k: data[k] for k in ('body_chars', 'code_chars', 'formula_chars')}:
        raise ValueError('课程讲解小节与已验证正文统计不一致')
    if metrics(teaching_body(lesson['content']), version=data.get('metrics_version', 1)) != {k: data[k] for k in ('body_chars', 'code_chars', 'formula_chars')}:
        raise ValueError('课程 JSON 与已验证正文统计不一致')
