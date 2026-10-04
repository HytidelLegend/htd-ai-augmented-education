"""Project-level progress and evidence limits, rendered from authoritative records."""
from pathlib import Path
from .markdown_report import markdown_table
from .structured_io import validate_json_schema, read_json

ROOT = Path(__file__).resolve().parents[2]


def build_report(model, config, aspects):
    active = [l for l in model['lessons'] if not l['archived']]
    progress = {state: sum(l['status'] == state for l in active) for state in ('completed', 'skipped')}
    progress['pending'] = len(active) - sum(progress.values())
    strengths, weaknesses, unverified, next_steps = [], [], [], []
    for aspect in aspects:
        aspect['learning_status'] = model['unit_progress'][aspect['unit_id']]['status']
        evidence = []
        for lesson in active:
            if not lesson.get('review'): continue
            scores = {s['question_id']: s for s in lesson['review']['scores']}
            for question in lesson['content']['questions']:
                if aspect['unit_id'] not in question['unit_ids']: continue
                score = scores[question['question_id']]['unit_scores'][aspect['unit_id']]
                evidence.append({'lesson_id': lesson['lesson_id'], 'question_id': question['question_id'],
                    'type': question['type'], 'score': score,
                    'label': f"课程 {lesson['number']} / {question['question_id']}",
                    'link': f"课程/{lesson['filename']}#feedback-{question['question_id']}"})
        aspect['evidence'] = evidence
        title = aspect['title']
        if not evidence: unverified.append(f'{title}：尚无正式习题评分证据。')
        elif all(e['type'] == 'multiple_choice' for e in evidence):
            unverified.append(f'{title}：目前仅有选择题证据，自主解释、应用与迁移能力尚未验证。')
        else:
            unverified.append(f'{title}：已有问答证据，长期保持与新情境迁移尚未验证。')
        if aspect['classification'] == '良好': strengths.append(f'{title}：当前已评分习题表现良好；结论限于这些习题。')
        if aspect['mastery'] is not None and any(e['score'] < 1 for e in evidence):
            related = [e['weakness'] for e in model['errors'] if e['unit_id'] == aspect['unit_id']]
            weaknesses.append(f"{title}：" + ('；'.join(dict.fromkeys(related)) or '当前评分仍有不足。'))
            next_steps.append(f'{title}：结合错题本纠正方法复习，并补充新的验证题。')
    next_steps.extend(f"继续课程 {l['number']}：{l['title']}。" for l in active if l['status'] == 'pending')
    if not next_steps: next_steps.append('针对未验证能力进行自主解释或新情境练习。')
    path = Path(model['navigation_json'])
    nav = read_json(path) if path.is_file() else {}
    goal = nav.get('planning_profile', {}).get('route_context', {}).get('learning_goal') or model['title']
    result = {'schema_version': '3.0', 'overview': model['title'], 'goal': goal, 'progress': progress, 'aspects': aspects,
              'strengths': strengths, 'weaknesses': weaknesses, 'unverified': unverified, 'next_steps': next_steps}
    result['track_progress'] = {track: {'total': sum(l['track'] == track for l in active),
                                     'completed': sum(l['track'] == track and l['status'] == 'completed' for l in active),
                                     'remaining': sum(l['track'] == track and l['status'] not in ('completed', 'skipped') for l in active)}
                                for track in ('main', 'branch')}
    for lesson in active:
        for q in lesson.get('content', {}).get('questions', []):
            if any(x['purpose'] == 'retest' for x in q.get('reuse_review', [])):
                unverified.append(f"课程 {lesson['number']} / {q['question_id']}：复用了既有题目，复测结果不足以独立证明新情境迁移。")
    validate_json_schema(result, ROOT / 'utils/references/interactive-tutor-report-v3.schema.json')
    return result


def render_report(report):
    p = report['progress']
    labels = {'pending': '待学习', 'awaiting_answer': '正在学习', 'completed': '已学习', 'retry': '正在学习', 'skipped': '已跳过'}
    text = f"# 学习报告\n\n{report['overview']}\n\n## 学习进展\n\n学习目标：{report['goal']}\n\n已完成课程：{p['completed']}；待完成：{p['pending']}；已跳过：{p['skipped']}。\n\n跳过不计为掌握；完成课程与掌握程度分别记录。\n\n## 知识点掌握与证据\n\n"
    text += markdown_table(['知识点', '学习状态', '掌握情况', '证据数量', '习题依据'],
        [[a['title'], labels[a['learning_status']], a['classification'], len(a['evidence']),
          '、'.join(f"[{e['label']}]({e['link']})" for e in a['evidence']) or '无'] for a in report['aspects']])
    if report.get('track_progress'):
        t = report['track_progress']
        text += f"\n\n主线已完成 {t['main']['completed']}/{t['main']['total']}；支线已完成 {t['branch']['completed']}/{t['branch']['total']}，剩余 {t['branch']['remaining']} 节。"
    text += '\n\n## 阶段优势与薄弱点\n\n**阶段优势：**\n\n'
    text += '\n'.join('- ' + x for x in report['strengths']) or '- 尚无足够证据。'
    text += '\n\n**薄弱点：**\n\n' + ('\n'.join('- ' + x for x in report['weaknesses']) or '- 当前评分未发现明显不足；仍需关注未验证能力。')
    for title, key in [('未验证能力', 'unverified'), ('下一步学习与复习建议', 'next_steps')]:
        text += f'\n\n## {title}\n\n' + '\n'.join('- ' + x for x in report[key])
    return text + '\n'
