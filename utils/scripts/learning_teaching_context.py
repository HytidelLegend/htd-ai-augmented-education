"""Evidence-bound material quotes and small, resumable context/layout reviews."""
import copy
import json
import re
from pathlib import Path

from .markdown_structure import sha256_file
from .structured_io import json_digest, read_json, write_text_atomic, validate_json_schema
from .workflow_checkpoint import WorkflowCheckpoint

ROOT = Path(__file__).resolve().parents[2]
TRANSITIONS = {
    'prepared': ('validating',),
    'validating': ('checking_context_layout', 'paused_error'),
    'checking_context_layout': ('review_required', 'completed', 'paused_error'),
    'review_required': ('validating_review', 'paused_error'),
    'validating_review': ('completed', 'revision_required', 'review_required', 'paused_error'),
    'revision_required': (), 'completed': ('validating',), 'paused_error': ('validating',),
}


def write_json(path, value):
    """A interrupted write must not leave a partial template or archived judgment."""
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def quote_snapshot(evidence, root):
    if evidence.get('evidence_kind') != 'source_excerpt':
        raise ValueError('材料引用必须绑定原材料证据，不能引用导航说明')
    path = Path(root) / evidence['source_path']
    if sha256_file(path) != evidence['source_sha256']:
        raise ValueError('材料引用来源哈希已变化')
    loc = evidence['locator']
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    start, end = loc['start_line'], loc['end_line']
    if not 1 <= start <= end <= len(lines):
        raise ValueError('材料引用定位无效')
    text = '\n'.join(lines[start - 1:end]).strip()
    if not text or text != evidence['text']:
        raise ValueError('材料引用证据与原文切片不一致')
    title = (loc.get('parent_heading_chain') or [path.stem])[0]
    return {'text': text, 'source': f'{title}｜{loc["heading"]}，第 {start}–{end} 行',
            'evidence': copy.deepcopy(evidence)}


def resolve_quotes(decision, evidence, root):
    result = copy.deepcopy(decision)
    by_id = {e['evidence_id']: e for e in evidence}
    for point in result['teaching_points']:
        for block in point['blocks']:
            for part in block['parts']:
                if part['type'] != 'material_quote': continue
                eid = part['evidence_id']
                if eid not in by_id or eid not in point['evidence_ids']:
                    raise ValueError('材料引用须关联本教学知识点的有效证据')
                if by_id[eid].get('unit_id') != point['unit_id']:
                    raise ValueError('材料引用证据不属于本教学知识点')
                snapshot = quote_snapshot(by_id[eid], root)
                if part['edited']:
                    if not part.get('edited_text', '').strip(): raise ValueError('删改引用不能为空')
                    if part['edited_text'].lstrip().startswith('（有删改）'):
                        raise ValueError('删改标记由脚本生成，无需手写')
                    snapshot['text'] = '（有删改）' + part['edited_text'].strip()
                part['resolved'] = snapshot  # Always recomputed; never trust supplied derived fields.
    return result


def verify_quotes(decision, root):
    for point in decision['teaching_points']:
        for block in point.get('blocks', []):
            for part in block.get('parts', []):
                if part['type'] != 'material_quote': continue
                expected = quote_snapshot(part['resolved']['evidence'], root)
                if part['evidence_id'] != expected['evidence']['evidence_id']:
                    raise ValueError('材料引用证据 ID 不一致')
                if part['edited']: expected['text'] = '（有删改）' + part['edited_text'].strip()
                if part['resolved'] != expected: raise ValueError('材料引用派生正文或来源不一致')


def legacy_findings(decision):
    """Preserve the evidence rules frozen in already published v7 records."""
    items = []
    for point in decision['teaching_points']:
        has_quote = False
        for block in point['blocks']:
            reasons = []
            for part in block['parts']:
                if part['type'] == 'material_quote':
                    has_quote = True
                    if part['edited']:
                        reasons.append('检查删改引文是否保留原意，且未把讲者举例变成已核实事实')
                if part['type'] != 'paragraph': continue
                text = part['text']
                if not has_quote and re.search(r'材料中|原文中|讲者(?:引用|举|说|认为)|这段(?:材料|话)', text):
                    reasons.append('检查未读材料的学习者是否需要前置背景及材料引用块')
                if re.search(r'(?:分成|分为|以下)[一二三四五六七八九十\d]+步|先[^。\n]+再[^。\n]+(?:最后|然后)', text):
                    reasons.append('检查连续步骤是否应改用 ordered_list，并将示例另起段落')
            if reasons: items.append({'block_id': block['block_id'], 'reason': '；'.join(dict.fromkeys(reasons))})
    return items


