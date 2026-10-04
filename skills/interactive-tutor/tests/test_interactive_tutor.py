"""Contract v3 regression and public CLI/HTTP smoke tests. All fixtures synthetic."""
from pathlib import Path
import copy
import importlib.util
import json
import subprocess
import sys
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from utils.scripts import learning_project as lp, adaptive_assessment as aa
from utils.scripts.learning_config import load_config, save_config, freeze_config, SKILLS
from utils.scripts.learning_navigation import render_navigation_markdown
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp
from utils.scripts.workflow_checkpoint import create_run_directory
from utils.scripts.structured_io import read_json, write_json

def import_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
runner=import_module('new_tutor_runner',ROOT/'skills/interactive-tutor/scripts/run_interactive_tutor.py')

@pytest.fixture
def workspace(monkeypatch):
    # Run artifacts stay in outputs; text receipts/state stay in logs.
    base=ROOT/'outputs/interactive-learning-tests/runs'
    root = create_run_directory(base)
    configs={
        SKILLS[0]:{'assessment':{'max_multiple_choice_questions':5},
                   'ordering':{'max_candidate_orders':10,'weights':{'goal_match':.4,'background_match':.3,
                                'difficulty_smoothness':.15,'topic_continuity':.1,'downstream_unlock':.05}}},
        SKILLS[1]:{'lesson':{'max_multiple_choice_questions':3,'max_open_ended_questions':2,'max_new_units':3},
                   'mastery':{'good_min':.8,'medium_min':.6},'podcast':{'enabled':False}},
    }
    for skill in SKILLS:
        file=root/'skills'/skill/'config.yaml';file.parent.mkdir(parents=True)
        file.write_text(yaml.safe_dump(configs[skill],allow_unicode=True,sort_keys=False),encoding='utf-8')
    # Teaching fixtures exercise the text-only flow even when the user's real
    # settings enable podcasts. Implicit config reads must also stay isolated.
    for function in (load_config,save_config,freeze_config):
        monkeypatch.setattr(function,'__defaults__',(root,))
    return root

def navigation(root,count=3):
    overview={'purpose':'学习测试知识','core_questions':['如何应用？'],'completion_criteria':['能说明方法']}
    units=[]
    for i in range(count):
        units.append({'unit_id':f'U-{i}','title':f'知识点 {i}','unit_type':'agent_overview','stage':'基础','module':'入门',
            'sequence':i+1,'importance':'required','difficulty':'beginner','purpose':'理解测试知识','learning_objectives':['能说明'],
            'prerequisites':[f'U-{i-1}'] if i else [],'concept_roles':[],'content_roles':[],'risk_tags':[],
            'teaching_notes':[],'case_context':None,'visual_references':[],'source':None})
    assessment=aa.begin(units,'初学者','学习测试知识',5)
    while aa.target(assessment,units):
        q={'unit_id':aa.target(assessment,units)['unit_id'],'prompt':'哪个结论符合说明？',
           'options':['正确','错误一','错误二','错误三',aa.UNKNOWN],'correct_index':0}
        aa.add_question(assessment,units,q);aa.answer(assessment,units,0)
    context={'current_level':'初学者','learning_goal':'学习测试知识','goal_unit_ids':assessment['goal_unit_ids'],'diagnostic_answers':assessment['answers']}
    nav={'schema_version':'2.0','title':'合成测试课程','created_at':iso_timestamp(),'updated_at':iso_timestamp(),
         'student_profile_ref':None,'planning_profile':{'assessment':assessment,'route_context':context,'ordering':aa.candidates(units,context,load_config(SKILLS[0])['config'])},
         'course_overview':overview,'lesson_generation_policy':{'image_policy':'source_pointer_only'},'sources':[],'source_relationships':[],
         'stages':[{'stage_id':'ST','title':'基础','sequence':1,'overview':overview,'modules':[{'module_id':'MO','title':'入门','sequence':1,'overview':overview}]}],
         'units':units,'concept_index':{},'coverage_gaps':[],'deferred_items':[]}
    path=root/'navigation.json';write_json(path,nav);path.with_suffix('.md').write_text(render_navigation_markdown(nav),encoding='utf-8');path.with_suffix('.sources').mkdir()
    return path

def new_project(root,count=3):
    result=lp.create(root,navigation(root,count), confirmed_name="合成测试课程");return Path(result['project_dir'])

