"""Structured lesson parts, deterministic Markdown, and recoverable layout checks."""
import copy
import re
from pathlib import Path
from .structured_io import validate_json_schema, json_digest
from .workflow_checkpoint import WorkflowCheckpoint

ROOT = Path(__file__).resolve().parents[2]
TRANSITIONS = {
    'prepared': ('validating',), 'validating': ('rendering', 'paused_error'),
    'rendering': ('verifying', 'paused_error'), 'verifying': ('completed', 'paused_error'),
    'paused_error': ('validating',), 'completed': (),
}


def render_parts(parts):
    rendered = []
    for part in parts:
        kind = part['type']
        texts = [value for value in part.values() if isinstance(value, str)] + part.get('items', [])
        if any(re.search(r'<!--\s*(?:answer|feedback):', value) for value in texts):
            raise ValueError('讲解内容不能包含作答或反馈保留标记')
        if kind == 'paragraph':
            text = part['text'].strip()
            if not text: raise ValueError('段落不能为空')
            # Preserve inline emphasis; section headings and answer markers are not prose.
            if re.search(r'^\s*(?:#{1,6}\s|```|~~~)|<!--\s*(?:answer|feedback):', text, re.M):
                raise ValueError('段落不能包含标题、代码围栏或作答标记，请使用对应内容类型')
        elif kind in ('unordered_list', 'ordered_list'):
            rows = []
            for index, item in enumerate(part['items'], 1):
                if not item.strip(): raise ValueError('列表项不能为空')
                prefix = f'{index}. ' if kind == 'ordered_list' else '- '
                lines = item.strip().splitlines()
                rows.append(prefix + lines[0] + ''.join('\n' + ' ' * len(prefix) + line for line in lines[1:]))
            text = '\n'.join(rows)
        elif kind == 'formula':
            if not part['latex'].strip() or '$$' in part['latex']: raise ValueError('公式正文无效')
            text = '$$\n' + part['latex'].strip() + '\n$$'
        elif kind == 'code':
            if not part['code'].strip(): raise ValueError('代码不能为空')
            longest = max((len(m[0]) for m in re.finditer(r'`+', part['code'])), default=0)
            fence = '`' * max(3, longest + 1)
            text = fence + part.get('language', '') + '\n' + part['code'].rstrip('\n') + '\n' + fence
        elif kind == 'material_quote':
            if not part.get('resolved'): raise ValueError('材料引用尚未绑定证据，请先校验课程')
            text = render_parts([{'type': 'quote', 'text': part['resolved']['text'], 'source': part['resolved']['source']}])
        elif kind == 'quote':
            if not part['text'].strip() or not part['source'].strip(): raise ValueError('引文和来源不能为空')
            text = '\n'.join('> ' + line for line in part['text'].strip().splitlines())
            source = part['source'].strip().replace('\n', ' ')
            url = part.get('url')
            if url:
                if not re.fullmatch(r'https?://[^\s<>]+', url): raise ValueError('引文出处链接无效')
                source = '[' + source.replace('[', '\\[').replace(']', '\\]') + '](' + url.replace('(', '%28').replace(')', '%29') + ')'
            text += '\n>\n> —— ' + source
        else:
            raise ValueError('未知讲解内容类型')
        if part.get('caption', '').strip(): text += '\n\n' + part['caption'].strip()
        rendered.append(text)
    if not rendered: raise ValueError('讲解内容不能为空')
    return '\n\n'.join(rendered)


def normalize(decision, run_dir=None, *, evidence=None, root=ROOT):
    """Validate the small source template; derived text is never another input truth."""
    if decision.get('schema_version') not in ('6.0', '7.0'): return copy.deepcopy(decision)
    flow = WorkflowCheckpoint(TRANSITIONS, Path(run_dir) / 'teaching-layout' / json_digest(decision) if run_dir else None, resume=True)
    result = copy.deepcopy(decision)
    try:
        if flow.state not in ('prepared', 'paused_error'):
            flow.move('paused_error', reason='恢复中断排版')
        flow.move('validating')
        version = decision['schema_version'].split('.')[0]
        validate_json_schema(result, ROOT / f'utils/references/interactive-tutor-lesson-decision-v{version}.schema.json')
        if version == '7':
            from .learning_teaching_context import resolve_quotes
            result = resolve_quotes(result, evidence or [], root)
        flow.move('rendering')
        for point in result['teaching_points']:
            for block in point['blocks']:
                block['text'] = render_parts(block['parts'])
        flow.move('verifying')
        for point in result['teaching_points']:
            for block in point['blocks']:
                if block['text'] != render_parts(block['parts']): raise ValueError('排版结果不一致')
        flow.move('completed')
        return result
    except Exception:
        if 'paused_error' in TRANSITIONS.get(flow.state, ()): flow.move('paused_error')
        raise
