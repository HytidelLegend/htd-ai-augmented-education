"""Goal-first graph assessment; observations never imply graph-wide mastery."""
from .dependency_graph import edges_from_units, validate_selected_order
from .structured_io import json_digest

UNKNOWN = '没听过 / 不清楚 / 没把握'

def graph_revision(units):
    return json_digest(sorted([{'unit_id':u['unit_id'], 'title':u['title'], 'module':u.get('module',''),
                       'prerequisites':sorted(u.get('prerequisites', [])), 'source':u.get('source')} for u in units], key=lambda x:x['unit_id']))

def begin(units, level, goal, limit, goal_ids=None):
    if not level.strip() or not goal.strip() or level == '待确认' or goal == '待确认':
        raise ValueError('请在对话中确认学习背景和学习目的')
    known = {u['unit_id'] for u in units}
    goals = goal_ids or [u['unit_id'] for u in units if u.get('importance') == 'required'] or list(known)
    if not set(goals) <= known:
        raise ValueError('目标引用未知知识点')
    return {'schema_version': '2.0', 'status': 'question_required', 'current_level': level,
            'learning_goal': goal, 'max_questions': limit, 'goal_unit_ids': goals,
            'graph_revision': graph_revision(units), 'answers': [], 'pending_question': None,
            'stop_reason': None}

def target(data, units):
    if data['status'] == 'completed' or len(data['answers']) >= data['max_questions']:
        return None
    by_id = {u['unit_id']: u for u in units}
    relevant = set(data['goal_unit_ids'])
    todo = list(relevant)
    while todo:
        for dep in by_id[todo.pop()].get('prerequisites', []):
            if dep not in relevant:
                relevant.add(dep); todo.append(dep)
    tested = {a['unit_id'] for a in data['answers']}
    branches = {by_id[a['unit_id']].get('module', '') for a in data['answers']}
    candidates = [u for u in units if u['unit_id'] not in tested and u['unit_id'] in relevant]
    if not candidates:
        return None
    last = data['answers'][-1] if data['answers'] else None
    followup = set()
    if last:
        uid = last['unit_id']
        if last['observation'] == 'positive':
            followup = {x for x in relevant if uid in by_id[x].get('prerequisites', [])}
        else:
            followup = set(by_id[uid].get('prerequisites', []))
    def score(u):
        unlocked = sum(u['unit_id'] in by_id[x].get('prerequisites', []) for x in relevant)
        return (unlocked > 1, u['unit_id'] in followup, u.get('module', '') not in branches,
                unlocked, u['unit_id'] in data['goal_unit_ids'], -u.get('sequence', 0))
    wanted = max(candidates, key=score)
    return {'unit_id': wanted['unit_id'], 'title': wanted['title'],
            'reason': '优先目标所需的共享基础；依据上一题向后续或前置调整，再覆盖缺少证据的主题'}

def add_question(data, units, question):
    wanted = target(data, units)
    if data['pending_question'] or not wanted or question['unit_id'] != wanted['unit_id']:
        raise ValueError('题目必须针对当前探测知识点')
    opts = question.get('options', [])
    if len(opts) != 5 or opts[-1] != UNKNOWN or len(set(opts)) != 5 or any(not isinstance(x, str) or not x.strip() for x in opts):
        raise ValueError('需要四个实质选项和末尾的不确定选项')
    if type(question.get('correct_index')) is not int or question['correct_index'] not in range(4):
        raise ValueError('正确选项必须是前四项之一')
    prompt = question.get('prompt', '')
    if not prompt.strip() or any(x in prompt for x in ('是否听过', '有没有听过', '听说过吗')):
        raise ValueError('诊断必须直接考查知识，不能询问是否听过名词')
    data['pending_question'] = question
    data['status'] = 'awaiting_answer'

