"""Persistent learning projects, dual graphs, rendering and event state machines.

All writers hold the project lock; readers use the same lock. JSON is committed
before its deterministic Markdown views. Answers are the only editable blocks.
"""
from __future__ import annotations
from pathlib import Path
import copy
import json
import re
import subprocess
import sys
from .structured_io import read_json, write_json, write_text_atomic, write_text_transaction, json_digest, validate_json_schema
from .timestamp import iso_timestamp, unique_filename_timestamp
from .file_transaction import project_lock
from .dependency_graph import analyze_dependency_graph, edges_from_units
from .learning_navigation_bundle import load_navigation_bundle
from .learning_material_backup import ensure_backup, verify_backup, manifest_path, BackupApprovalRequired, BackupConflict, finish_backup, protect_inputs, backup_stage
from .learning_evidence import build_evidence, resolve_heading_locator, build_navigation_context_evidence
from .learning_config import freeze_config
from .learning_config import load_config
from .adaptive_assessment import candidates as rank_candidates
from .markdown_report import markdown_table
from .markdown_structure import sha256_file
from .bilingual_glossary import parse_markdown as parse_bilingual
from .student_learning_profile import parse_student_profile
from .mermaid_flowchart import render_flowchart, verify_flowchart
from .workflow_checkpoint import WorkflowCheckpoint

ROOT = Path(__file__).resolve().parents[2]
TUTOR = 'beta-interactive-tutor'
STATUSES = {'pending', 'awaiting_answer', 'completed', 'retry', 'skipped'}
TRANSITIONS = {
    'backup_required': {'ready'},
    'ready': {'lesson_decision_required', 'completed'},
    'lesson_decision_required': {'awaiting_answer', 'ready'},
    'awaiting_answer': {'review_decision_required', 'ready'},
    'review_decision_required': {'awaiting_questions', 'review_decision_required'},
    'awaiting_questions': {'awaiting_questions', 'ready'},
    'completed': {'ready'},
}
LABELS = {'pending': '待学习', 'awaiting_answer': '正在学习', 'retry': '正在学习',
          'completed': '已学习', 'skipped': '已跳过'}
DOCUMENT_TRANSITIONS = {
    'prepared': ('validating_graphs',),
    'validating_graphs': ('rendering_documents', 'paused_error'),
    'rendering_documents': ('verifying_documents', 'paused_error'),
    'verifying_documents': ('publishing', 'paused_error'),
    'publishing': ('completed', 'paused_error'),
    'completed': (),
    'paused_error': ('validating_graphs',),
}

def dumps(value): return json.dumps(value, ensure_ascii=False, indent=2) + '\n'
def store(path, value): write_text_atomic(path, dumps(value))
def art(project, name): return project / 'artifacts' / f'{name}.json'
def lock(project): return project_lock(project / 'artifacts' / '.project.lock', 'learning-project')
def safe_path(root, value):
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    # Reject junctions/symlinks before resolving them.
    for item in [candidate, *candidate.parents]:
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('路径不可包含链接')
    resolved = candidate.resolve()
    resolved.relative_to(root.resolve())
    return resolved

