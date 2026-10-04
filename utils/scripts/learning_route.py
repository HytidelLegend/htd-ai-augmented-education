"""Persisted fork choices and branch continuation for the tutor."""
from .structured_io import json_digest


def pending(model):
    return [l for l in model['lessons'] if not l['archived'] and not l['skip']
            and l['status'] in ('pending', 'retry') and any(not model['unit_progress'][u]['skip'] for u in l['teaches_unit_ids'])]


def eligible(model, lessons):
    def satisfied(uid):
        p = model['unit_progress'][uid]
        return p['status'] == 'completed' or p['skip'] or all(
            next(l for l in model['lessons'] if l['lesson_id'] == pid)['skip'] for pid in model['coverage'][uid])
    return [l for l in lessons if all(satisfied(u) for u in l['prerequisite_unit_ids'])]


def snapshot(model, candidates, phase):
    return {'phase': phase, 'candidates': [l['lesson_id'] for l in candidates],
            'sha256': json_digest([model['current_lesson_id'], phase, model['lesson_edges'],
                                   [l['lesson_id'] for l in candidates],
                                   [(l['lesson_id'], l['title'], l['number'], l['track'], l['status'], l['skip'], l['archived'], l['prerequisite_unit_ids']) for l in model['lessons'] if not l['archived']],
                                   [(uid, p['status'], p['skip']) for uid, p in sorted(model['unit_progress'].items())]])}


def ordered(model, lessons):
    routes = model['candidate_orders'].get('candidates', [])
    chosen = next((x for x in routes if x['candidate_id'] == model['candidate_orders'].get('selected_order_id')), None)
    units = (chosen or routes[0])['unit_ids'] if routes else [u['unit_id'] for u in model['units']]
    rank = {uid: i for i, uid in enumerate(units)}
    return sorted(lessons, key=lambda l: (l['track'] != 'main', min(rank.get(uid, len(rank)) for uid in l['teaches_unit_ids']), l['lesson_id']))


def refresh(model, transition):
    """Reconcile a waiting choice after skip/restore/plan/navigation changes."""
    candidates = ordered(model, eligible(model, pending(model)))
    mains = any(l['track'] == 'main' for l in pending(model))
    phase = 'fork' if mains else 'branches'
    if not candidates or (phase == 'fork' and not any(l['track'] == 'branch' for l in candidates)):
        transition(model, 'ready', 'route_choice_no_longer_needed')
        model.pop('route_choice', None)
        return None
    if phase == 'branches' and model['state'] == 'awaiting_route_choice':
        transition(model, 'ready', 'main_route_changed')
        transition(model, 'awaiting_branch_continuation', 'main_finished_with_pending_branches')
    elif phase == 'fork' and model['state'] in ('main_completed', 'awaiting_branch_continuation'):
        transition(model, 'ready', 'main_route_restored')
        transition(model, 'awaiting_route_choice', 'fork_detected')
    model['route_choice'] = snapshot(model, candidates, phase)
    return receipt(model)


def receipt(model):
    by_id = {l['lesson_id']: l for l in model['lessons']}
    choice = model['route_choice']
    lines = ['主线已完成，是否继续学习剩余支线？' if choice['phase'] == 'branches' else '下一步出现主线与支线分叉：继续主线，还是开启支线？',
             '', '请选择支线课程，或回复“暂不”保留待学支线。' if choice['phase'] == 'branches' else '未特别指定路线时，“继续／下一课”默认持续推进主线。',
             '请到 Web 应用「交互式学习」的「图谱页」查看更详细的学习路线图。', '']
    for lid in choice['candidates']:
        l = by_id[lid]; lines.append(f"- {l['number']}｜{l['title']}｜{'主线' if l['track'] == 'main' else '支线'}（{lid}）")
    return {'status': model['state'], 'choice_sha256': choice['sha256'], 'markdown': '\n'.join(lines)}


def gate(model, transition):
    if model['state'] in ('awaiting_route_choice', 'awaiting_branch_continuation', 'main_completed'):
        return refresh(model, transition)
    all_pending = pending(model); ready = ordered(model, eligible(model, all_pending))
    mains = [l for l in all_pending if l['track'] == 'main']
    branch_ready = [l for l in ready if l['track'] == 'branch']
    selected = model.get('route_selection')
    if selected:
        current = snapshot(model, ready, 'branches' if not mains else 'fork')['sha256']
        if selected['sha256'] != current:
            model.pop('route_selection', None)
        else: return None
    session = model.get('branch_session')
    if session:
        branch_ids = set(session['lesson_ids'])
        if any(l['lesson_id'] in branch_ids for l in branch_ready): return None
        model.pop('branch_session', None)
    if not mains and all_pending:
        if not branch_ready: return {'status': 'blocked_prerequisites', 'message': '剩余支线前置条件尚未满足'}
        model['route_choice'] = snapshot(model, branch_ready, 'branches')
        transition(model, 'awaiting_branch_continuation', 'main_finished_with_pending_branches')
        return receipt(model)
    successors = {e['successor_id'] for e in model['lesson_edges'] if e['predecessor_id'] == model['current_lesson_id']}
    next_ready = [l for l in ready if l['lesson_id'] in successors]
    if any(l['track'] == 'main' for l in next_ready) and any(l['track'] == 'branch' for l in next_ready):
        model['route_choice'] = snapshot(model, ready, 'fork')
        transition(model, 'awaiting_route_choice', 'fork_detected')
        return receipt(model)
    return None


def choose(model, choice, digest, transition):
    if model['state'] not in ('awaiting_route_choice', 'awaiting_branch_continuation', 'main_completed'):
        raise ValueError('当前不在选路阶段')
    stored = model['route_choice']
    result = refresh(model, transition)
    if result is None: return {'status': model['state'], 'message': '路线已变化，请重新准备课程'}
    candidates = ordered(model, eligible(model, pending(model)))
    if digest != stored['sha256'] or model['route_choice']['sha256'] != digest:
        return {**result, 'message': '路线候选已变化，请按新快照重选'}
    if choice in ('later', '暂不', '不继续') and stored['phase'] == 'branches':
        if model['state'] != 'main_completed': transition(model, 'main_completed', 'branches_deferred')
        return receipt(model)
    if choice in ('继续', '下一课', 'main', '主线'):
        selected = next((l for l in candidates if l['track'] == 'main'), None)
        if not selected: return {**result, 'message': '主线已完成，请指定支线课程或回复暂不'}
    elif choice in ('branch', '支线', '继续支线'):
        branches = [l for l in candidates if l['track'] == 'branch']
        if len(branches) != 1: return {**receipt(model), 'message': '请指定要开启的支线课程 ID 或编号'}
        selected = branches[0]
    else:
        selected = next((l for l in candidates if choice in (l['lesson_id'], l['number'])), None)
    if not selected or selected['lesson_id'] not in stored['candidates']: raise ValueError('所选课程不在当前可学候选中')
    model['route_selection'] = {'lesson_id': selected['lesson_id'], 'sha256': digest}
    if selected['track'] == 'branch':
        ids = {selected['lesson_id']}; changed = True
        while changed:
            changed = False
            for edge in model['lesson_edges']:
                target = next(l for l in model['lessons'] if l['lesson_id'] == edge['successor_id'])
                if edge['predecessor_id'] in ids and target['track'] == 'branch' and target['lesson_id'] not in ids:
                    ids.add(target['lesson_id']); changed = True
        model['branch_session'] = {'lesson_ids': sorted(ids)}
    transition(model, 'ready', 'route_selected')
    return {'status': 'ready', 'lesson_id': selected['lesson_id']}
