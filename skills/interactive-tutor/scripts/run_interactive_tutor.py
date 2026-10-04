"""Public CLI for the project-based tutor state machine (contract v3)."""
from pathlib import Path
import argparse
import json
import sys
import copy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts import learning_project as lp
from utils.scripts.learning_config import freeze_config, acknowledge_config
from utils.scripts.structured_io import read_json, write_json, validate_json_schema, structured_error_receipt
from utils.scripts.bilingual_glossary import render_markdown
from utils.scripts import learning_project_selection as selection

def parser():
    p = argparse.ArgumentParser(description='可恢复交互式学习；先导航、后课程，批改后必须答疑')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-request'); init.add_argument('--file', type=Path, required=True); init.add_argument('--input')
    start = sub.add_parser('start'); start.add_argument('--root', type=Path, default=Path.cwd()); start.add_argument('--request', type=Path)
    start.add_argument('--backup-plan-sha256')
    listing = sub.add_parser('list-projects'); listing.add_argument('--root', type=Path, default=Path.cwd())
    for name in ('select-project', 'select-tutor'):
        item = sub.add_parser(name)
        item.add_argument('--selection-dir', type=Path, required=True)
        item.add_argument('--choice', required=True)
        item.add_argument('--catalog-sha256', required=True)
    retry = sub.add_parser('resume-selection'); retry.add_argument('--selection-dir', type=Path, required=True)
    for name in ('suggest-project-name', 'confirm-project-name', 'creation-status'):
        item = sub.add_parser(name); item.add_argument('--creation-dir', type=Path, required=True)
        if name == 'suggest-project-name': item.add_argument('--decision', type=Path, required=True)
        if name == 'confirm-project-name':
            item.add_argument('--name', required=True); item.add_argument('--proposal-sha256', required=True)
    glossary = sub.add_parser('init-bilingual-glossary'); glossary.add_argument('--file', type=Path, required=True)
    for name in ('status','resume','prepare-lesson','publish-lesson','check-answers','submit-chat-answer','review-answers','report','verify',
                 'questions','plan','skip','restore','note-prepare','note-confirm','supply-navigation',
                 'apply-adaptation', 'choose-route', 'confirm-plan', 'deliver-feedback'):
        item = sub.add_parser(name)
        item.add_argument('--backup-plan-sha256')
        target = item.add_mutually_exclusive_group(required=True)
        target.add_argument('--project-dir', type=Path); target.add_argument('--run-dir', type=Path)
        if name == 'prepare-lesson':
            item.add_argument('--lesson'); item.add_argument('--allow-missing', action='store_true')
        if name == 'resume': item.add_argument('--podcast-response', type=Path)
        if name == 'publish-lesson':
            item.add_argument('--podcast-mock', action='store_true')
            item.add_argument('--quality-review', type=Path)
            item.add_argument('--layout-review', type=Path)
        if name in ('publish-lesson','review-answers','plan','note-prepare'):
            item.add_argument({'publish-lesson':'--decision','review-answers':'--review','plan':'--patch','note-prepare':'--draft'}[name], type=Path, required=True)
        if name == 'submit-chat-answer': item.add_argument('--answers', type=Path, required=True)
        if name == 'apply-adaptation': item.add_argument('--decision', type=Path, required=True)
        if name == 'choose-route':
            item.add_argument('--choice', required=True); item.add_argument('--choice-sha256', required=True)
        if name == 'confirm-plan': item.add_argument('--revision', type=int, required=True)
        if name == 'deliver-feedback':
            item.add_argument('--lesson', required=True)
            item.add_argument('--review-sha256', required=True)
            item.add_argument('--feedback-sha256', required=True)
        if name in ('check-answers','submit-chat-answer'): item.add_argument('--lesson')
        if name == 'questions':
            item.add_argument('--no-questions', action='store_true'); item.add_argument('--question'); item.add_argument('--answer'); item.add_argument('--evidence-refs', nargs='*')
        if name in ('skip','restore'):
            item.add_argument('--target-type', choices=['lesson','unit'], required=True); item.add_argument('--target-id', required=True)
        if name == 'note-confirm': item.add_argument('--draft-hash', required=True); item.add_argument('--confirmed-by', required=True)
        if name == 'supply-navigation': item.add_argument('--navigation', type=Path, required=True)
    return p