def navigation_preflight(root, path, return_message=False, material_project=None):
    cli = ROOT / 'skills/beta-build-curriculum-navigation/scripts/cli.py'
    extra = ['--material-project', str(material_project)] if material_project is not None else []
    result = subprocess.run([sys.executable, str(cli), 'verify', '--root', str(root), '--navigation', str(path), *extra],
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise ValueError('导航前置校验失败：' + result.stdout.strip())
    bundle = load_navigation_bundle(root=root, navigation_json=path, material_project=material_project)
    nav = bundle['navigation']
    assessment = nav.get('planning_profile', {}).get('assessment')
    if not assessment or assessment.get('status') != 'completed' or assessment.get('schema_version') != '2.0':
        raise ValueError('导航缺少新版诊断；请先调用导航 skill 完成背景讨论与诊断')
    message = json.loads(result.stdout).get('config_message', '')
    return (nav, message) if return_message else nav

def transition(model, target, event):
    current = model['state']
    if target not in TRANSITIONS.get(current, set()):
        raise ValueError(f'非法状态迁移：{current} → {target}')
    model['state'] = target
    model['events'].append({'at': iso_timestamp(), 'from': current, 'to': target, 'event': event})

def initial_plan(nav):
    chapters = []; lessons = []; modules = {}
    for u in sorted(nav['units'], key=lambda x: x['sequence']):
        group = (u['stage'], u['module'])
        if group not in modules:
            number = len(chapters) + 1
            cid = f'CH-{number}'
            modules[group] = cid
            chapters.append({'chapter_id': cid, 'number': number, 'title': u['module']})
        cid = modules[group]
        section = sum(l['chapter_id'] == cid for l in lessons) + 1
        lessons.append({'lesson_id': 'LESSON-' + json_digest([u['unit_id']])[:16], 'chapter_id': cid,
                        'section_number': section, 'title': u['title'], 'track': 'main' if u['importance'] in ('required', 'critical_reading') else 'branch',
                        'teaches_unit_ids': [u['unit_id']], 'internal_unit_order': [u['unit_id']],
                        'prerequisite_providers': {}, 'status': 'pending', 'archived': False, 'skip': False})
    return chapters, lessons

def rebuild(model, config):
    units = {u['unit_id']: u for u in model['units']}
    if len(units) != len(model['units']): raise ValueError('知识点 ID 重复')
    if not analyze_dependency_graph(list(units), edges_from_units(model['units']))['is_dag']:
        raise ValueError('知识点依赖图包含环')
    chapters = {c['chapter_id']: c for c in model['chapters']}
    if len(chapters) != len(model['chapters']) or len({c['number'] for c in model['chapters']}) != len(chapters):
        raise ValueError('章节 ID 或编号重复')
    active = [l for l in model['lessons'] if not l['archived']]
    by_id = {l['lesson_id']: l for l in active}
    if len({l['lesson_id'] for l in model['lessons']}) != len(model['lessons']): raise ValueError('课程 ID 重复')
    numbers = set(); providers = {x: [] for x in units}
    for l in active:
        if l['status'] not in STATUSES or l['track'] not in ('main', 'branch') or l['chapter_id'] not in chapters:
            raise ValueError('课程状态、主支线或章节无效')
        section = l['section_number']
        if type(section) is not int or section < 1: raise ValueError('小节编号无效')
        number = (l['chapter_id'], section)
        if number in numbers: raise ValueError('课程编号重复')
        numbers.add(number)
        taught = l['teaches_unit_ids']
        if not taught or len(set(taught)) != len(taught) or not set(taught) <= set(units): raise ValueError('课程引用未知或重复知识点')
        # Existing published material survives a later reduction of the configured limit.
        if l['status'] == 'pending' and len(taught) > config['lesson']['max_new_units']:
            raise ValueError('课程知识点过多，请拆分尚未发布课程')
        order = l['internal_unit_order']
        if set(order) != set(taught) or len(order) != len(taught): raise ValueError('课内顺序必须完整覆盖讲解知识点')
        pos = {x: i for i, x in enumerate(order)}
        for x in taught:
            for dep in units[x]['prerequisites']:
                if dep in pos and pos[dep] >= pos[x]: raise ValueError('课内教学顺序违反前置要求')
            providers[x].append(l['lesson_id'])
        l['number'] = f"{chapters[l['chapter_id']]['number']}.{section}"
        l['filename'] = f"课程_{chapters[l['chapter_id']]['number']}-{section}.md"
    gaps = [x for x in units if not providers[x] and not model['unit_progress'][x]['skip'] and model['unit_progress'][x]['status'] != 'completed']
    if gaps: raise ValueError('未跳过知识点缺少讲解课程：' + '、'.join(units[x]['title'] for x in gaps))
    edges = []; prereqs = {}
    for l in active:
        taught = set(l['teaches_unit_ids'])
        required = sorted({d for x in taught for d in units[x]['prerequisites'] if d not in taught})
        prereqs[l['lesson_id']] = required
        l['prerequisite_unit_ids'] = required
        for dep in required:
            candidates = [x for x in providers[dep] if x != l['lesson_id']]
            preferred = l['prerequisite_providers'].get(dep)
            if preferred and preferred not in candidates: raise ValueError('前置知识提供课程无效')
            provider = preferred or (candidates[0] if candidates else None)
            if provider:
                edges.append({'predecessor_id': provider, 'successor_id': l['lesson_id'], 'unit_id': dep})
            elif not model['unit_progress'][dep]['skip'] and model['unit_progress'][dep]['status'] != 'completed':
                raise ValueError('前置知识缺少提供课程')
    analysis = analyze_dependency_graph(list(by_id), edges)
    if not analysis['is_dag']: raise ValueError('课程依赖图包含环；请明确前置知识提供课程或拆课')
    changed = True
    while changed:
        changed = False
        for edge in edges:
            before, after = by_id[edge['predecessor_id']], by_id[edge['successor_id']]
            if after['track'] == 'main' and before['track'] == 'branch':
                before['track'] = 'main'; changed = True
    model['unit_edges'] = edges_from_units(model['units'])
    model['lesson_edges'] = edges
    model['coverage'] = providers
    return model

def classification(score, config):
    if score is None: return '未评估'
    return '良好' if score >= config['mastery']['good_min'] else '中等' if score >= config['mastery']['medium_min'] else '一般'

def roadmap_graphs(model):
    """Project the two authoritative graphs without inventing hierarchy edges."""
    units = {'nodes': [{'id': u['unit_id'], 'label': u['title'],
                       'status': LABELS[model['unit_progress'][u['unit_id']]['status']]}
                      for u in model['units']],
             'edges': [{'from': e['predecessor_id'], 'to': e['successor_id']} for e in model['unit_edges']]}
    lessons = {'nodes': [{'id': l['lesson_id'], 'label': f"{l['number']} {l['title']}",
                         'track': '主线' if l['track'] == 'main' else '支线', 'status': LABELS[l['status']]}
                        for l in model['lessons'] if not l['archived']],
               'edges': [{'from': e['predecessor_id'], 'to': e['successor_id']} for e in model['lesson_edges']]}
    return units, lessons


def verify_roadmap_flowcharts(model, markdown):
    blocks = re.findall(r'^```mermaid\n.*?^```$', markdown, flags=re.MULTILINE | re.DOTALL)
    graphs = roadmap_graphs(model)
    if len(blocks) != len(graphs):
        raise ValueError('学习路线必须包含知识点、课程双思维导图')
    for graph, block in zip(graphs, blocks):
        verify_flowchart(graph, block)


def roadmap_md(model):
    titles = {u['unit_id']: u['title'] for u in model['units']}
    lessons = {l['lesson_id']: l for l in model['lessons']}
    rows = [[u['title'], '、'.join(titles[x] for x in u['prerequisites']) or '无', LABELS[model['unit_progress'][u['unit_id']]['status']],
             '、'.join(lessons[x]['number'] for x in model['coverage'][u['unit_id']])] for u in model['units']]
    result = '# 学习路线\n\n请在 Web 应用「交互式学习」的「图谱页」查看课程与知识点的可交互依赖图。\n\n## 知识点与前置关系\n\n'
    result += markdown_table(['知识点', '前置知识点', '学习状态', '讲解课程'], rows)
    rows = [[l['number'], l['title'], '主线' if l['track'] == 'main' else '支线',
             '、'.join(lessons[e['predecessor_id']]['number'] for e in model['lesson_edges'] if e['successor_id'] == l['lesson_id']) or '无',
             LABELS[l['status']]] for l in model['lessons'] if not l['archived']]
    result += '\n\n## 课程与前置关系\n\n' + markdown_table(['课程编号', '标题', '安排', '前置课程', '学习状态'], rows)
    result += '\n\n箭头表示“前置 → 后续”；已跳过节点保留依赖关系，退出的历史课程不展示。\n'
    for title, graph in zip(('知识点思维导图', '课程思维导图'), roadmap_graphs(model)):
        result += f'\n## {title}\n\n' + render_flowchart(graph) + '\n'
    return result

def documents(model, project, config):
    summary = {'schema_version': '2.0', 'lessons': []}
    report = {'schema_version': '2.0', 'overview': model['title'], 'aspects': []}
    summary_md = '# 总结\n'
    for l in model['lessons']:
        if not l.get('review') or not l.get('content'): continue
        c = l['content']
        item = {'lesson_id': l['lesson_id'], 'number': l['number'], 'overview': c['overview'], 'key_points': c['key_points'], 'formulas': c['formulas']}
        summary['lessons'].append(item)
        summary_md += f"\n## 课程 {l['number']}｜{l['title']}\n\n{c['overview']}\n\n"
        summary_md += '\n'.join('- ' + x for x in c['key_points']) + '\n\n'
        summary_md += '\n'.join('- ' + x for x in c['formulas']) + '\n'
    for u in model['units']:
        p = model['unit_progress'][u['unit_id']]
        report['aspects'].append({'unit_id': u['unit_id'], 'title': u['title'], 'mastery': p['mastery'],
                                  'classification': classification(p['mastery'], config), 'evidence_refs': p['evidence_refs']})
    report_md = '# 学习报告\n\n' + model['title'] + '\n\n已学习内容：' + ('、'.join(l['title'] for l in model['lessons'] if l.get('review')) or '尚无完成课程') + '\n\n'
    report_md += markdown_table(['知识点', '掌握情况', '习题依据'], [[a['title'], a['classification'], '、'.join(a['evidence_refs']) or '无'] for a in report['aspects']]) + '\n'
    for l in model['lessons']:
        if l.get('feedback'):
            report_md += f"\n## 课程 {l['number']} 作答反馈\n\n{l['feedback']['strengths']}\n\n{l['feedback']['weaknesses']}\n"
    notes = model['notes']; errors = model['errors']
    notes_md = '# 笔记本\n' + ''.join(f"\n## {n['title']}\n\n{n['background']}\n\n{n['text']}\n\n来源：课程 {n['lesson_number']}，{n['location']}\n" for n in notes)
    errors_md = '# 错题本\n' + ''.join(f"\n## {e['title']}\n\n薄弱点：{e['weakness']}\n\n普遍规律：{e['rule']}\n\n纠正与复习：{e['correction']}\n\n### 例题\n\n" + '\n\n'.join(f"课程 {x['lesson_number']}：{x['prompt']}\n\n我的回答：{x['answer']}\n\n参考答案：{x['reference_answer']}" for x in e['examples']) + '\n' for e in errors)
    return {'学习路线': (model, roadmap_md(model)), '总结': (summary, summary_md), '学习报告': (report, report_md),
            '笔记本': ({'entries': notes}, notes_md), '错题本': ({'entries': errors}, errors_md)}

def _adopt_model(target, value):
    """Adopt a committed snapshot while retaining caller-held project references."""
    if isinstance(target, dict) and isinstance(value, dict):
        for key in list(target):
            if key not in value:
                del target[key]
        for key, item in value.items():
            target[key] = _adopt_model(target.get(key), item)
        return target
    if isinstance(target, list) and isinstance(value, list):
        target[:] = [_adopt_model(target[i] if i < len(target) else None, item)
                     for i, item in enumerate(value)]
        return target
    return value


def save(project, model, config, material_backup=None):
    # Resume always revalidates authoritative JSON, never trusts a stale diagram.
    workflow = WorkflowCheckpoint(DOCUMENT_TRANSITIONS, Path(model['run_dir']) / 'document-render', resume=True)
    if workflow.state not in ('prepared', 'paused_error'):
        workflow.move('paused_error', error='上次文档生成中断，重新校验权威数据', resume_stage='validating_graphs')
    workflow.move('validating_graphs', source_revision=model['revision'], error=None, resume_stage=None)
    candidate = copy.deepcopy(model)
    try:
        _save_documents(project, candidate, config, material_backup, workflow)
        _adopt_model(model, candidate)
        workflow.move('completed', published_revision=model['revision'])
    except Exception as exc:
        workflow.move('paused_error', error=str(exc), resume_stage='validating_graphs')
        raise


def _save_documents(project, model, config, material_backup, workflow):
    rebuild(model, config)
    model['revision'] += 1; model['updated_at'] = iso_timestamp()
    model['render_config'] = copy.deepcopy(config)
    validate_json_schema(model, ROOT/'utils/references/interactive-tutor-project-state-v3.schema.json')
    model['project_status'] = 'completed' if all(p['status'] in ('completed', 'skipped') for p in model['unit_progress'].values()) and all(l['status'] in ('completed', 'skipped') for l in model['lessons'] if not l['archived']) and model['state'] == 'completed' else 'in_progress'
    workflow.move('rendering_documents')
    docs = documents(model, project, config)
    workflow.move('verifying_documents')
    verify_roadmap_flowcharts(model, docs['学习路线'][1])
    # Readers hold the lock, so they cannot observe a partially published revision.
    updates = {art(project, name): value for name, (value, _) in docs.items()}
    updates[project / '项目.json'] = {'schema_version': '3.0', 'project_id': model['project_id'], 'title': model['title'],
         'revision': model['revision'], 'status': model['project_status'], 'updated_at': model['updated_at'],
         'navigation_json': model['navigation_json'], 'route': 'artifacts/学习路线.json', 'run_dir': model['run_dir']}
    for l in model['lessons']:
        if l.get('content'):
            updates[project / 'artifacts/lessons' / Path(l['filename']).with_suffix('.json')] = l
    if material_backup is not None:
        nav = read_json(Path(model['navigation_json']))
        if json_digest(nav) != model['navigation_hash']:
            raise ValueError('发布期间导航发生变化，禁止发布材料与项目数据')
        verify_backup(Path(model['workspace_root']), project, nav, plan=material_backup)
        if manifest_path(project).is_file():
            previous = read_json(manifest_path(project))
            if previous != material_backup:
                updates[project / 'artifacts/material-backup-history' / f'{json_digest(previous)}.json'] = previous
        updates[manifest_path(project)] = material_backup
    # Project JSON, Markdown views and a new material manifest share one rollback.
    text_updates = {path: dumps(value) for path, value in updates.items()}
    text_updates.update({project / f'{name}.md': md for name, (_, md) in docs.items()})
    text_updates[Path(model['run_dir']) / 'state.json'] = dumps({
        'state': model['state'], 'project_dir': str(project), 'revision': model['revision']})
    text_updates[Path(model['run_dir']) / 'plan.template.json'] = dumps({
        'base_revision': model['revision'], 'operations': []})
    protection = material_backup
    if protection is None and manifest_path(project).is_file():
        protection = read_json(manifest_path(project))
    if protection is not None:
        protect_inputs(Path(model['workspace_root']), protection, list(text_updates))
    workflow.move('publishing', target_revision=model['revision'])
    try:
        write_text_transaction(text_updates)
    except Exception as exc:
        if material_backup is not None:
            backup_stage(Path(model['run_dir']), 'paused_backup_error', error=str(exc), resume_stage='planning_backup')
        raise
    if material_backup is not None:
        state_path = Path(model['run_dir']) / 'material-backup-state.json'
        if state_path.is_file() and read_json(state_path)['state'] == 'verifying_backup':
            finish_backup(Path(model['run_dir']), material_backup)

def load(project):
    model = read_json(art(project, '学习路线'))
    validate_json_schema(model, ROOT/'utils/references/interactive-tutor-project-state-v3.schema.json')
    return model

def create(root, nav_path, output=None, request=None, approved_plan_sha256=None):
    nav = navigation_preflight(root, nav_path)
    runs = root / 'logs' / TUTOR / 'runs'
    stamp = unique_filename_timestamp([p.name for p in runs.iterdir()] if runs.exists() else [])
    run_dir = runs / stamp
    project = output or root / 'outputs' / TUTOR / 'runs' / stamp
    if project.exists(): raise ValueError('项目目录已存在，请恢复原项目')
    receipt = freeze_config(TUTOR, run_dir)
    request = request or {}
    snapshots = []; glossary = []
    profile = request.get('student_profile') or (nav.get('student_profile_ref') or {}).get('path')
    for kind, value in [('student_profile', profile), ('bilingual_glossary', request.get('bilingual_glossary'))]:
        if not value: continue
        path = safe_path(root, value)
        if kind == 'bilingual_glossary': glossary = parse_bilingual(path)
        else: parse_student_profile(path, schema_path=ROOT/'utils/references/student-learning-profile-v2.schema.json')
        snapshots.append({'kind': kind, 'path': str(path), 'sha256': sha256_file(path)})
    chapters, lessons = initial_plan(nav)
    model = {'schema_version': '3.0', 'project_id': stamp, 'title': nav['title'], 'navigation_json': str(nav_path),
             'navigation_hash': json_digest(nav), 'workspace_root': str(root), 'revision': 0, 'state': 'ready', 'current_lesson_id': None,
             'run_dir': str(run_dir), 'units': nav['units'], 'chapters': chapters, 'lessons': lessons,
             'unit_progress': {u['unit_id']: {'status': 'pending', 'skip': False, 'mastery': None, 'evidence_refs': []} for u in nav['units']},
             'notes': [], 'errors': [], 'events': [], 'question_discussions': [], 'applied_actions': [],
             'request': request, 'input_snapshots': snapshots, 'bilingual_terms': glossary,
             'candidate_history': [], 'candidate_orders': nav['planning_profile']['ordering']}
    project.mkdir(parents=True)
    with lock(project):
        model['material_project'] = str(project)
        # Persist a recoverable setup checkpoint before any material publication.
        model['state'] = 'backup_required'
        save(project, model, receipt['config'])
        try:
            backup = ensure_backup(root, project, nav, run_dir, approved_plan_sha256, publish=False)
        except BackupApprovalRequired as exc:
            return exc.receipt
        transition(model, 'ready', 'material_backup_verified')
        save(project, model, receipt['config'], material_backup=backup)
    return {'status': 'ready', 'project_dir': str(project), 'run_dir': str(run_dir)}

def sync_navigation(model, nav):
    if json_digest(nav) == model['navigation_hash']: return
    if model['state'] not in ('ready', 'completed', 'awaiting_questions'):
        raise ValueError('导航已改变；请先结束当前作答与答疑，再同步')
    old_ids = {u['unit_id'] for u in model['units']}; new_ids = {u['unit_id'] for u in nav['units']}
    for l in model['lessons']:
        if set(l['teaches_unit_ids']) - new_ids: l['archived'] = True
    for x in new_ids - old_ids:
        model['unit_progress'][x] = {'status': 'pending', 'skip': False, 'mastery': None, 'evidence_refs': []}
    for x in old_ids - new_ids: model['unit_progress'].pop(x, None)
    model['units'] = nav['units']; model['navigation_hash'] = json_digest(nav)
    old_orders = model['candidate_orders']
    if old_orders not in model.setdefault('candidate_history', []):
        model['candidate_history'].append(copy.deepcopy(old_orders))
    model['candidate_orders'] = copy.deepcopy(nav['planning_profile']['ordering'])
    # New source material receives real courses; unaffected published courses retain IDs.
    for u in nav['units']:
        if not any(not l['archived'] and u['unit_id'] in l['teaches_unit_ids'] for l in model['lessons']):
            chapter = next((c for c in model['chapters'] if c['title'] == u['module']), None)
            if chapter is None:
                chapter = {'chapter_id': 'CH-' + str(len(model['chapters'])+1), 'number': len(model['chapters'])+1, 'title': u['module']}
                model['chapters'].append(chapter)
            section = 1 + max((l['section_number'] for l in model['lessons'] if l['chapter_id'] == chapter['chapter_id']), default=0)
            model['lessons'].append({'lesson_id': 'LESSON-' + json_digest([u['unit_id'], section])[:16], 'chapter_id': chapter['chapter_id'],
                'section_number': section, 'title': u['title'], 'track': 'branch', 'teaches_unit_ids': [u['unit_id']],
                'internal_unit_order': [u['unit_id']], 'prerequisite_providers': {}, 'status': 'pending', 'archived': False, 'skip': False})
    if model['state'] == 'completed': transition(model, 'ready', 'new_navigation')

def apply_actions(project, model):
    path = art(project, '调整指令')
    if not path.is_file(): return
    data = read_json(path)
    for action in data['actions']:
        if action['action_id'] in model['applied_actions']: continue
        target = action['target_id']; skip = action['operation'] == 'skip'
        if action['target_type'] == 'unit':
            if target not in model['unit_progress']: raise ValueError('调整指令引用未知知识点')
            p = model['unit_progress'][target]
            p['skip'] = skip
            p['status'] = 'skipped' if skip else 'completed' if p['mastery'] is not None else 'pending'
        else:
            l = next(l for l in model['lessons'] if l['lesson_id'] == target and not l['archived'])
            l['skip'] = skip
            l['status'] = 'skipped' if skip else 'completed' if l.get('review') else 'pending'
        model['applied_actions'].append(action['action_id'])
    # Skipping a course leaves its knowledge points pending unless explicitly skipped.
    for lesson in model['lessons']:
        suppressed = all(model['unit_progress'][x]['skip'] for x in lesson['teaches_unit_ids'] if x in model['unit_progress'])
        if suppressed and not lesson['skip']:
            lesson['derived_skip'] = True
            lesson['status'] = 'skipped'
        elif lesson.get('derived_skip'):
            lesson['derived_skip'] = False
            lesson['status'] = 'skipped' if lesson['skip'] else 'completed' if lesson.get('review') else 'pending'
    if model['state'] == 'completed' and (any(p['status'] not in ('completed','skipped') for p in model['unit_progress'].values()) or any(l['status'] not in ('completed','skipped') for l in model['lessons'] if not l['archived'])):
        transition(model, 'ready', 'restored_learning')
    if model['state'] in ('awaiting_answer', 'lesson_decision_required'):
        current = next(l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id'])
        if current['status'] == 'skipped':
            for uid in current['teaches_unit_ids']:
                p = model['unit_progress'][uid]
                if not p['skip'] and p['status'] == 'awaiting_answer':
                    p['status'] = 'completed' if p['mastery'] is not None else 'pending'
            transition(model, 'ready', 'current_course_explicitly_skipped')
            model['current_lesson_id'] = None

def verify_input_snapshots(model, root):
    for snapshot in model.get('input_snapshots', []):
        path = safe_path(root, snapshot['path'])
        if not path.is_file() or sha256_file(path) != snapshot['sha256']:
            raise ValueError('学习档案或术语表已变化，请核对后重新创建请求')


def context(project, root=ROOT, preflight=True, approved_plan_sha256=None):
    model = load(project)
    root = Path(model.get('workspace_root', root))
    receipt = freeze_config(TUTOR, Path(model['run_dir']))
    if preflight:
        verify_input_snapshots(model, root)
        nav_path = Path(model['navigation_json'])
        current = read_json(nav_path)
        if manifest_path(project).is_file():
            if json_digest(current) != model['navigation_hash']:
                raise ValueError('导航已变化，请使用 supply-navigation 显式同步材料与导航')
            nav, nav_message = navigation_preflight(root, nav_path, return_message=True, material_project=project)
        else:
            nav, nav_message = navigation_preflight(root, nav_path, return_message=True)
            ensure_backup(root, project, nav, Path(model['run_dir']), approved_plan_sha256)
        model['material_project'] = str(project)
        if model['state'] == 'backup_required':
            transition(model, 'ready', 'material_backup_verified')
        receipt['message'] = ' '.join(x for x in (nav_message, receipt['message']) if x)
        sync_navigation(model, nav)
        nav_config = load_config('beta-build-curriculum-navigation')['config']
        route_context = copy.deepcopy(nav['planning_profile'].get('route_context', {}))
        observations = {a['unit_id']: a for a in route_context.get('diagnostic_answers', [])}
        for uid, p in model['unit_progress'].items():
            if p['mastery'] is not None:
                observations[uid] = {'unit_id': uid, 'observation': 'positive' if p['mastery'] >= receipt['config']['mastery']['good_min'] else 'negative'}
        route_context['diagnostic_answers'] = list(observations.values())
        ranked = rank_candidates(model['units'], route_context, nav_config)
        old = model['candidate_orders']
        if json_digest(old) != json_digest(ranked) and old not in model.setdefault('candidate_history', []):
            model['candidate_history'].append(old)
        selected_id = old.get('selected_order_id')
        if selected_id and any(r['candidate_id'] == selected_id for r in ranked['candidates']):
            ranked['selected_order_id'] = selected_id
        model['candidate_orders'] = ranked
    if model['state'] in ('ready', 'completed', 'awaiting_questions', 'awaiting_answer', 'lesson_decision_required'):
        apply_actions(project, model)
    return model, receipt

def evidence_for(model, lesson, root):
    nav = read_json(Path(model['navigation_json']))
    paths = verify_backup(root, Path(model['material_project']), nav)
    result = []
    for uid in lesson['teaches_unit_ids']:
        u = next(u for u in model['units'] if u['unit_id'] == uid)
        if u['source']:
            path = paths[u['source']['file']]
            heading = resolve_heading_locator(root, path, u['source'])
            evidence = build_evidence(root, path, heading=heading)
        else: evidence = build_navigation_context_evidence(u, nav)
        result.extend([{**e, 'unit_id': uid} for e in evidence])
    return result

def prepare(project, model, config, root, requested=None, allow_missing=False):
    if model['state'] == 'awaiting_questions':
        return {'status': 'awaiting_questions', 'message': '本课批改和文档已更新。你还有疑问吗？明确没有疑问后再生成下一课。'}
    if model['state'] != 'ready': raise ValueError('当前状态不能生成下一课')
    rebuild(model, config)
    pending = [l for l in model['lessons'] if not l['archived'] and not l['skip'] and l['status'] in ('pending', 'retry') and any(not model['unit_progress'][x]['skip'] for x in l['teaches_unit_ids'])]
    def missing(l): return [x for x in l['prerequisite_unit_ids'] if model['unit_progress'][x]['status'] != 'completed']
    def explicitly_skipped(uid):
        if model['unit_progress'][uid]['skip']: return True
        providers = model['coverage'][uid]
        return bool(providers) and all(next(l for l in model['lessons'] if l['lesson_id'] == pid)['skip'] for pid in providers)
    rank_order = model['candidate_orders'].get('candidates', [])
    selected_order = next((r for r in rank_order if r['candidate_id'] == model['candidate_orders'].get('selected_order_id')), None)
    order = (selected_order or rank_order[0])['unit_ids'] if rank_order else [u['unit_id'] for u in model['units']]
    pending.sort(key=lambda l: (l['track'] != 'main', min(order.index(x) for x in l['teaches_unit_ids'])))
    lesson = next((l for l in pending if l['lesson_id'] == requested), None) if requested else next((l for l in pending if all(explicitly_skipped(x) for x in missing(l))), None)
    if not lesson:
        if pending or any(p['status'] not in ('completed', 'skipped') for p in model['unit_progress'].values()):
            return {'status': 'blocked_prerequisites', 'message': '请恢复被跳过课程、重新分配知识点，或明确指定越过待学基础的课程'}
        transition(model, 'completed', 'all_coverage_finished')
        save(project, model, config); return {'status': 'completed'}
    gaps = missing(lesson)
    if gaps and not allow_missing and not all(explicitly_skipped(x) for x in gaps): raise ValueError('前置知识待学；用户明确指定跳学后使用 --allow-missing')
    lesson['missing_prerequisites'] = gaps
    evidence = evidence_for(model, lesson, root)
    run = Path(model['run_dir'])
    nav = read_json(Path(model['navigation_json']))
    store(run / 'evidence.json', {'lesson_id': lesson['lesson_id'], 'evidence': evidence,
          'learner_context': nav['planning_profile'].get('route_context', {}),
          'mastery_evidence': {x: model['unit_progress'][x] for x in lesson['teaches_unit_ids']},
          'teaching_policy': nav['lesson_generation_policy'], 'request': model.get('request', {})})
    tested_units = [uid for uid in lesson['teaches_unit_ids'] if not model['unit_progress'][uid]['skip']]
    template = {'schema_version': '4.0', 'lesson_id': lesson['lesson_id'], 'overview': '', 'key_points': [], 'formulas': [],
                'teaching_points': [{'unit_id': uid, 'text': '', 'evidence_ids': [e['evidence_id'] for e in evidence if e['unit_id'] == uid], 'external_explanation': False} for uid in lesson['teaches_unit_ids']],
                'guiding_questions': [], 'questions': []}
    mc_limit = config['lesson']['max_multiple_choice_questions']
    for i, uid in enumerate(tested_units[:mc_limit]):
        template['questions'].append({'question_id': f'Q-{i+1}', 'type': 'multiple_choice', 'unit_ids': [uid], 'prompt': '', 'options': [], 'correct_index': 0,
            'reference_answer': '', 'expected_points': {uid: []}, 'evidence_ids': [e['evidence_id'] for e in evidence if e['unit_id'] == uid]})
    # One open question can assess several points, each with its own scoring rubric.
    remainder = tested_units[mc_limit:]
    if remainder:
        template['questions'].append({'question_id': f'Q-{mc_limit+1}', 'type': 'open_ended', 'unit_ids': remainder, 'prompt': '', 'options': [], 'correct_index': None,
            'reference_answer': '', 'expected_points': {uid: [] for uid in remainder}, 'evidence_ids': [e['evidence_id'] for e in evidence if e['unit_id'] in remainder]})
    store(run / 'lesson-decision.template.json', template)
    model['current_lesson_id'] = lesson['lesson_id']
    transition(model, 'lesson_decision_required', 'prepare_lesson')
    save(project, model, config)
    return {'status': model['state'], 'template': str(run / 'lesson-decision.template.json'), 'evidence': str(run / 'evidence.json')}

def lesson_md(model, lesson, content):
    titles = {u['unit_id']: u['title'] for u in model['units']}
    by_id = {l['lesson_id']: l for l in model['lessons']}
    rows = []
    for uid in lesson['prerequisite_unit_ids']:
        providers = [by_id[x]['number'] for x in model['coverage'][uid]]
        p = model['unit_progress'][uid]
        rows.append([titles[uid], '、'.join('课程 '+x for x in providers) or '暂无对应课程', '已学' if p['status'] == 'completed' else '待学（已跳过）' if p['skip'] else '待学'])
    text = f"# 课程 {lesson['number']}｜{lesson['title']}\n\n## 前置知识\n\n"
    text += markdown_table(['知识点', '所在课程', '基础状态'], rows) if rows else '无前置知识要求。'
    text += '\n\n## 本课学习的知识点\n\n' + '、'.join(titles[x] for x in lesson['teaches_unit_ids']) + '\n\n'
    text += content['overview'] + '\n\n' + '\n\n'.join(p['text'] for p in content['teaching_points'])
    if content['guiding_questions']: text += '\n\n## 思考与讨论\n\n' + '\n'.join('- '+x for x in content['guiding_questions'])
    text += '\n\n## 正式习题\n\n请填写下方作答区域，也可以在对话中按题号回答。\n'
    for i, q in enumerate(content['questions'], 1):
        text += f"\n### 习题 {i}｜{'选择题' if q['type'] == 'multiple_choice' else '问答题'}\n\n{q['prompt']}\n\n"
        if q['type'] == 'multiple_choice': text += '\n'.join(f'{chr(65+j)}. {o}' for j,o in enumerate(q['options'])) + '\n\n'
        text += f"<!-- answer:{q['question_id']}:start -->\n> **✍️ 在这里作答**\n>\n> **{'我的选择' if q['type'] == 'multiple_choice' else '我的回答'}：**\n>\n> ⟦请将这行替换为你的回答，可分段填写⟧\n<!-- answer:{q['question_id']}:end -->\n\n---\n"
    terms = model.get('bilingual_terms', [])
    if terms:
        text += '\n## 双语术语\n\n' + markdown_table(['原文术语','中文译法','说明'], [[t['source_term'],t['target_term'],t['note']] for t in terms]) + '\n'
    text = re.sub(r'\bunit(?:s)?\b', '知识点', re.sub(r'\blesson(?:s)?\b', '课程', text, flags=re.I), flags=re.I)
    by_unit = {u['unit_id']: u for u in model['units']}
    pointers = []
    for uid in lesson['teaches_unit_ids']:
        for v in by_unit[uid].get('visual_references', []):
            material = next((m for m in read_json(manifest_path(Path(model['material_project'])))['materials'] if m['original_path'] == v['source_path']), None)
            if material is None:
                raise ValueError('图片来源未登记在学习材料备份中，禁止回退到源文件')
            display_path = material['backup_path']
            pointers.append(f"- 请到 `{display_path}` 的“{v['heading_text']}”查看第 {v['image_index_in_section']} 张图：{v['purpose']}")
    if pointers: text += '\n## 资料图片指引\n\n' + '\n'.join(pointers) + '\n'
    return text

def publish(project, model, config, decision):
    if model['state'] != 'lesson_decision_required': raise ValueError('请先准备课程')
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id'])
    if decision['lesson_id'] != lesson['lesson_id']: raise ValueError('决策对应课程错误')
    required = set(lesson['teaches_unit_ids']) - {x for x,p in model['unit_progress'].items() if p['skip']}
    if len(lesson['teaches_unit_ids']) > config['lesson']['max_new_units']:
        raise ValueError('课程知识点超过当前配置上限，请先拆分尚未发布课程')
    evidence = read_json(Path(model['run_dir']) / 'evidence.json')['evidence']
    valid = {e['evidence_id'] for e in evidence}
    for p in decision['teaching_points']:
        if p['unit_id'] not in lesson['teaches_unit_ids'] or not p['text'].strip(): raise ValueError('讲解知识点或内容无效')
        if not set(p['evidence_ids']) <= valid or (not p['external_explanation'] and not p['evidence_ids']): raise ValueError('讲解证据无效')
    if not required <= {p['unit_id'] for p in decision['teaching_points']}: raise ValueError('讲解未覆盖本课知识点')
    qs = decision['questions']; seen = set(); coverage = set()
    if not decision['overview'].strip() or not decision['key_points']: raise ValueError('请填写课程概述与核心要点')
    if sum(q['type'] == 'multiple_choice' for q in qs) > config['lesson']['max_multiple_choice_questions'] or sum(q['type'] == 'open_ended' for q in qs) > config['lesson']['max_open_ended_questions']:
        raise ValueError('正式习题超过配置上限')
    for q in qs:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', q['question_id']) or q['question_id'] in seen: raise ValueError('题目 ID 无效或重复')
        seen.add(q['question_id'])
        if q['type'] not in ('multiple_choice','open_ended') or not q['prompt'].strip() or not q['reference_answer'].strip(): raise ValueError('题目缺少正文或参考答案')
        if not set(q['unit_ids']) <= set(lesson['teaches_unit_ids']) or not q['unit_ids'] or len(set(q['unit_ids'])) != len(q['unit_ids']): raise ValueError('题目知识点引用无效')
        if set(q['expected_points']) != set(q['unit_ids']) or any(not points or any(not isinstance(p, str) or not p.strip() for p in points) for points in q['expected_points'].values()): raise ValueError('每个受测知识点都需要独立评分点')
        if not q['evidence_ids'] or not set(q['evidence_ids']) <= valid: raise ValueError('题目缺少有效证据')
        if q['type'] == 'multiple_choice':
            if len(q['unit_ids']) != 1 or len(q['options']) != 4 or len(set(q['options'])) != 4 or any(not isinstance(o, str) or not o.strip() for o in q['options']) or type(q['correct_index']) is not int or q['correct_index'] not in range(4):
                raise ValueError('正式选择题需要四个选项并独立检验一个知识点')
        elif q['options'] or q['correct_index'] is not None:
            raise ValueError('问答题不应包含选择题选项或正确选项索引')
        coverage.update(q['unit_ids'])
    if not required <= coverage: raise ValueError('每个本课学习知识点至少需要一道正式习题检验')
    if lesson.get('content'):
        lesson.setdefault('publication_history', []).append({
            'at': iso_timestamp(), 'content': copy.deepcopy(lesson['content']),
            'markdown': lesson['markdown_template'], 'answers': copy.deepcopy(lesson.get('answers', {})),
            'review': copy.deepcopy(lesson.get('review'))})
    lesson.pop('checked_answer_hash', None)
    lesson['content'] = decision; lesson['status'] = 'awaiting_answer'
    lesson['podcast'] = {'requested': config['podcast']['enabled'], 'status': 'not_implemented' if config['podcast']['enabled'] else 'disabled'}
    md = lesson_md(model, lesson, decision)
    lesson['markdown_template'] = md
    for uid in required:
        if model['unit_progress'][uid]['status'] != 'completed': model['unit_progress'][uid]['status'] = 'awaiting_answer'
    transition(model, 'awaiting_answer', 'publish_lesson')
    save(project, model, config)
    write_text_atomic(project / '课程' / lesson['filename'], md)
    return {'status': model['state'], 'lesson': str(project/'课程'/lesson['filename']), 'podcast_message': '播客功能待实现' if config['podcast']['enabled'] else ''}

def normalize_answers(text):
    return re.sub(r'<!-- answer:([^:]+):start -->.*?<!-- answer:\1:end -->', lambda m: '<!-- answer:'+m[1]+':start --><!-- answer:'+m[1]+':end -->', text, flags=re.S)

def collect_answers(project, model, answers=None):
    if model['state'] not in ('awaiting_answer', 'review_decision_required'): raise ValueError('当前不接受作答')
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id'])
    if answers is not None:
        lesson.pop('checked_answer_hash', None)
    if answers is None:
        text = (project/'课程'/lesson['filename']).read_text(encoding='utf-8-sig')
        if normalize_answers(text) != normalize_answers(lesson['markdown_template']): raise ValueError('教学正文已变化，仅允许编辑作答区域')
        lesson['checked_answer_hash'] = json_digest(text)
        answers = {}
        for q in lesson['content']['questions']:
            match = re.search(r'<!-- answer:'+re.escape(q['question_id'])+r':start -->(.*?)<!-- answer:'+re.escape(q['question_id'])+r':end -->', text, re.S)
            block = match[1] if match else ''
            if '⟦' in block: raise ValueError('仍有未填写的作答区域')
            value = re.sub(r'>\s*\*\*.*?\*\*', '', block).replace('>', '').strip()
            answers[q['question_id']] = value
    if not isinstance(answers, dict) or set(answers) != {q['question_id'] for q in lesson['content']['questions']} or any(not isinstance(x, str) or not x.strip() for x in answers.values()):
        raise ValueError('请逐题提交完整答案')
    lesson['answers'] = answers
    review = {'lesson_id': lesson['lesson_id'], 'scores': [], 'strengths': '', 'weaknesses': ''}
    for q in lesson['content']['questions']:
        if q['type'] == 'multiple_choice':
            score = multiple_choice_score(q, answers[q['question_id']])
        else: score = None
        review['scores'].append({'question_id': q['question_id'], 'unit_scores': {x: score for x in q['unit_ids']},
                                  'feedback': '', 'misconceptions': []})
    store(Path(model['run_dir'])/'review.template.json', review)
    transition(model, 'review_decision_required', 'answers_received')
    return review

def multiple_choice_score(question, answer):
    answer = answer.strip().upper()
    selected = answer[0] if re.match(r'^[A-D](?:$|[.、\s：:])', answer) else answer
    return 1.0 if selected == chr(65+question['correct_index']) else 0.0

def review(project, model, config, data):
    if model['state'] != 'review_decision_required': raise ValueError('请先提交本课答案')
    validate_json_schema(data, ROOT/'utils/references/interactive-tutor-review-v3.schema.json')
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id'])
    if lesson.get('checked_answer_hash'):
        text = (project/'课程'/lesson['filename']).read_text(encoding='utf-8-sig')
        if json_digest(text) != lesson['checked_answer_hash']:
            raise ValueError('检查之后作答内容已变化，请重新提交答案')
    questions = {q['question_id']: q for q in lesson['content']['questions']}
    if data['lesson_id'] != lesson['lesson_id'] or {s['question_id'] for s in data['scores']} != set(questions) or len(data['scores']) != len(questions):
        raise ValueError('批改必须逐题覆盖当前课程')
    if not data['strengths'].strip() or not data['weaknesses'].strip(): raise ValueError('请明确指出掌握优点与不足；没有不足也需说明')
    values = {x: [] for x in lesson['teaches_unit_ids']}; lines = []
    for item in data['scores']:
        q = questions[item['question_id']]
        if set(item['unit_scores']) != set(q['unit_ids']): raise ValueError('评分点覆盖错误')
        for uid, score in item['unit_scores'].items():
            if type(score) not in (int,float) or not 0 <= score <= 1: raise ValueError('评分必须在 0～1')
            if q['type'] == 'multiple_choice':
                automatic = multiple_choice_score(q, lesson['answers'][q['question_id']])
                if score != automatic: raise ValueError('选择题评分与脚本判分不一致')
            values[uid].append(score)
            p = model['unit_progress'][uid]
            p['evidence_refs'] = list(dict.fromkeys([*p['evidence_refs'], f"课程 {lesson['number']} / {q['question_id']}"]))
        if any(x < 1 for x in item['unit_scores'].values()):
            if not item['misconceptions']: raise ValueError('答错或不完整时需填写薄弱点、普遍规律和纠正说明')
            weak_units = {uid for uid, score in item['unit_scores'].items() if score < 1}
            if not weak_units <= {error['unit_id'] for error in item['misconceptions']}:
                raise ValueError('每个回答不完整的知识点都需要错题归纳')
            for error in item['misconceptions']:
                if error['unit_id'] not in q['unit_ids'] or not all(str(error.get(x,'')).strip() for x in ('title','weakness','rule','correction')):
                    raise ValueError('错题总结必须引用本题知识点并包含规律')
                eid = json_digest([error['unit_id'], error['rule']])[:16]
                entry = next((e for e in model['errors'] if e['error_id'] == eid), None)
                if entry is None:
                    entry = {**error, 'error_id': eid, 'examples': []}; model['errors'].append(entry)
                prompt = q['prompt'] + ('\n\n' + '\n'.join(f'{chr(65+i)}. {o}' for i, o in enumerate(q['options'])) if q['type'] == 'multiple_choice' else '')
                example = {'lesson_number': lesson['number'], 'prompt': prompt, 'answer': str(lesson['answers'][q['question_id']]), 'reference_answer': q['reference_answer']}
                if example not in entry['examples']: entry['examples'].append(example)
        lines.append(f"- {q['question_id']}：{sum(item['unit_scores'].values())/len(item['unit_scores']):.0%}；{item['feedback']}")
    for uid, scores in values.items():
        p = model['unit_progress'][uid]
        if scores:
            p['mastery'] = sum(scores)/len(scores)
            if not p['skip']: p['status'] = 'completed'
    lesson['review'] = data; lesson['feedback'] = {'strengths': data['strengths'], 'weaknesses': data['weaknesses']}; lesson['status'] = 'completed'
    transition(model, 'awaiting_questions', 'review_and_documents_completed')
    save(project, model, config)
    feedback = '# 作答反馈\n\n' + '\n'.join(lines) + '\n\n## 掌握优点\n\n' + data['strengths'] + '\n\n## 需要加强\n\n' + data['weaknesses'] + '\n\n本课文档已更新。你还有疑问吗？\n'
    write_text_atomic(Path(model['run_dir'])/'feedback.md', feedback)
    return {'status': model['state'], 'feedback_markdown': feedback}

def questions_event(model, no_questions=False, question=None, answer=None, evidence_refs=None):
    if no_questions:
        if model['state'] != 'awaiting_questions': raise ValueError('当前不在课后答疑阶段')
        if question or answer: raise ValueError('结束答疑不能同时提交新问题')
        transition(model, 'ready', 'user_explicitly_has_no_questions')
    else:
        if not question or not answer: raise ValueError('请提交用户问题和回答')
        model['question_discussions'].append({'lesson_id': model['current_lesson_id'], 'question': question, 'answer': answer, 'evidence_refs': evidence_refs or [], 'at': iso_timestamp()})
        if model['state'] == 'awaiting_questions':
            transition(model, 'awaiting_questions', 'question_answered')
        else:
            model['events'].append({'at': iso_timestamp(), 'from': model['state'], 'to': model['state'], 'event': 'in_course_question_answered'})
    return {'status': model['state'], 'message': '可以准备下一课' if no_questions else '已记录答疑。你还有疑问吗？'}

def plan_patch(model, patches):
    for item in patches:
        op = item['operation']
        if op == 'add_chapter':
            model['chapters'].append({'chapter_id': item['chapter_id'], 'number': len(model['chapters'])+1, 'title': item['title']}); continue
        lesson = next((l for l in model['lessons'] if l['lesson_id'] == item.get('lesson_id')), None)
        if op == 'add':
            if lesson: raise ValueError('课程 ID 已存在')
            cid = item['chapter_id']
            number = 1 + max((l['section_number'] for l in model['lessons'] if l['chapter_id'] == cid), default=0)
            model['lessons'].append({'lesson_id': item['lesson_id'], 'chapter_id': cid, 'section_number': number,
                'title': item['title'], 'track': item.get('track','branch'), 'teaches_unit_ids': item['teaches_unit_ids'],
                'internal_unit_order': item.get('internal_unit_order', item['teaches_unit_ids']), 'prerequisite_providers': item.get('prerequisite_providers',{}),
                'status': 'pending', 'archived': False, 'skip': False})
        elif lesson is None: raise ValueError('课程不存在')
        elif op == 'delete': lesson['archived'] = True
        elif op == 'update':
            if lesson.get('content'): raise ValueError('已发布课程保留历史；请新增替代课程并退出旧课程')
            patch = item['patch']
            if not set(patch) <= {'title','track','teaches_unit_ids','internal_unit_order','prerequisite_providers'}: raise ValueError('不支持的课程字段')
            lesson.update(patch)
        else: raise ValueError('未知计划操作')
    if model['state'] == 'completed' and any(l['status'] == 'pending' and not l['archived'] for l in model['lessons']):
        transition(model, 'ready', 'additional_course_planned')

def note_prepare(model, project, draft):
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == draft['lesson_id'])
    if not lesson.get('content'): raise ValueError('笔记必须来自已发布课程')
    if not all(draft.get(k,'').strip() for k in ('title','location','background','text')): raise ValueError('笔记缺少内容定位或整理稿')
    note = {**draft, 'lesson_number': lesson['number']}
    note['source_hash'] = json_digest(lesson['content'])
    note['confirmation_state'] = 'awaiting_confirmation'
    note['draft_hash'] = json_digest(note)
    store(Path(model['run_dir'])/'note-draft.json', note)
    return {'status': 'awaiting_note_confirmation', 'draft_hash': note['draft_hash'],
            'preview_markdown': f"## {note['title']}\n\n{note['background']}\n\n{note['text']}"}

def note_confirm(model, digest, confirmed_by):
    note = read_json(Path(model['run_dir'])/'note-draft.json')
    payload = {k: v for k, v in note.items() if k != 'draft_hash'}
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == note['lesson_id'])
    if note['draft_hash'] != digest or json_digest(payload) != digest or json_digest(lesson['content']) != note['source_hash'] or not confirmed_by.strip():
        raise ValueError('需要用户对当前笔记稿明确确认；草稿或来源变化后请重新确认')
    if not any(n['draft_hash'] == digest for n in model['notes']):
        model['notes'].append({**note, 'confirmation_state': 'confirmed', 'confirmed_by': confirmed_by, 'confirmed_at': iso_timestamp()})

