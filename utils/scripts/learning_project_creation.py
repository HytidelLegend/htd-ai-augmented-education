"""Name-confirmation gate shared by tutor entry points; no project writes before confirmation."""
from pathlib import Path

from .structured_io import read_json, write_json, json_digest, validate_json_schema
from .markdown_structure import sha256_file
from .timestamp import unique_filename_timestamp
from .workflow_checkpoint import WorkflowCheckpoint

ROOT = Path(__file__).resolve().parents[2]
TRANSITIONS = {
    'prepared': ('preparing_name_suggestion',),
    'preparing_name_suggestion': ('awaiting_name_suggestion', 'awaiting_name_confirmation'),
    'awaiting_name_suggestion': ('awaiting_name_confirmation',),
    'awaiting_name_confirmation': ('validating_confirmed_name',),
    'validating_confirmed_name': ('creating_project',),
    'creating_project': ('completed',), 'completed': (),
}


def receipt(directory, data, state):
    if state == 'completed':
        from .learning_project import load as load_project, startup_receipt
        result = read_json(directory / 'result.json')
        project = Path(result['project_dir'])
        model = load_project(project)
        return startup_receipt(project, model) if model['state'] != 'backup_required' else result
    return {'status': state, 'creation_dir': str(directory), 'suggested_name': data.get('suggested_name'),
            'proposal_sha256': json_digest(data), 'sources': data['sources'],
            'template': str(directory / 'name-suggestion.template.json'),
            'message': '请阅读全部材料并提交核心主题建议。' if state == 'awaiting_name_suggestion'
                       else '请确认项目名称，也可以修改建议名称。'}


def prepare(root, navigation, output=None, request=None):
    from .learning_project import navigation_preflight
    root = root.resolve(); nav = navigation_preflight(root, navigation)
    runs = root / 'logs/interactive-tutor/runs'
    stamp = unique_filename_timestamp(p.name for p in runs.iterdir()) if runs.exists() else unique_filename_timestamp([])
    directory = runs / stamp / 'creation'
    workflow = WorkflowCheckpoint(TRANSITIONS, directory)
    workflow.move('preparing_name_suggestion')
    sources = [{'source_id': s['source_id'], 'path': s['path'], 'sha256': s['sha256']} for s in nav['sources']]
    data = {'root': str(root), 'navigation': str(navigation.resolve()), 'navigation_sha256': sha256_file(navigation),
            'output': str(output) if output else None, 'request': request or {}, 'sources': sources,
            'suggested_name': Path(sources[0]['path']).stem if len(sources) == 1 else nav['title'] if not sources else None,
            'rationale': '单份材料名称' if len(sources) == 1 else '无材料，沿用导航主题'}
    write_json(directory / 'proposal.json', data)
    write_json(directory / 'name-suggestion.template.json', {'title': '', 'rationale': '',
               'reviewed_source_ids': [s['source_id'] for s in sources]})
    workflow.move('awaiting_name_suggestion' if len(sources) > 1 else 'awaiting_name_confirmation')
    return receipt(directory, data, workflow.state)


def load(directory):
    directory = directory.resolve()
    data = read_json(directory / 'proposal.json')
    workflow = WorkflowCheckpoint(TRANSITIONS, directory, resume=True, restart_completed=False)
    from .learning_project import safe_path
    root = Path(data['root'])
    safe_path(root / 'logs/interactive-tutor/runs', directory)
    if workflow.state == 'completed': return data, workflow
    if sha256_file(safe_path(root, data['navigation'])) != data['navigation_sha256']:
        raise ValueError('导航已变化，请重新准备名称建议')
    for source in data['sources']:
        if sha256_file(safe_path(root, source['path'])) != source['sha256']:
            raise ValueError('材料已变化，请更新导航并重新准备名称建议')
    return data, workflow


def suggest(directory, decision):
    data, workflow = load(directory)
    if workflow.state != 'awaiting_name_suggestion':
        raise ValueError('当前不接受名称建议')
    validate_json_schema(decision, ROOT / 'utils/references/learning-project-name-suggestion-v1.schema.json')
    if set(decision['reviewed_source_ids']) != {s['source_id'] for s in data['sources']}:
        raise ValueError('必须阅读全部学习材料')
    data.update(suggested_name=decision['title'].strip(), rationale=decision['rationale'].strip())
    write_json(directory / 'proposal.json', data)
    workflow.move('awaiting_name_confirmation')
    return receipt(directory, data, workflow.state)


def confirm(directory, name, digest):
    data, workflow = load(directory)
    if workflow.state == 'completed':
        confirmed = read_json(directory / 'confirmation.json')
        if confirmed != {'name': name.strip(), 'proposal_sha256': digest}: raise ValueError('已确认名称不同')
        return receipt(directory, data, workflow.state)
    if workflow.state not in ('awaiting_name_confirmation', 'validating_confirmed_name', 'creating_project'):
        raise ValueError('当前不接受名称确认')
    if digest != json_digest(data):
        raise ValueError('名称建议已变化，请重新确认')
    if not name.strip() or len(name.strip()) > 200 or any(ord(c) < 32 for c in name):
        raise ValueError('项目名称须为 1 至 200 字的单行文本')
    confirmation = directory / 'confirmation.json'
    if confirmation.is_file() and read_json(confirmation)['name'] != name.strip():
        raise ValueError('已确认名称不同，请重新准备项目')
    write_json(confirmation, {'name': name.strip(), 'proposal_sha256': digest})
    if workflow.state == 'awaiting_name_confirmation': workflow.move('validating_confirmed_name')
    if workflow.state == 'validating_confirmed_name': workflow.move('creating_project')
    from .learning_project import create, load as load_project, context, save, startup_receipt, BackupApprovalRequired
    project = Path(data['output']) if data['output'] else Path(data['root']) / 'outputs/interactive-tutor/runs' / directory.parent.name
    if project.exists():
        model = load_project(project)
        if model['run_dir'] != str(directory.parent) or model['title'] != name.strip(): raise ValueError('项目目录冲突')
        try:
            model, config = context(project); save(project, model, config['config'])
            result = startup_receipt(project, model)
        except BackupApprovalRequired as exc: result = exc.receipt
    else:
        result = create(Path(data['root']), Path(data['navigation']), project, data['request'],
                        confirmed_name=name.strip(), creation_run=directory.parent)
    write_json(directory / 'result.json', result)
    workflow.move('completed')
    return result
