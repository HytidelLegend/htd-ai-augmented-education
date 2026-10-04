"""Tutor podcast composition gate; publish the lesson only after audio verification."""
from pathlib import Path
from . import dialogue_pipeline as dp
from .artifact_location import register
from .structured_io import json_digest
from .workflow_checkpoint import WorkflowCheckpoint

TRANSITIONS = {
    'prepared': ('preparing_input',), 'preparing_input': ('invoking_podcast', 'paused_error'),
    'invoking_podcast': ('verifying_outputs', 'paused_error'),
    'verifying_outputs': ('publishing_audio', 'linking_lesson', 'paused_error'),
    'publishing_audio': ('linking_lesson', 'completed', 'paused_error'),
    'linking_lesson': ('publishing_audio', 'completed', 'paused_error'),
    'paused_error': ('invoking_podcast',), 'completed': (),
}


def recover_published(project, model, config):
    """Finish an interrupted checkpoint after durable audio/document publication."""
    from . import learning_project as lp
    recovered = []
    for lesson in model['lessons']:
        podcast = lesson.get('podcast', {})
        if podcast.get('status') != 'completed': continue
        directory = Path(model['run_dir']) / 'lesson-podcasts' / podcast['run_id']
        if not (directory / 'state.json').is_file(): continue
        flow = WorkflowCheckpoint(TRANSITIONS, directory, resume=True, restart_completed=False)
        if flow.state == 'completed': continue
        lp.verify(project, model, config)
        # Revalidation reuses the already verified child/audio, without republishing.
        if flow.state == 'paused_error': flow.move('invoking_podcast')
        if flow.state == 'prepared': flow.move('preparing_input')
        if flow.state == 'preparing_input': flow.move('invoking_podcast')
        if flow.state == 'invoking_podcast': flow.move('verifying_outputs')
        if flow.state == 'verifying_outputs': flow.move('linking_lesson')
        if flow.state == 'linking_lesson': flow.move('publishing_audio')
        flow.move('completed', recovered_after_publication=True)
        recovered.append(lesson['lesson_id'])
    return recovered


def advance(project, model, config, response=None, *, mock=False):
    from . import learning_project as lp
    if model['state'] != 'podcast_required': raise ValueError('当前没有待完成的课程播客')
    lesson = next(l for l in model['lessons'] if l['lesson_id'] == model['current_lesson_id'])
    root = Path(model['workspace_root'])
    if 'run_id' not in lesson['podcast']:
        settings = dp.config_snapshot(lp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
        version = json_digest({'body': lesson['base_markdown'], 'config': settings})
        run_id = dp.allocate(root, dp.SKILLS['podcast'])
        output = project / 'artifacts/podcasts' / lesson['lesson_id'] / version / run_id
        register(root, dp.SKILLS['podcast'], run_id, output)
        lesson['podcast'].update(run_id=run_id, version=version, config=settings, mock=mock,
                                 output_dir=output.relative_to(project).as_posix(), status='pending')
        lp.save(project, model, config)
    podcast = lesson['podcast']
    directory = Path(model['run_dir']) / 'lesson-podcasts' / podcast['run_id']
    workflow = WorkflowCheckpoint(TRANSITIONS, directory, resume=True, restart_completed=False)
    if workflow.state == 'prepared': workflow.move('preparing_input')
    f = dp.Flow(root, 'podcast', podcast['run_id'])
    try:
        if not f.store.path.exists():
            f.create({'input_file': None, 'text': lesson['base_markdown'], 'config': podcast['config'],
                      'mock': podcast['mock'], 'tutor_record': str(project / 'artifacts/lessons' / Path(lesson['filename']).with_suffix('.json'))})
        else: f.load()
        if workflow.state in ('preparing_input', 'paused_error'): workflow.move('invoking_podcast')
        if workflow.state == 'invoking_podcast':
            with f.store.lock():
                f.load(); dp.advance(f, response)
            if f.s['status'] != 'completed':
                return {'status': 'podcast_required', 'podcast': f.receipt(),
                        'message': '播客尚未完成，课程暂不交付。按 pending_decisions 填写小模板，再 resume --podcast-response。'}
            workflow.move('verifying_outputs')
        if workflow.state == 'verifying_outputs':
            dp.verify(f)
            workflow.move('linking_lesson')
        if workflow.state == 'linking_lesson':
            workflow.move('publishing_audio')
        if workflow.state == 'publishing_audio':
            target = '播客/' + Path(lesson['filename']).with_suffix('.mp3').name
            lesson['_audio_publication'] = {'source': (f.out / 'podcast.mp3').relative_to(project).as_posix(),
                                           'target': target, 'sha256': dp.file_sha256(f.out / 'podcast.mp3')}
            podcast.update(status='completed', audio_path=target,
                           audio_sha256=dp.file_sha256(f.out / 'podcast.mp3'))
            lp.transition(model, 'awaiting_answer', 'podcast_verified_and_lesson_published')
            lp.save(project, model, config)
            workflow.move('completed')
        return {**lp.startup_receipt(project, model), 'lesson': str(project / '课程' / lesson['filename']), 'podcast': f.receipt()}
    except Exception as exc:
        if workflow.state != 'paused_error': workflow.move('paused_error', error=str(exc))
        raise