def legacy_decision(data):
    """Exercise the readable historical v4 contract without rewriting old lessons."""
    data['schema_version'] = '4.0'
    for point in data['teaching_points']: point.pop('blocks', None)
    mc_units = {u for q in data['questions'] if q['type'] == 'multiple_choice' for u in q['unit_ids']}
    data['questions'] = [q for q in data['questions'] if q['type'] == 'multiple_choice' or not set(q['unit_ids']) <= mc_units]
    return data


def decision(project,model,cfg,open_question=False):
    lp.prepare(project,model,cfg,Path(model['workspace_root']))
    data=legacy_decision(read_json(Path(model['run_dir'])/'lesson-decision.template.json'))
    data['overview']='本课说明测试方法';data['key_points']=['依据证据判断'];data['formulas']=['x = 1']
    for p in data['teaching_points']:p['text']='根据当前证据解释。'
    for q in data['questions']:
        q['prompt']='哪项符合测试说明？';q['options']=['正确','错误一','错误二','错误三'];q['reference_answer']='正确';q['expected_points']={x:['正确结论'] for x in q['unit_ids']}
        if open_question:q.update(type='open_ended',options=[],correct_index=None)
    return data

def acknowledge_feedback(project, model, cfg, result):
    return lp.deliver_feedback(project, model, cfg, result["lesson_id"], result["review_sha256"], result["feedback_sha256"])


def complete_lesson(project,incorrect=False,deliver=True):
    with lp.lock(project):
        model,receipt=lp.context(project);cfg=receipt['config'];data=decision(project,model,cfg)
        lp.publish(project,model,cfg,data)
        review=lp.collect_answers(project,model,{q['question_id']:'B' if incorrect else 'A' for q in data['questions']})
        lp.save(project,model,cfg)
        review.update(strengths='能明确表达作答依据',weaknesses='需要加强概念辨析' if incorrect else '当前习题未发现明显不足')
        for score in review['scores']:
            score['feedback']='请对照定义'
            if incorrect:score['misconceptions']=[{'unit_id':next(iter(score['unit_scores'])),'title':'概念辨析','weakness':'混淆条件','rule':'先检查定义与前提','correction':'对照条件再判断'}]
        result=lp.review(project,model,cfg,review)
        if deliver: acknowledge_feedback(project, model, cfg, result)
        return model,cfg,result

def test_nav_required_and_all_commands_registered(workspace):
    request=workspace/'request.json';write_json(request,{'schema_version':'3.0','input_paths':['material.md'],'interaction_mode':'chat'})
    assert runner.main(['start','--root',str(workspace),'--request',str(request)])==3
    names=runner.parser()._subparsers._group_actions[0].choices
    assert {'questions','plan','skip','restore','note-prepare','note-confirm','verify'}<=set(names)
    assert not {'assessment-start','assessment-import'}&set(names)

def test_review_waits_for_explicit_no_questions_and_updates_documents(workspace):
    project=new_project(workspace);model,cfg,result=complete_lesson(project,True)
    assert result['status']=='awaiting_questions'
    assert lp.prepare(project,model,cfg,workspace)['status']=='awaiting_questions'
    assert not (project/'课程/课程_1-2.md').exists()
    assert len(read_json(lp.art(project,'总结'))['lessons'])==1
    assert read_json(lp.art(project,'学习报告'))['aspects'][0]['classification']=='一般'
    errors=read_json(lp.art(project,'错题本'))['entries'];assert errors[0]['rule'] and errors[0]['examples']
    lp.questions_event(model,question='为什么？',answer='因为需要先检查定义。')
    assert model['state']=='awaiting_questions'
    lp.questions_event(model,no_questions=True);lp.save(project,model,cfg)
    assert lp.prepare(project,model,cfg,workspace)['status']=='lesson_decision_required'
    assert lp.verify(project,lp.load(project),cfg)['status']=='verified'
    assert set(p.name for p in project.glob('*.json'))=={'项目.json'}
    assert not list((project/'课程').glob('*.json'))