def findings(decision, *, version=2):
    if version == 1: return legacy_findings(decision)
    if version != 2: raise ValueError('未知上下文排版复核规则版本')
    items = []
    for point in decision['teaching_points']:
        has_quote = False
        for block in point['blocks']:
            reasons = []
            for part in block['parts']:
                kind = part['type']
                if kind == 'material_quote':
                    has_quote = True
                    reasons.append('检查引用是否切合当前讲解，且未读材料的学习者已有必要背景')
                    if part['edited']:
                        reasons.append('检查删改引文是否保留原意，且未把讲者举例变成已核实事实')
                texts = [part['text']] if kind == 'paragraph' else part.get('items', [])
                for text in texts:
                    if re.search(r'材料中|原文中|讲者(?:引用|举|说|认为)|这段(?:材料|话)', text):
                        reasons.append('检查未读材料的学习者是否需要前置背景及材料引用块' if not has_quote else
                                       '检查已有引用是否支持当前材料指代，不能用不相关引用替代背景')
                    if re.search(r'(?:分成|分为|以下)[一二三四五六七八九十\d]+步|先[^。\n]+再[^。\n]+(?:最后|然后)|(?:步骤|流程)[：:]\s*(?:1[.、）)]|一[、）)])', text):
                        reasons.append('检查连续步骤是否应改用 ordered_list，并将示例另起段落')
                    if re.search(r'(?:逐字稿|材料|原文)[^。\n]*；[^。\n]*(?:核实|历史事实)', text):
                        reasons.append('检查材料性质和核实范围是否应拆为清楚的独立句子；保留引文原标点')
            if reasons: items.append({'block_id': block['block_id'], 'reason': '；'.join(dict.fromkeys(reasons))})
    return items


def review_basis(decision, evidence):
    clean = copy.deepcopy(decision)
    for point in clean['teaching_points']:
        point['text'] = ''
        for block in point['blocks']:
            block['text'] = ''
            for part in block['parts']: part.pop('resolved', None)
    return {'lesson_id': decision['lesson_id'], 'decision': clean, 'evidence': evidence, 'policy_version': 2}


def teaching_input(decision):
    """Actual teaching content, independent of block IDs, coverage labels and question/config edits."""
    return [[{k: v for k, v in part.items() if k != 'resolved'}
             for block in point['blocks'] for part in block['parts']]
            for point in decision['teaching_points']]


def validate_review(review, data, pending):
    validate_json_schema(review, ROOT/'utils/references/learning-teaching-review-v1.schema.json')
    if review['lesson_id'] != data['lesson_id'] or review['fingerprint'] != json_digest(data):
        raise ValueError('上下文排版复核已过期，请使用当前模板')
    ids = [x['block_id'] for x in review['items']]
    if len(ids) != len(set(ids)) or set(ids) != {x['block_id'] for x in pending}:
        raise ValueError('上下文排版复核须逐项对应待复核块')
    if any(not x['reason'].strip() for x in review['items']): raise ValueError('上下文排版复核理由不能为空')


def gate_receipt(run, state):
    return {'status': 'teaching_layout_' + state, 'layout_template': str(run/'review.template.json'),
            'layout_packet': str(run/'packet.json'),
            'message': '请修改引用、背景或步骤正文后重新提交，不能只改判断。' if state == 'revision_required' else
                       '补充背景/引用或拆分步骤，或填写小复核后以 --layout-review 重提。'}


def evaluate(decision, evidence, root, run_dir, review=None):
    """Invalid input leaves checkpoints unchanged; persistence failures pause and recover."""
    if decision.get('schema_version') != '7.0': return None, None
    try:
        return _evaluate(decision, evidence, root, run_dir, review)
    except OSError:
        data = review_basis(decision, evidence)
        run = Path(run_dir)/'teaching-context'/decision['lesson_id']/json_digest(data)
        if (run/'state.json').exists():
            flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
            if 'paused_error' in TRANSITIONS[flow.state]: flow.move('paused_error')
        raise