def project_path(args):
    if args.project_dir: return lp.safe_path(ROOT, args.project_dir)
    run = lp.safe_path(ROOT, args.run_dir)
    return lp.safe_path(ROOT, read_json(run/'state.json')['project_dir'])

def execute(args):
    if args.command in ('suggest-project-name', 'confirm-project-name', 'creation-status'):
        from utils.scripts import learning_project_creation as creation
        if args.command == 'suggest-project-name': return creation.suggest(args.creation_dir, read_json(args.decision))
        if args.command == 'confirm-project-name': return creation.confirm(args.creation_dir, args.name, args.proposal_sha256)
        data, workflow = creation.load(args.creation_dir)
        return creation.receipt(args.creation_dir, data, workflow.state)
    if args.command == 'list-projects' or (args.command == 'start' and args.request is None):
        return selection.start(args.root)
    if args.command == 'resume-selection':
        return selection.resume(args.selection_dir)
    if args.command in ('select-project', 'select-tutor'):
        def finish(mode, root, navigation, project, request):
            if mode == 'new':
                request = {**request, 'schema_version': '3.0', 'input_paths': [str(navigation)],
                           'interaction_mode': request.get('interaction_mode', 'file_driven')}
                output = lp.safe_path(root, request['project_dir']) if request.get('project_dir') else None
                return lp.create(root, navigation, output, request)
            # Resume through the existing CLI context so teaching/backup/question gates remain active.
            return execute(parser().parse_args(['resume', '--project-dir', str(project)]))
        return selection.select(args.selection_dir, args.choice, args.catalog_sha256,
                                tutor=args.command == 'select-tutor', finish=finish)
    if args.command == 'init-request':
        if args.file.exists(): raise ValueError('请求文件已存在')
        write_json(args.file, {'schema_version':'3.0','input_paths':[args.input] if args.input else [], 'interaction_mode':'file_driven','project_dir':None})
        return {'status':'prepared','request':str(args.file)}
    if args.command == 'init-bilingual-glossary':
        if args.file.exists(): raise ValueError('术语表已存在')
        from utils.scripts.structured_io import write_text_atomic
        write_text_atomic(args.file, '| Source term | Target term | Note |\n|---|---|---|\n')
        return {'status':'prepared','file':str(args.file)}
    if args.command == 'start':
        req = read_json(args.request)
        validate_json_schema(req, ROOT/'utils/references/interactive-tutor-request-v2.schema.json')
        if not req['input_paths']:
            return selection.start(args.root, req)
        if len(req['input_paths']) != 1 or not req['input_paths'][0].endswith('.json'):
            return {'status':'navigation_required','message':'单份资料也必须先调用 build-curriculum-navigation'}
        root = args.root.resolve()
        nav = lp.safe_path(root, req['input_paths'][0])
        return lp.create(root, nav, lp.safe_path(root, req['project_dir']) if req.get('project_dir') else None, req, args.backup_plan_sha256)
    project = project_path(args)
    with lp.lock(project):
        if args.command == 'verify':
            model = lp.load(project)
            lp.navigation_preflight(Path(model.get('workspace_root', ROOT)), Path(model['navigation_json']), material_project=project)
            receipt = freeze_config(lp.TUTOR, Path(model['run_dir']))
        elif args.command == 'supply-navigation':
            model = lp.load(project)
            receipt = freeze_config(lp.TUTOR, Path(model['run_dir']))
        else:
            model, receipt = lp.context(project, approved_plan_sha256=args.backup_plan_sha256)
        cfg = receipt['config']
        cmd = args.command
        if cmd in ('status','resume','report'):
            if cmd == 'resume' and model['state'] == 'podcast_required':
                from utils.scripts.learning_lesson_podcast import advance
                result = advance(project, model, cfg, read_json(args.podcast_response) if args.podcast_response else None)
                result['config_message'] = receipt['message']
                acknowledge_config(Path(model['run_dir']))
                return result
            if cmd == 'resume' and args.podcast_response: raise ValueError('当前没有待完成的播客')
            if cmd == 'resume':
                from utils.scripts.learning_lesson_podcast import recover_published
                recover_published(project, model, model.get('render_config', cfg))
            feedback = lp.review_delivery.pending(model)
            lp.save(project, model, cfg)
            result = {**lp.startup_receipt(project, model), 'current_lesson_id': model['current_lesson_id'],
                      'message':'你还有疑问吗？明确没有疑问后再生成下一课。' if model['state']=='awaiting_questions' else ''}
            if model['state'] in ('awaiting_route_choice', 'awaiting_branch_continuation', 'main_completed'):
                result.update(lp.learning_route.receipt(model))
            if model['state'] == 'adaptation_decision_required': result['adaptation_template'] = str(Path(model['run_dir'])/'adaptation.template.json')
            if model['state'] == 'lesson_plan_required': result['plan_template'] = str(Path(model['run_dir'])/'plan.template.json')
            if model['state'] == 'lesson_decision_required':
                result['message'] = '请填写课程模板并执行 publish-lesson；若返回上下文排版或讲解复核模板，补充或复核后重新提交。'
            if feedback:
                result.update(feedback)
        elif cmd == 'verify': result = lp.verify(project, model, model.get('render_config', cfg))
        elif cmd == 'prepare-lesson': result = lp.prepare(project, model, cfg, Path(model.get('workspace_root', ROOT)), args.lesson, args.allow_missing)
        elif cmd == 'publish-lesson':
            decision = read_json(args.decision)
            if decision.get('schema_version') != '7.0':
                raise ValueError('新发布课程必须使用 v7 parts 模板；历史 v4/v5/v6 讲义和批改只读兼容，不自动重写')
            version = 'v7'
            validate_json_schema(decision, ROOT/f'utils/references/interactive-tutor-lesson-decision-{version}.schema.json')
            result = lp.publish(project, model, cfg, decision, podcast_mock=args.podcast_mock,
                                quality_review=read_json(args.quality_review) if args.quality_review else None,
                                layout_review=read_json(args.layout_review) if args.layout_review else None)
        elif cmd in ('check-answers','submit-chat-answer'):
            if args.lesson and args.lesson != model['current_lesson_id']: raise ValueError('答案对应课程错误')
            template = lp.collect_answers(project, model, read_json(args.answers) if cmd == 'submit-chat-answer' else None)
            lp.save(project, model, cfg)
            result = {'status':model['state'], 'review_template':str(Path(model['run_dir'])/'review.template.json')}
        elif cmd == 'review-answers': result = lp.review(project, model, cfg, read_json(args.review))
        elif cmd == 'deliver-feedback':
            result = lp.deliver_feedback(project, model, cfg, args.lesson, args.review_sha256, args.feedback_sha256)
        elif cmd == 'apply-adaptation': result = lp.apply_adaptation(project, model, cfg, read_json(args.decision))
        elif cmd == 'confirm-plan': result = lp.confirm_plan(project, model, cfg, args.revision)
        elif cmd == 'choose-route':
            result = lp.learning_route.choose(model, args.choice, args.choice_sha256, lp.transition)
            lp.save(project, model, cfg)
        elif cmd == 'questions':
            result = lp.questions_event(model, args.no_questions, args.question, args.answer, args.evidence_refs)
            lp.save(project, model, cfg)
        elif cmd == 'plan':
            if model['state'] not in ('ready','awaiting_questions','completed','lesson_decision_required','lesson_plan_required','awaiting_route_choice','awaiting_branch_continuation','main_completed'): raise ValueError('请先完成当前作答再调整课程')
            patch = read_json(args.patch)
            validate_json_schema(patch, ROOT/'utils/references/interactive-tutor-plan-patch-v1.schema.json')
            if patch['base_revision'] != model['revision']: raise ValueError('路线版本冲突')
            if model['state'] == 'lesson_decision_required':
                lp.transition(model,'ready','unpublished_course_replanned')
                model['current_lesson_id'] = None
            lp.plan_patch(model, patch['operations']); lp.save(project, model, cfg)
            result = {'status':model['state'],'revision':model['revision']}
        elif cmd in ('skip','restore'):
            if args.target_type == 'unit' and args.target_id not in model['unit_progress']:
                raise ValueError('调整指令引用未知知识点')
            if args.target_type == 'lesson' and not any(l['lesson_id'] == args.target_id and not l['archived'] for l in model['lessons']):
                raise ValueError('调整指令引用未知课程')
            action = {'action_id':lp.json_digest([model['revision'],cmd,args.target_type,args.target_id]), 'target_type':args.target_type,'target_id':args.target_id,'operation':'skip' if cmd=='skip' else 'restore'}
            path = lp.art(project,'调整指令'); data = read_json(path) if path.is_file() else {'actions':[]}
            data['actions'].append(action); lp.store(path,data)
            if model['state'] != 'review_decision_required': lp.apply_actions(project,model)
            lp.save(project,model,cfg)
            result = {'status':model['state'],'revision':model['revision']}
        elif cmd == 'note-prepare': result = lp.note_prepare(model,project,read_json(args.draft))
        elif cmd == 'note-confirm':
            lp.note_confirm(model,args.draft_hash,args.confirmed_by); lp.save(project,model,cfg); result={'status':'note_saved'}
        elif cmd == 'supply-navigation':
            root = Path(model.get('workspace_root', ROOT))
            lp.verify_input_snapshots(model, root)
            if model['state'] not in ('ready', 'completed', 'awaiting_questions'):
                raise ValueError('请先结束当前作答与批改，再同步导航和材料')
            nav_path = lp.safe_path(root,args.navigation)
            nav = lp.navigation_preflight(root,nav_path)
            updated = copy.deepcopy(model)
            lp.sync_navigation(updated,nav)
            backup = lp.ensure_backup(root,project,nav,Path(model['run_dir']),args.backup_plan_sha256,refresh=True,publish=False)
            updated['navigation_json'] = str(nav_path)
            updated['material_project'] = str(project)
            lp.save(project,updated,cfg,material_backup=backup); result={'status':updated['state']}
        else: raise ValueError('未知命令')
        result['config_message'] = receipt['message']
        acknowledge_config(Path(model['run_dir']))
        return result

def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result,ensure_ascii=False))
        if args.command == 'creation-status': return 0
        status = (result.get('result') or result).get('status')
        return 3 if status in ('navigation_required','blocked_prerequisites','awaiting_backup_approval',
                              'awaiting_name_suggestion','awaiting_name_confirmation','podcast_required',
                              'awaiting_project_selection','awaiting_tutor_selection','lesson_plan_required',
                              'adaptation_decision_required','awaiting_feedback_questions','awaiting_route_choice',
                              'awaiting_branch_continuation','main_completed',
                              'teaching_review_required','teaching_revision_required','feedback_delivery_required',
                              'teaching_layout_review_required','teaching_layout_revision_required') else 0
    except (lp.BackupApprovalRequired, lp.BackupConflict) as exc:
        print(json.dumps(exc.receipt,ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps(structured_error_receipt(exc),ensure_ascii=False))
        return 4 if args.command=='verify' else 6 if isinstance(exc,ImportError) else 2 if isinstance(exc,(ValueError,KeyError,StopIteration)) else 5

if __name__ == '__main__': raise SystemExit(main())