def test_answer_keys_hidden_and_body_tampering_rejected(workspace):
    project=new_project(workspace,1);model,receipt=lp.context(project);cfg=receipt['config'];data=decision(project,model,cfg)
    data['questions'][0]['reference_answer']='ONLY_INTERNAL_ANSWER'
    result=lp.publish(project,model,cfg,data);file=Path(result['lesson']);text=file.read_text(encoding='utf-8')
    assert 'ONLY_INTERNAL_ANSWER' not in text
    assert 'ONLY_INTERNAL_ANSWER' in (project/'artifacts/lessons/课程_1-1.json').read_text(encoding='utf-8')
    with pytest.raises(ValueError,match='未填写'):lp.collect_answers(project,model)
    text=text.replace('⟦请将这行替换为你的回答，可分段填写⟧','A')
    file.write_text(text,encoding='utf-8');lp.collect_answers(project,model)
    model=lp.load(project);file.write_text(text.replace('本课说明测试方法','未授权正文更改'),encoding='utf-8')
    with pytest.raises(ValueError,match='正文'):lp.collect_answers(project,model)

def test_multipoint_coverage_and_repeated_teaching_graph(workspace):
    project=new_project(workspace);model,receipt=lp.context(project);cfg=receipt['config']
    first,second,third=model['lessons'];first['teaches_unit_ids']=['U-0','U-1'];first['internal_unit_order']=['U-0','U-1'];second['archived']=True
    lp.rebuild(model,cfg)
    assert model['coverage']['U-1']==[first['lesson_id']]
    assert model['lesson_edges'][0]['predecessor_id']==first['lesson_id']
    data=decision(project,model,cfg);data['questions']=data['questions'][:1]
    with pytest.raises(ValueError,match='习题检验'):lp.publish(project,model,cfg,data)
    model['state']='ready';first['teaches_unit_ids']=['U-0'];first['internal_unit_order']=['U-0']
    with pytest.raises(ValueError,match='缺少讲解'):lp.rebuild(model,cfg)

def test_limits_and_notebook_confirmation(workspace):
    project=new_project(workspace,1);model,receipt=lp.context(project);cfg=receipt['config'];data=decision(project,model,cfg)
    data['questions']=[{**data['questions'][0],'question_id':f'Q-{i}'} for i in range(4)]
    with pytest.raises(ValueError,match='上限'):lp.publish(project,model,cfg,data)
    data['questions']=data['questions'][:1];lp.publish(project,model,cfg,data)
    draft={'lesson_id':model['current_lesson_id'],'location':'核心解释','title':'定义笔记','background':'用于概念辨析','text':'先检查定义与前提。'}
    result=lp.note_prepare(model,project,draft)
    assert read_json(lp.art(project,'笔记本'))['entries']==[]
    with pytest.raises(ValueError):lp.note_confirm(model,'bad','合成用户')
    lp.note_confirm(model,result['draft_hash'],'合成用户');lp.save(project,model,cfg)
    assert '定义笔记' in (project/'笔记本.md').read_text(encoding='utf-8')

def test_config_revision_freeze_and_invalid_weights(workspace):
    initial=load_config(SKILLS[0],workspace);value=copy.deepcopy(initial['config']);value['ordering']['max_candidate_orders']=2
    run=workspace/'logs/config-test';freeze_config(SKILLS[0],run,workspace)
    saved=save_config(SKILLS[0],value,initial['revision'],workspace)
    assert freeze_config(SKILLS[0],run,workspace)['changed']
    assert list((run/'config-history').glob('*/config.yaml'))
    with pytest.raises(ValueError,match='版本冲突'):save_config(SKILLS[0],value,initial['revision'],workspace)
    value['ordering']['weights']['goal_match']=.9
    with pytest.raises(ValueError,match='总和'):save_config(SKILLS[0],value,saved['revision'],workspace)

def test_adaptive_unknown_and_candidate_retention(workspace):
    nav=read_json(navigation(workspace,4));units=nav['units']
    units[2]['prerequisites']=[];units[3]['prerequisites']=[]
    assessment=aa.begin(units,'初学','综合目标',1)
    uid=aa.target(assessment,units)['unit_id'];q={'unit_id':uid,'prompt':'你是否听过概念？','options':['A','B','C','D',aa.UNKNOWN],'correct_index':0}
    with pytest.raises(ValueError,match='听过'):aa.add_question(assessment,units,q)
    q['prompt']='哪项定义正确？';aa.add_question(assessment,units,q);aa.answer(assessment,units,4)
    assert assessment['answers'][0]['observation']=='uncertain' and assessment['status']=='completed'
    cfg=load_config(SKILLS[0])['config'];ranked=aa.candidates(units,{},cfg)
    assert len(ranked['candidates'])<=10
    scores=[x['score'] for x in ranked['candidates']];assert scores==sorted(scores,reverse=True)
    assert len({tuple(x['unit_ids']) for x in ranked['candidates']})==len(scores)