def _evaluate(decision, evidence, root, run_dir, review):
    data = review_basis(decision, evidence)
    clean = data['decision']
    fingerprint = json_digest(data)
    run = Path(run_dir) / 'teaching-context' / decision['lesson_id'] / fingerprint
    pending = findings(decision)
    template = {'schema_version': '1.0', 'lesson_id': decision['lesson_id'], 'fingerprint': fingerprint,
                'short_complete_reason': '', 'items': [{'block_id': x['block_id'], 'judgment': 'needs_expansion', 'reason': ''} for x in pending]}
    if review is not None:
        validate_review(review, data, pending)
    if run.parent.exists():
        for prior in run.parent.iterdir():
            if prior == run or not (prior/'state.json').exists(): continue
            previous_flow = WorkflowCheckpoint(TRANSITIONS, prior, resume=True, restart_completed=False)
            if previous_flow.state != 'revision_required': continue
            previous = read_json(prior/'packet.json')
            previous_data = {k: v for k, v in previous.items() if k not in ('fingerprint', 'findings')}
            if json_digest(previous_data) != previous['fingerprint']: raise ValueError('历史复核依据损坏')
            archived = read_json(prior/'review.json')
            validate_review(archived, previous_data, previous['findings'])
            if not any(x['judgment'] == 'needs_expansion' for x in archived['items']):
                raise ValueError('历史需修订状态与判断不一致')
            if teaching_input(previous['decision']) == teaching_input(clean):
                return gate_receipt(prior, 'revision_required'), None
    flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
    if (run/'packet.json').exists() and read_json(run/'packet.json') != {
        **data, 'fingerprint': fingerprint, 'findings': pending}:
        raise ValueError('上下文排版复核日志依据损坏，拒绝覆盖')
    if flow.state == 'completed':
        if (run/'result.json').is_file():
            record = read_json(run/'result.json')
            if record['basis'] != data: raise ValueError('上下文排版复核依据损坏')
            verify_record(decision, record, root, run_dir=run_dir)
            return None, record
        flow.move('validating')  # Recover interruption between automatic completion and result persistence.
        if (run/'review.json').exists():
            review = read_json(run/'review.json')
            validate_review(review, data, pending)
    if flow.state == 'revision_required':
        archived = read_json(run/'review.json')
        validate_review(archived, data, pending)
        if not any(x['judgment'] == 'needs_expansion' for x in archived['items']):
            raise ValueError('需修订状态与归档判断不一致')
        return gate_receipt(run, 'revision_required'), None
    if flow.state in ('validating_review', 'paused_error') and (run/'review.json').exists():
        archived = read_json(run/'review.json')
        validate_review(archived, data, pending)
        if flow.state == 'paused_error':
            flow.move('validating'); flow.move('checking_context_layout'); flow.move('review_required')
            flow.move('validating_review')
        review = archived  # Replay the persisted judgment; a new judgment cannot erase it.
    elif flow.state == 'validating_review':
        flow.move('review_required')  # Interrupted before any judgment was durably archived.
    if flow.state in ('prepared', 'paused_error'): flow.move('validating')
    if flow.state == 'validating':
        flow.move('checking_context_layout')
    if flow.state == 'checking_context_layout':
        write_json(run/'packet.json', {**data, 'fingerprint': fingerprint, 'findings': pending})
        if not (run/'review.template.json').exists(): write_json(run/'review.template.json', template)
        if not pending:
            record = {'basis': data, 'fingerprint': fingerprint, 'findings': [], 'review': None}
            write_json(run/'result.json', record)
            flow.move('completed')
            return None, record
        flow.move('review_required')
    if flow.state == 'review_required':
        if review is None:
            return gate_receipt(run, 'review_required'), None
        flow.move('validating_review')
        write_json(run/'review.json', review)
    if flow.state == 'validating_review':
        if review is None:
            flow.move('review_required')
            return gate_receipt(run, 'review_required'), None
        if any(x['judgment'] == 'needs_expansion' for x in review['items']):
            flow.move('revision_required')
            return evaluate(decision, evidence, root, run_dir)
    record = {'basis': data, 'fingerprint': fingerprint, 'findings': pending, 'review': review}
    write_json(run/'result.json', record)
    if flow.state != 'completed': flow.move('completed')
    return None, record


def verify_record(decision, record, root, *, run_dir=None):
    verify_quotes(decision, root)
    if json_digest(record['basis']) != record['fingerprint']:
        raise ValueError('上下文排版复核指纹不一致')
    if record['basis']['lesson_id'] != decision['lesson_id']:
        raise ValueError('上下文排版复核对应课程错误')
    resolved = resolve_quotes(decision, record['basis']['evidence'], root)
    if resolved['teaching_points'] != decision['teaching_points']:
        raise ValueError('材料引用与上下文复核证据不一致')
    clean = review_basis(decision, record['basis']['evidence'])['decision']
    # Question options can be rotated after this gate; compare only teaching inputs.
    if clean['teaching_points'] != record['basis']['decision']['teaching_points']:
        raise ValueError('上下文排版复核教学正文不一致')
    if findings(decision, version=record['basis'].get('policy_version', 1)) != record['findings']:
        raise ValueError('上下文排版复核项目不一致')
    if record['findings']:
        review = record['review']
        validate_json_schema(review, ROOT/'utils/references/learning-teaching-review-v1.schema.json')
        if review['lesson_id'] != decision['lesson_id'] or review['fingerprint'] != record['fingerprint']:
            raise ValueError('上下文排版复核绑定不一致')
        ids = [x['block_id'] for x in review['items']]
        if len(ids) != len(set(ids)) or set(ids) != {x['block_id'] for x in record['findings']}:
            raise ValueError('上下文排版复核项目缺失')
        if any(x['judgment'] != 'sufficient' or not x['reason'].strip() for x in review['items']):
            raise ValueError('上下文排版复核未通过')
    if run_dir is not None:
        run = Path(run_dir)/'teaching-context'/decision['lesson_id']/record['fingerprint']
        if not (run/'state.json').is_file(): raise ValueError('上下文排版复核检查点缺失')
        flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
        if flow.state != 'completed' or read_json(run/'result.json') != record:
            raise ValueError('上下文排版复核检查点或结果不一致')
        packet = read_json(run/'packet.json')
        if packet != {**record['basis'], 'fingerprint': record['fingerprint'], 'findings': record['findings']}:
            raise ValueError('上下文排版复核日志依据不一致')
        if record['findings'] and read_json(run/'review.json') != record['review']:
            raise ValueError('上下文排版归档判断与已接受结果不一致')
