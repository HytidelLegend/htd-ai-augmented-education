"""Deterministic exercise feedback and recoverable chat delivery gate."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from .structured_io import json_digest, read_json, validate_json_schema, write_json
from .timestamp import iso_timestamp

TRANSITIONS = {
    'pending': {'rendering', 'paused_error'},
    'rendering': {'awaiting_display', 'paused_error'},
    'awaiting_display': {'delivered', 'paused_error'},
    'paused_error': {'rendering'},
    'delivered': set(),
}
SCHEMA = Path(__file__).resolve().parents[1] / 'references/interactive-tutor-review-delivery-v1.schema.json'


def validate_checkpoint(record):
    """Validate every stage, including interrupted rendering and failure records."""
    validate_json_schema(record, SCHEMA)
    state = 'pending'
    for item in record['history']:
        if item['from'] != state or item['to'] not in TRANSITIONS[state]:
            raise ValueError('批改反馈交付迁移历史损坏')
        state = item['to']
    if state != record['state'] or (state == 'delivered') != bool(record['delivered_at']):
        raise ValueError('批改反馈交付状态不一致')


def move(record, state):
    if state not in TRANSITIONS.get(record['state'], set()):
        raise ValueError('非法批改反馈交付状态迁移')
    record['history'].append({'from': record['state'], 'to': state, 'at': iso_timestamp()})
    record['state'] = state


def fingerprint(lesson):
    return json_digest({k: lesson[k] for k in ('lesson_id', 'number', 'title', 'content', 'answers', 'review')})


def question_feedback(question, score, answer):
    points = list(dict.fromkeys(p for values in question['expected_points'].values() for p in values))
    explanation = score.get('reference_explanation', '').strip() or '；'.join(points) or score['feedback']
    difference = score.get('difference_notes', '').strip()
    if not difference:
        if question['type'] == 'multiple_choice':
            difference = ('所选选项与参考答案一致。' if all(v == 1 for v in score['unit_scores'].values())
                          else f"你选择的是 {answer}，参考选项是 {chr(65 + question['correct_index'])}。")
        else:
            difference = '；'.join(f"{m['weakness']}；{m['correction']}" for m in score['misconceptions'])
            if not difference:
                difference = '评分点均已覆盖；不同表达的具体判断见批改意见。'
    reference = question['reference_answer']
    if question['type'] == 'multiple_choice':
        # Show both the stored full reference and the actual correct option.
        reference = f"{chr(65 + question['correct_index'])}. {question['options'][question['correct_index']]}\n\n{reference}"
    return reference, explanation, difference


def render(lesson):
    review = lesson['review']
    scores = {s['question_id']: s for s in review['scores']}
    lines = [f"# 课程 {lesson['number']}｜习题批改", '']
    for i, question in enumerate(lesson['content']['questions'], 1):
        score = scores[question['question_id']]
        answer = lesson['answers'][question['question_id']]
        reference, explanation, difference = question_feedback(question, score, answer)
        values = list(score['unit_scores'].values())
        judgment = '正确' if all(v == 1 for v in values) else '错误' if all(v == 0 for v in values) else '部分正确／不完整'
        lines += [f"## 第 {i} 题：{question['prompt']}", '']
        if question['type'] == 'multiple_choice':
            lines += [f'{chr(65 + j)}. {option}' for j, option in enumerate(question['options'])] + ['']
        lines += ['**你的作答：**', '', *('> ' + s for s in answer.splitlines()), '',
                  '**作答情况：** ' + judgment + '；' + '；'.join(f'{uid}：{value:.0%}' for uid, value in score['unit_scores'].items()), '',
                  '**批改意见：** ' + score['feedback'], '', '**完整参考答案：**', '', reference, '',
                  '**简略解释：** ' + explanation, '', '**与参考答案的差异：** ' + difference, '']
    lines += ['## 本课总结', '', '**掌握优点：** ' + review['strengths'], '',
              '**需要加强：** ' + review['weaknesses'], '']
    return '\n'.join(lines)


def prepare(lesson, run_dir=None):
    digest = fingerprint(lesson)
    previous = lesson.get('review_delivery')
    failure_path = Path(run_dir) / 'review-delivery' / digest / 'state.json' if run_dir else None
    if previous is None and failure_path and failure_path.is_file():
        checkpoint = read_json(failure_path)
        validate_checkpoint(checkpoint)
        if checkpoint['review_sha256'] != digest or checkpoint['lesson_id'] != lesson['lesson_id']:
            raise ValueError('批改反馈交付检查点与当前课程不一致')
        if checkpoint['state'] in ('pending', 'rendering', 'paused_error'):
            previous = checkpoint
    if previous and previous['review_sha256'] == digest:
        validate_checkpoint(previous)
        if previous['lesson_id'] != lesson['lesson_id']:
            raise ValueError('批改反馈交付检查点与当前课程不一致')
        if previous['state'] in ('pending', 'rendering', 'paused_error'):
            record = copy.deepcopy(previous)
            if record['state'] == 'rendering':
                record['error'] = '上次反馈生成中断，重新校验并渲染'
                move(record, 'paused_error')
            move(record, 'rendering')
        else:
            verify_record(lesson, previous)
            return previous
    else:
        record = {'schema_version': '1.0', 'lesson_id': lesson['lesson_id'], 'review_sha256': digest,
                  'state': 'pending', 'history': [], 'delivered_at': None}
        move(record, 'rendering')
    try:
        markdown = render(lesson)
        record['feedback_sha256'] = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
        move(record, 'awaiting_display')
        record.pop('error', None)
    except Exception as exc:
        record['error'] = str(exc)
        move(record, 'paused_error')
        lesson['review_delivery'] = record
        if failure_path:
            write_json(failure_path, record)
        raise
    lesson['review_delivery'] = record
    return record


def verify_record(lesson, record):
    validate_checkpoint(record)
    if record['state'] not in TRANSITIONS or record['lesson_id'] != lesson['lesson_id']:
        raise ValueError('批改反馈交付检查点损坏')
    if record['review_sha256'] != fingerprint(lesson):
        raise ValueError('批改反馈版本已变化')
    markdown = render(lesson)
    if record.get('feedback_sha256') != hashlib.sha256(markdown.encode('utf-8')).hexdigest():
        raise ValueError('批改反馈正文哈希不一致')
    return markdown


def directory(model, record):
    # Hash directories avoid incorporating caller-controlled lesson IDs into paths.
    return Path(model['run_dir']) / 'review-delivery' / record['review_sha256']


def publications(model):
    updates = {}
    for lesson in model['lessons']:
        record = lesson.get('review_delivery')
        if not record:
            continue
        markdown = verify_record(lesson, record)
        updates[directory(model, record) / 'state.json'] = record
        updates[directory(model, record) / 'feedback.md'] = markdown
    return updates


def pending(model):
    lesson = next((l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id']), None)
    if not lesson or not lesson.get('review'):
        return None
    record = prepare(lesson, model['run_dir'])
    if record['state'] == 'delivered':
        return None
    return {'status': 'feedback_delivery_required', 'teaching_status': model['state'],
            'lesson_id': lesson['lesson_id'], 'review_sha256': record['review_sha256'],
            'feedback_sha256': record['feedback_sha256'], 'feedback_markdown': render(lesson),
            'message': '请原样展示批改反馈，再调用 deliver-feedback 留痕；随后同一轮继续学习。'}


def acknowledge(model, lesson_id, review_sha256, feedback_sha256):
    lesson = next((l for l in model['lessons'] if l['lesson_id'] == lesson_id), None)
    if lesson is None or lesson_id != model['current_lesson_id'] or not lesson.get('review'):
        raise ValueError('交付回执必须对应当前已批改课程')
    record = prepare(lesson, model['run_dir'])
    if (review_sha256, feedback_sha256) != (record['review_sha256'], record['feedback_sha256']):
        raise ValueError('批改反馈交付回执已过期')
    if record['state'] != 'delivered':
        move(record, 'delivered')
        record['delivered_at'] = iso_timestamp()
    return {'status': model['state'], 'lesson_id': lesson_id, 'delivery_status': 'delivered'}


def verify_logs(model):
    for lesson in model['lessons']:
        record = lesson.get('review_delivery')
        if record:
            markdown = verify_record(lesson, record)
            path = directory(model, record)
            if read_json(path / 'state.json') != record or (path / 'feedback.md').read_text(encoding='utf-8') != markdown:
                raise ValueError('批改反馈交付日志与权威数据不一致')