def verify(project, model, config):
    verify_backup(Path(model['workspace_root']), project, read_json(Path(model['navigation_json'])))
    rebuild(model, config)
    meta = read_json(project/'项目.json')
    if meta['revision'] != model['revision']: raise ValueError('项目版本不一致')
    verify_roadmap_flowcharts(model, (project/'学习路线.md').read_text(encoding='utf-8'))
    for name, (value, text) in documents(model, project, config).items():
        if read_json(art(project, name)) != value or (project/f'{name}.md').read_text(encoding='utf-8') != text:
            raise ValueError('JSON/Markdown 不一致：' + name)
    for lesson in model['lessons']:
        if not lesson.get('content'): continue
        path = project/'课程'/lesson['filename']
        if read_json(project/'artifacts/lessons'/Path(lesson['filename']).with_suffix('.json')) != lesson:
            raise ValueError('课程 JSON 不一致')
        if normalize_answers(path.read_text(encoding='utf-8-sig')) != normalize_answers(lesson['markdown_template']): raise ValueError('课程正文不一致')
    course_dir = project/'课程'
    if any(p.is_file() and p.name != '项目.json' and p.suffix != '.md' for p in project.iterdir()) or (course_dir.exists() and any(p.is_file() and p.suffix != '.md' for p in course_dir.iterdir())):
        raise ValueError('JSON 布局不符合约定')
    return {'status': 'verified', 'revision': model['revision'], 'state': model['state']}