def test_selected_navigation_route_is_preserved(workspace):
    path=navigation(workspace,3);nav=read_json(path)
    for unit in nav['units']:unit['prerequisites']=[]
    nav['planning_profile']['assessment']['graph_revision']=aa.graph_revision(nav['units'])
    ranked=aa.candidates(nav['units'],nav['planning_profile']['route_context'],load_config(SKILLS[0])['config'])
    chosen=next(r for r in ranked['candidates'] if r['unit_ids'][0]!='U-0')
    ranked['selected_order_id']=chosen['candidate_id'];nav['planning_profile']['ordering']=ranked
    write_json(path,nav);path.with_suffix('.md').write_text(render_navigation_markdown(nav),encoding='utf-8')
    project=Path(lp.create(workspace,path, confirmed_name="合成测试课程")['project_dir']);model,receipt=lp.context(project)
    assert model['candidate_orders']['selected_order_id']==chosen['candidate_id']
    lp.prepare(project,model,receipt['config'],workspace)
    lesson=next(l for l in model['lessons'] if l['lesson_id']==model['current_lesson_id'])
    assert lesson['teaches_unit_ids']==[chosen['unit_ids'][0]]

def test_http_interfaces_and_pending_skip_are_idempotent(workspace,monkeypatch):
    project=new_project(workspace);model=lp.load(project)
    service=import_module('new_learning_service',ROOT/'applications/交互式学习/scripts/workspace_service.py')
    monkeypatch.setattr(service,'ROOT',workspace)
    server=service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def call(path,method='GET',body=None):
        req=Request(base+path,method=method,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json'} if body is not None else {})
        with urlopen(req,timeout=10) as response:return json.load(response)
    try:
        assert call('/health')['protocolVersion']==3
        assert call('/api/ui-config')['poll_interval_seconds']==5
        assert call('/api/projects')[0]['id']==model['project_id']
        prefix='/api/projects/'+model['project_id'];snapshot=call(prefix)
        assert call(prefix+'/version')['revision']==snapshot['revision']
        assert len(snapshot['lessons'])==3 and len(snapshot['units'])==3
        settings=call('/api/config')
        for skill in SKILLS:
            value=settings[skill]
            assert call('/api/config/'+skill,'PUT',{'config':value['config'],'expectedRevision':value['revision']})['revision']==value['revision']
        # A direct node change must trigger polling even if the numeric revision was not updated.
        edited=lp.load(project);edited['unit_progress']['U-1']['status']='completed';lp.store(lp.art(project,'学习路线'),edited)
        changed=call(prefix);assert changed['revision']!=snapshot['revision'];snapshot=changed
        action={'actionId':'TEST_ACTION','expectedRevision':snapshot['revision'],'targetType':'unit','targetId':'U-0','operation':'skip'}
        first=call(prefix+'/actions','POST',action);second=call(prefix+'/actions','POST',action)
        assert first['revision']==second['revision'] and first['pending']['unit:U-0']
        assert lp.load(project)['unit_progress']['U-0']['status']=='pending'
        with lp.lock(project):
            current,receipt=lp.context(project);lp.save(project,current,receipt['config'])
        assert call(prefix)['units'][0]['skip']
        # Explicitly skipping a foundation authorizes continuing, without claiming it was learned.
        current=lp.load(project)
        assert lp.prepare(project,current,receipt['config'],workspace)['status']=='lesson_decision_required'
        selected=next(l for l in current['lessons'] if l['lesson_id']==current['current_lesson_id'])
        assert 'U-0' in selected['missing_prerequisites']
        with pytest.raises(HTTPError):call('/api/projects/../unsafe')
        with pytest.raises(HTTPError):call(prefix+'/actions','POST',{**action,'actionId':'OTHER','expectedRevision':'stale'})
    finally:server.shutdown();server.server_close();thread.join(timeout=3)

def test_public_tutor_cli_complete_flow(workspace,capsys):
    nav=navigation(workspace,1);request=workspace/'request.json'
    assert runner.main(['init-request','--file',str(request),'--input',str(nav)])==0;capsys.readouterr()
    assert runner.main(['start','--root',str(workspace),'--request',str(request)])==3
    pending=json.loads(capsys.readouterr().out)
    assert runner.main(['confirm-project-name','--creation-dir',pending['creation_dir'],'--name',pending['suggested_name'],'--proposal-sha256',pending['proposal_sha256']])==0
    result=json.loads(capsys.readouterr().out);project=Path(result['project_dir'])
    # Project paths are within the actual repository; source paths use workspace_root.
    for cmd in ('status','resume','report','verify'):
        assert runner.main([cmd,'--project-dir',str(project)])==0;capsys.readouterr()
    assert runner.main(['prepare-lesson','--project-dir',str(project)])==0;capsys.readouterr()
    model=lp.load(project);run=Path(model['run_dir']);data=read_json(run/'lesson-decision.template.json')
    data['questions']=data['questions'][:1]
    data.update(overview='测试概述',key_points=['测试要点'])
    data['teaching_points'][0]['blocks'][0].update(parts=[{'type':'paragraph','text':'基于证据解释'}], coverage='explained');q=data['questions'][0];q.update(prompt='哪项正确？',options=['正确','错一','错二','错三'],reference_answer='正确',expected_points={'U-0':['正确']})
    file=run/'decision.json';write_json(file,data)
    assert runner.main(['publish-lesson','--project-dir',str(project),'--decision',str(file)])==3
    pending=json.loads(capsys.readouterr().out)
    quality=read_json(Path(pending['quality_template']))
    quality['short_complete_reason']='合成目标只需说明依据证据解释，正文已明确该动作和依据。'
    for item in quality['items']: item.update(judgment='sufficient', reason='已明确解释动作及证据依据。')
    quality_file=run/'teaching-review.json';write_json(quality_file,quality)
    assert runner.main(['publish-lesson','--project-dir',str(project),'--decision',str(file),'--quality-review',str(quality_file)])==0;capsys.readouterr()
    answers=run/'answers.json';write_json(answers,{'Q-1':'A'})
    assert runner.main(['submit-chat-answer','--project-dir',str(project),'--answers',str(answers)])==0;capsys.readouterr()
    review=read_json(run/'review.template.json');review.update(strengths='已掌握测试结论',weaknesses='当前题目未发现不足')
    for score in review['scores']:score['feedback']='已正确理解本题结论。'
    write_json(run/'review.json',review)
    assert runner.main(['review-answers','--project-dir',str(project),'--review',str(run/'review.json')])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='ready'
    assert runner.main(['deliver-feedback','--project-dir',str(project),'--lesson',result['lesson_id'],'--review-sha256',result['review_sha256'],'--feedback-sha256',result['feedback_sha256']])==0;capsys.readouterr()
    assert runner.main(['questions','--project-dir',str(project),'--question','为什么？','--answer','依照定义。'])==0;capsys.readouterr()
    assert runner.main(['questions','--project-dir',str(project),'--no-questions'])==2;capsys.readouterr()
    assert runner.main(['prepare-lesson','--project-dir',str(project)])==0
    assert json.loads(capsys.readouterr().out)['status']=='completed'
    assert runner.main(['verify','--project-dir',str(project)])==0

def test_remaining_cli_interfaces_and_answer_recheck(workspace,capsys):
    project=new_project(workspace,2)
    target=['--project-dir',str(project)]
    glossary=workspace/'terms.md'
    assert runner.main(['init-bilingual-glossary','--file',str(glossary)])==0;capsys.readouterr()
    model=lp.load(project);second=model['lessons'][1]['lesson_id']
    for cmd in ('skip','restore'):
        assert runner.main([cmd,*target,'--target-type','lesson','--target-id',second])==0;capsys.readouterr()
    assert runner.main(['supply-navigation',*target,'--navigation',model['navigation_json']])==0;capsys.readouterr()
    model=lp.load(project);patch=workspace/'plan.json'
    write_json(patch,{'base_revision':model['revision'],'operations':[{'operation':'update','lesson_id':second,'patch':{'title':'调整后的课程'}}]})
    assert runner.main(['plan',*target,'--patch',str(patch)])==0;capsys.readouterr()
    with lp.lock(project):
        model,receipt=lp.context(project);data=decision(project,model,receipt['config']);lp.publish(project,model,receipt['config'],data)
    lesson=project/'课程/课程_1-1.md';text=lesson.read_text(encoding='utf-8').replace('⟦请将这行替换为你的回答，可分段填写⟧','A');lesson.write_text(text,encoding='utf-8')
    assert runner.main(['check-answers',*target])==0;capsys.readouterr()
    model=lp.load(project);run=Path(model['run_dir']);review=read_json(run/'review.template.json');review.update(strengths='掌握测试结论',weaknesses='暂未发现不足');write_json(run/'review.json',review)
    lesson.write_text(text.replace('\n> A\n','\n> B\n'),encoding='utf-8')
    assert runner.main(['review-answers',*target,'--review',str(run/'review.json')])==2;capsys.readouterr()
    assert runner.main(['check-answers',*target])==0;capsys.readouterr()
    draft={'lesson_id':model['current_lesson_id'],'title':'待确认笔记','location':'讲解段落','background':'相关背景','text':'整理后的内容'}
    write_json(run/'note.json',draft)
    assert runner.main(['note-prepare',*target,'--draft',str(run/'note.json')])==0
    digest=json.loads(capsys.readouterr().out)['draft_hash']
    assert runner.main(['note-confirm',*target,'--draft-hash',digest,'--confirmed-by','合成用户'])==0;capsys.readouterr()
    assert '待确认笔记' in (project/'笔记本.md').read_text(encoding='utf-8')


def test_candidate_history_survives_navigation_update(workspace):
    project=new_project(workspace);model=lp.load(project)
    old=copy.deepcopy(model['candidate_orders'])
    nav=read_json(Path(model['navigation_json']));nav['title']='更新后的合成课程'
    lp.sync_navigation(model,nav)
    assert old in model['candidate_history']


def test_note_modified_after_preview_requires_new_confirmation(workspace):
    project=new_project(workspace,1);model,receipt=lp.context(project);cfg=receipt['config']
    lp.publish(project,model,cfg,decision(project,model,cfg))
    draft={'lesson_id':model['current_lesson_id'],'location':'核心解释','title':'笔记','background':'概念背景','text':'原始整理稿'}
    result=lp.note_prepare(model,project,draft)
    path=Path(model['run_dir'])/'note-draft.json';value=read_json(path);value['text']='未经确认的变化';write_json(path,value)
    with pytest.raises(ValueError,match='重新确认'):lp.note_confirm(model,result['draft_hash'],'合成用户')
    assert not model['notes']


def test_mc_grading_does_not_trust_editable_template(workspace):
    project=new_project(workspace,1);model,receipt=lp.context(project);cfg=receipt['config']
    data=decision(project,model,cfg);lp.publish(project,model,cfg,data)
    review=lp.collect_answers(project,model,{'Q-1':'B'})
    review.update(strengths='能作答',weaknesses='需要复习')
    review['scores'][0].update(unit_scores={'U-0':1},feedback='伪造评分')
    write_json(Path(model['run_dir'])/'review.template.json',review)
    with pytest.raises(ValueError,match='脚本判分'):lp.review(project,model,cfg,review)


def test_reduced_limits_produce_bounded_template_and_block_publish(workspace):
    project=new_project(workspace);model,receipt=lp.context(project);cfg=copy.deepcopy(receipt['config'])
    first=model['lessons'][0];first['teaches_unit_ids']=['U-0','U-1','U-2'];first['internal_unit_order']=first['teaches_unit_ids'][:]
    for other in model['lessons'][1:]:other['archived']=True
    cfg['lesson']['max_multiple_choice_questions']=1
    lp.prepare(project,model,cfg,workspace)
    template=read_json(Path(model['run_dir'])/'lesson-decision.template.json')
    assert [q['type'] for q in template['questions']]==['multiple_choice','open_ended']
    assert template['questions'][1]['unit_ids']==['U-1','U-2']
    cfg['lesson']['max_new_units']=1
    with pytest.raises(ValueError,match='知识点超过'):lp.publish(project,model,cfg,template)


def test_each_weak_point_requires_error_summary(workspace):
    project=new_project(workspace);model,receipt=lp.context(project);cfg=receipt['config']
    first=model['lessons'][0];first['teaches_unit_ids']=['U-0','U-1'];first['internal_unit_order']=first['teaches_unit_ids'][:]
    model['lessons'][1]['archived']=True
    data=decision(project,model,cfg,open_question=True)
    q=data['questions'][0];q['unit_ids']=['U-0','U-1'];q['expected_points']={uid:['说明定义'] for uid in q['unit_ids']};data['questions']=[q]
    lp.publish(project,model,cfg,data);review=lp.collect_answers(project,model,{'Q-1':'不完整的解释'})
    review.update(strengths='能表达部分概念',weaknesses='定义尚不完整')
    review['scores'][0].update(unit_scores={'U-0':.5,'U-1':.5},feedback='补充条件',misconceptions=[{'unit_id':'U-0','title':'定义','weakness':'遗漏条件','rule':'先检查定义','correction':'补全条件'}])
    with pytest.raises(ValueError,match='每个回答不完整'):lp.review(project,model,cfg,review)


def test_current_course_can_be_explicitly_skipped(workspace):
    project=new_project(workspace);model,receipt=lp.context(project);cfg=receipt['config']
    lp.publish(project,model,cfg,decision(project,model,cfg));current=model['current_lesson_id']
    lp.questions_event(model,question='课内疑问',answer='说明当前概念')
    assert model['state']=='awaiting_answer'
    with pytest.raises(ValueError,match='答疑阶段'):lp.questions_event(model,no_questions=True)
    write_json(lp.art(project,'调整指令'),{'actions':[{'action_id':'skip-current','target_type':'lesson','target_id':current,'operation':'skip'}]})
    lp.apply_actions(project,model);lp.save(project,model,cfg)
    assert model['state']=='ready' and model['current_lesson_id'] is None
    assert model['unit_progress']['U-0']['status']=='pending'
    assert lp.prepare(project,model,cfg,workspace)['status']=='lesson_decision_required'
    assert next(l for l in model['lessons'] if l['lesson_id']==model['current_lesson_id'])['missing_prerequisites']==['U-0']


def test_configuration_notice_survives_failure_until_success(workspace):
    from utils.scripts.learning_config import acknowledge_config
    skill=SKILLS[1];run=workspace/'logs/config-notice'
    initial=load_config(skill,workspace);freeze_config(skill,run,workspace)
    changed=copy.deepcopy(initial['config']);changed['podcast']['enabled']=True
    save_config(skill,changed,initial['revision'],workspace)
    assert freeze_config(skill,run,workspace)['message']
    assert freeze_config(skill,run,workspace)['message']
    acknowledge_config(run)
    assert not freeze_config(skill,run,workspace)['message']


def test_adaptive_target_responds_to_observed_answer():
    units=[{'unit_id':'A','title':'共享基础','module':'一','sequence':1,'prerequisites':[]},
           {'unit_id':'B','title':'中间概念','module':'一','sequence':2,'prerequisites':['A']},
           {'unit_id':'C','title':'目标应用','module':'一','sequence':3,'prerequisites':['B']},
           {'unit_id':'D','title':'另一主题','module':'二','sequence':4,'prerequisites':[]}]
    base=aa.begin(units,'初学','目标',5,['C','D'])
    base['answers']=[{'unit_id':'B','observation':'positive'}]
    assert aa.target(base,units)['unit_id']=='C'
    base['answers'][0]['observation']='negative'
    assert aa.target(base,units)['unit_id']=='A'


def test_restore_and_republish_keeps_previous_publication(workspace):
    project=new_project(workspace,1);model,receipt=lp.context(project);cfg=receipt['config']
    original=decision(project,model,cfg);lp.publish(project,model,cfg,original)
    current=model['current_lesson_id'];path=lp.art(project,'调整指令')
    write_json(path,{'actions':[{'action_id':'pause','target_type':'lesson','target_id':current,'operation':'skip'}]})
    lp.apply_actions(project,model)
    write_json(path,{'actions':[{'action_id':'return','target_type':'lesson','target_id':current,'operation':'restore'}]})
    lp.apply_actions(project,model)
    revised=decision(project,model,cfg);revised['overview']='新的讲解版本';lp.publish(project,model,cfg,revised)
    lesson=next(l for l in model['lessons'] if l['lesson_id']==current)
    assert lesson['publication_history'][0]['content']==original
    assert lesson['content']==revised


def test_empty_project_verifies_and_rejects_unexpected_root_files(workspace):
    project=new_project(workspace,1);model=lp.load(project);cfg=load_config(SKILLS[1])['config']
    assert lp.verify(project,model,cfg)['status']=='verified'
    (project/'unexpected.txt').write_text('synthetic',encoding='utf-8')
    with pytest.raises(ValueError,match='布局'):lp.verify(project,model,cfg)