def answer(data, units, choice):
    if data['status'] != 'awaiting_answer' or not data['pending_question']:
        raise ValueError('没有待回答的题目')
    if choice is not None and (type(choice) is not int or choice not in range(5)):
        raise ValueError('答案必须是 0～4 或不确定')
    q = data['pending_question']
    unsure = choice is None or choice == 4
    data['answers'].append({**q, 'choice': choice, 'correct': not unsure and choice == q['correct_index'],
                            'observation': 'uncertain' if unsure else 'positive' if choice == q['correct_index'] else 'negative'})
    data['pending_question'] = None
    data['status'] = 'question_required'
    if target(data, units) is None:
        data['status'] = 'completed'
        data['stop_reason'] = 'budget_reached' if len(data['answers']) >= data['max_questions'] else 'relevant_points_sampled'
    data['untested_unit_ids'] = [u['unit_id'] for u in units if u['unit_id'] not in {a['unit_id'] for a in data['answers']}]
    return data

def candidates(units, context, config):
    """Bounded deterministic DFS; preserve every emitted order and explain scores."""
    ids = [u['unit_id'] for u in units]
    by_id = {u['unit_id']: u for u in units}
    deps = {x: set(by_id[x].get('prerequisites', [])) for x in ids}
    orders = []
    limit = config['ordering']['max_candidate_orders']
    def visit(prefix, remaining):
        if len(orders) >= limit: return
        if not remaining:
            orders.append(prefix); return
        for x in sorted((x for x in remaining if deps[x] <= set(prefix)), key=lambda x: (by_id[x].get('sequence', ids.index(x)), x)):
            visit(prefix + [x], remaining - {x})
            if len(orders) >= limit: break
    visit([], set(ids))
    if not orders: raise ValueError('知识点依赖图包含环')
    goals = set(context.get('goal_unit_ids', [])) or {x for x in ids if by_id[x].get('importance') == 'required'}
    observations = {a['unit_id']: a.get('observation', 'positive' if a.get('correct') else 'negative') for a in context.get('diagnostic_answers', [])}
    difficulty = {'easy': 1, 'medium': 2, 'hard': 3, 'beginner': 1, 'intermediate': 2, 'advanced': 3, '简单': 1, '中等': 2, '困难': 3}
    records = []
    n = max(1, len(ids)); pair_count = max(1, n - 1)
    for order in orders:
        validate_selected_order(ids, edges_from_units(units), order)
        components = {
            'goal_match': sum(1 - order.index(x) / n for x in goals) / max(1, len(goals)),
            'background_match': sum((order.index(x) / n if v == 'positive' else 1 - order.index(x) / n) for x, v in observations.items()) / len(observations) if observations else .5,
            'difficulty_smoothness': 1 - sum(abs(difficulty.get(by_id[a]['difficulty'], 2) - difficulty.get(by_id[b]['difficulty'], 2)) / 2 for a, b in zip(order, order[1:])) / pair_count,
            'topic_continuity': 1 - sum(by_id[a]['module'] != by_id[b]['module'] for a, b in zip(order, order[1:])) / pair_count,
            'downstream_unlock': sum((1 - i / n) * sum(x in deps[y] for y in ids) for i, x in enumerate(order)) / max(1, sum(map(len, deps.values()))),
        }
        score = round(sum(config['ordering']['weights'][k] * v for k, v in components.items()) * 100, 4)
        records.append({'candidate_id': 'ORDER-' + json_digest(order)[:12], 'unit_ids': order,
                        'score_components': components, 'score': score,
                        'recommendation_reasons': [f'先从“{by_id[order[0]]["title"]}”进入；目标匹配 {components["goal_match"]:.0%}；背景匹配 {components["background_match"]:.0%}；难度平缓 {components["difficulty_smoothness"]:.0%}；主题连续 {components["topic_continuity"]:.0%}；后续解锁 {components["downstream_unlock"]:.0%}；已满足全部前置关系']})
    records.sort(key=lambda r: (-r['score'], r['candidate_id']))
    return {'graph_revision': graph_revision(units), 'config_revision': json_digest(config),
            'candidates': records, 'recommended_order_ids': [r['candidate_id'] for r in records if r['score'] == records[0]['score']],
            'generation': {'limit': limit, 'count': len(records), 'exhaustive': len(records) < limit,
                           'stop_reason': 'candidate_limit' if len(records) == limit else 'exhausted'}}
