"""Offline interface and recovery regressions, using only synthetic teaching text."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from utils.scripts import dialogue_pipeline as dp
from utils.scripts.podcast_content import inspect_content, selected_content, tutor_provenance
from utils.scripts.structured_io import read_json, write_json


def call(root, kind, command, *args):
    cli = dp.ROOT / 'skills' / dp.SKILLS[kind] / 'scripts/cli.py'
    mode = [] if kind == 'podcast' else ['--mode', 'qa-dialogue' if kind == 'convert' else 'dialogue']
    result = subprocess.run([sys.executable, str(cli), command, '--root', str(root), *mode, *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    return result.returncode, json.loads(result.stdout)


def pairs(*refs):
    return {'schema_version': '1.0', 'pairs': [
        {'source_block_ids': [ref], 'question': '水受热后会怎样？', 'answer': '水受热后可以蒸发。'} for ref in refs]}


def ready_conversion(root):
    f = dp.Flow(root, 'convert', dp.allocate(root, dp.SKILLS['convert'])).create({'text': '水受热后可以蒸发。', 'input_file': None})
    dp.advance(f)
    dp.advance(f, pairs('b001'))
    assert f.s['status'] == 'completed'
    return f


def test_tutor_lists_do_not_swallow_body_or_tail(tmp_path):
    text = '# 课程 1｜蒸发\n\n## 前置知识\n\n无前置知识要求。\n\n## 本课学习的知识点\n\n蒸发\n\n水受热后可以蒸发。\n\n## 正式习题\n\n### 习题 1\n解释蒸发。\n\n## 双语术语\n\n蒸发 evaporation\n'
    project = tmp_path / 'project'
    lesson = project / '课程/lesson.md'
    lesson.parent.mkdir(parents=True)
    lesson.write_text(text, encoding='utf-8')
    write_json(project / 'artifacts/lessons/lesson.json', {'lesson_id': 'l1', 'filename': 'lesson.md', 'markdown_template': text})
    write_json(project / 'artifacts/学习路线.json', {'lessons': [{'lesson_id': 'l1', 'filename': 'lesson.md'}]})
    assert tutor_provenance(lesson)['producer_skill'] == 'interactive-tutor'
    found = inspect_content(text, tutor=True)
    assert sum(c['automatic_skip'] for c in found) == 3
    selected = selected_content(text, found, [{'candidate_id': c['candidate_id'], 'skip': False} for c in found if not c['automatic_skip']])
    assert '水受热后可以蒸发。' in selected
    assert 'evaporation' in selected
    assert '前置知识' not in selected and '正式习题' not in selected
    lesson.write_text(text.replace('可以蒸发', '可能蒸发'), encoding='utf-8')
    assert tutor_provenance(lesson) is None


def test_heading_lookalike_and_fenced_heading_do_not_auto_skip():
    text = '# 文章\n\n## 前置知识\n\n列表\n\n## 正文\n\n```\n## 正式习题\n```\n正文。\n'
    candidates = inspect_content(text)
    assert len(candidates) == 1 and not candidates[0]['automatic_skip']
    with pytest.raises(ValueError):
        selected_content(text, candidates, [])
    assert '列表' in selected_content(text, candidates, [{'candidate_id': 'c001', 'skip': False}])


def test_tutor_audio_reference_and_feedback_are_not_read():
    text = '# 课程\n\n[🎧 收听本课播客](../artifacts/podcast.mp3)\n\n## 课程讲解\n\n水受热后可以蒸发。\n\n## 本课学习反馈\n\n需要复习。\n'
    candidates = inspect_content(text, tutor=True)
    assert all(c['automatic_skip'] for c in candidates)
    selected = selected_content(text, candidates, [])
    assert '可以蒸发' in selected and '.mp3' not in selected and '需要复习' not in selected


@pytest.mark.parametrize('backend', ['edge-tts', 'volcengine'])
def test_all_cli_interfaces_and_both_backend_paths(tmp_path, backend):
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    cfg['tts'].update(backend=backend, question_to_answer_gap_ms=125, answer_to_question_gap_ms=275)
    config = tmp_path / 'config.yaml'
    import yaml
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    code, receipt = call(tmp_path, 'podcast', 'start', '--text', '水受热后可以蒸发。', '--config', config, '--mock')
    assert code == 3 and receipt['status'] == 'awaiting_content_inspection'
    rid = receipt['run_id']
    response = tmp_path / 'response.json'
    write_json(response, {'extra_candidates': []})
    code, receipt = call(tmp_path, 'podcast', 'resume', '--run-id', rid, '--input', response)
    assert code == 3 and receipt['status'] == 'invoking_transcript'
    write_json(response, {'schema_version': '1.0', 'pairs': [*pairs('b001')['pairs'], *pairs('b001')['pairs']]})
    code, receipt = call(tmp_path, 'podcast', 'resume', '--run-id', rid, '--input', response)
    assert code == 0, receipt
    for command in ('status', 'verify', 'deliver', 'resume'):
        assert call(tmp_path, 'podcast', command, '--run-id', rid)[0] == 0
    parent = dp.Flow(tmp_path, 'podcast', rid).load()
    convert_id, speech_id = parent.s['children']['transcript'], parent.s['children']['speech']
    for kind, child_id in (('convert', convert_id), ('tts', speech_id)):
        for command in ('status', 'verify', 'deliver', 'resume'):
            assert call(tmp_path, kind, command, '--run-id', child_id)[0] == 0
    speech = dp.Flow(tmp_path, 'tts', speech_id).load()
    timeline = read_json(speech.log / 'timeline.json')
    assert [t['gap_after_ms'] for t in timeline['turns']] == [125, 275, 125, 0]
    assert [p['duration_ms'] for p in timeline['pauses'] if p.get('kind') == 'role_switch'] == [125, 275, 125]
    assert [t['role'] for t in timeline['turns']] == ['questioner', 'answerer', 'questioner', 'answerer']
    for turn, audio in zip(speech.s['turns'], speech.s['audio_turns']):
        child = dp.legacy('tts').state_store(tmp_path, audio['run_id']).load()
        assert child['speaker_id'] == cfg['tts']['voices'][backend][turn['role']]
        assert not child['config_snapshot']['preview']['enabled']
        assert child['config_snapshot']['backend'] == backend
        receipt_path = tmp_path / turn['handoff']['approval_receipt_path']
        assert read_json(receipt_path)['approval_kind'] == 'policy_skip_review'
    assert {p.name for p in parent.out.iterdir()} == {'source.txt', 'transcript.txt', 'podcast.mp3'}
    before = copy.deepcopy(parent.s)
    (parent.out / 'transcript.txt').write_text('篡改', encoding='utf-8')
    assert call(tmp_path, 'podcast', 'verify', '--run-id', rid)[0] == 4
    assert dp.Flow(tmp_path, 'podcast', rid).load().s == before


def test_incremental_generation_rejects_unknown_and_reordered_sources(tmp_path):
    f = dp.Flow(tmp_path, 'convert', dp.allocate(tmp_path, dp.SKILLS['convert'])).create({'text': '水' * 3400, 'input_file': None})
    dp.advance(f)
    with pytest.raises(ValueError):
        dp.advance(f, pairs('b002'))
    assert f.s['pairs'] == []
    dp.advance(f, pairs('b001'))
    assert f.s['status'] == 'awaiting_agent_generation'
    packet = read_json(f.log / 'generation-packet.json')
    assert [b['block_id'] for b in packet['blocks']] == ['b002', 'b003']
    dp.advance(f, pairs('b002', 'b003'))
    dp.verify(f)


def test_semantic_decisions_remain_small_and_skip_review(tmp_path):
    f = dp.Flow(tmp_path, 'convert', dp.allocate(tmp_path, dp.SKILLS['convert'])).create({'text': '变量 x。', 'input_file': None})
    dp.advance(f)
    response = {'schema_version': '1.0', 'pairs': [{'source_block_ids': ['b001'], 'question': '变量是什么？', 'answer': '变量是 $x$。'}]}
    dp.advance(f, response)
    assert f.s['status'] == 'normalizing_turns'
    pending = f.s['pending_decisions'][0]
    decisions = read_json(Path(pending['decisions_path']))
    scan = read_json(Path(pending['scan_path']))
    for decision, candidate in zip(decisions['decisions'], scan['semantic_candidates']):
        decision['replacement'] = candidate['allowed_replacements'][0] if candidate['allowed_replacements'] else '艾克斯'
    dp.advance(f, {'turn_id': pending['turn_id'], 'decisions': decisions})
    assert f.s['status'] == 'completed'
    dp.verify(f)


def test_resume_does_not_resynthesize_verified_turns(tmp_path, monkeypatch):
    upstream = ready_conversion(tmp_path)
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    f = dp.Flow(tmp_path, 'tts', dp.allocate(tmp_path, dp.SKILLS['tts'])).create({'dialogue_run_id': upstream.run_id, 'config': cfg, 'mock': True})
    merge = dp.merge_dialogue_audio
    monkeypatch.setattr(dp, 'merge_dialogue_audio', lambda _: (_ for _ in ()).throw(RuntimeError('synthetic merge failure')))
    with pytest.raises(RuntimeError):
        dp.advance(f)
    assert f.s['status'] == 'merging_audio'
    hashes = [(x['run_id'], x['sha256']) for x in f.s['audio_turns']]
    monkeypatch.setattr(dp, 'merge_dialogue_audio', merge)
    dp.advance(f)
    assert f.s['status'] == 'completed'
    assert hashes == [(x['run_id'], x['sha256']) for x in f.s['audio_turns']]


def test_optional_sections_wait_for_user_and_original_is_unchanged(tmp_path):
    original = tmp_path / 'article.md'
    text = '# 内容\n\n水受热后可以蒸发。\n\n## 习题\n\n解释蒸发。\n'
    original.write_text(text, encoding='utf-8')
    f = dp.Flow(tmp_path, 'podcast', dp.allocate(tmp_path, dp.SKILLS['podcast'])).create({
        'input_file': str(original), 'text': None,
        'config': dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml'), 'mock': True})
    dp.advance(f)
    dp.advance(f, {'extra_candidates': []})
    assert f.s['status'] == 'awaiting_content_decision'
    dp.advance(f, {'schema_version': '1.0', 'decisions': [{'candidate_id': 'c001', 'skip': True}]})
    assert '习题' not in (f.log / 'selected-content.txt').read_text(encoding='utf-8')
    assert original.read_text(encoding='utf-8') == text


def test_bad_config_and_run_id_are_rejected_before_side_effects(tmp_path):
    config = tmp_path / 'bad.yaml'
    config.write_text('schema_version: 1\ntts: {}\n', encoding='utf-8')
    assert call(tmp_path, 'podcast', 'start', '--text', '正文。', '--config', config)[0] == 6
    assert not (tmp_path / 'outputs').exists()
    with pytest.raises(ValueError):
        dp.Flow(tmp_path, 'podcast', '../escape')


def test_same_second_child_reservations_are_unique(tmp_path, monkeypatch):
    from datetime import datetime
    from utils.scripts.timestamp import unique_filename_timestamp
    fixed = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(dp, 'unique_filename_timestamp', lambda used: unique_filename_timestamp(used, fixed))
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    request = {'text': '正文。', 'input_file': None, 'config': cfg, 'mock': True}
    a = dp.Flow(tmp_path, 'podcast', dp.allocate(tmp_path, dp.SKILLS['podcast'])).create(request)
    b = dp.Flow(tmp_path, 'podcast', dp.allocate(tmp_path, dp.SKILLS['podcast'])).create(request)
    first = a.child_id('transcript', 'convert')
    second = b.child_id('transcript', 'convert')
    assert second == first + '_1'
    assert a.child_id('transcript', 'convert') == first
    child = dp.child_flow(a, 'convert', 'transcript', {'text': '正文。', 'input_file': None})
    assert child.run_id == first


def test_standalone_convert_and_dialogue_tts_start_interfaces(tmp_path):
    code, receipt = call(tmp_path, 'convert', 'start', '--text', '水受热后可以蒸发。')
    assert code == 3 and receipt['status'] == 'awaiting_agent_generation'
    response = tmp_path / 'pairs.json'
    write_json(response, pairs('b001'))
    code, receipt = call(tmp_path, 'convert', 'resume', '--run-id', receipt['run_id'], '--input', response)
    assert code == 0, receipt
    conversion_id = receipt['run_id']
    code, receipt = call(tmp_path, 'tts', 'start', '--dialogue-run-id', conversion_id, '--mock')
    assert code == 0 and receipt['status'] == 'completed', receipt
    assert call(tmp_path, 'tts', 'verify', '--run-id', receipt['run_id'])[0] == 0


def test_single_voice_skip_preview_and_requested_id(tmp_path):
    conversion = ready_conversion(tmp_path)
    turn = conversion.s['turns'][0]
    module = dp.legacy('tts')
    rid = dp.allocate(tmp_path, dp.SKILLS['tts'])
    args = module.build_parser().parse_args(['start', '--root', str(tmp_path), '--transcript-run-id',
        turn['handoff']['producer_run_id'], '--skip-preview', '--requested-run-id', rid, '--mock'])
    cfg = module.load_config()
    cfg['white_noise']['enabled'] = False
    state, store = module.initialize_run(args, cfg)
    assert state['run_id'] == rid
    assert state['config_snapshot']['preview']['enabled'] is False
    with store.lock():
        state = module.advance_state_machine(state, store)
    assert state['status'] == 'completed'
    assert module.verify_output(Path(state['output_dir']))['status'] == 'passed'


def test_nested_exclusions_do_not_overlap():
    text = '# 内容\n\n正文。\n\n## 习题\n\n### 练习\n\n题目。\n\n## 总结\n\n总结内容。\n'
    candidates = inspect_content(text)
    assert len(candidates) == 1
    result = selected_content(text, candidates, [{'candidate_id': 'c001', 'skip': True}])
    assert '题目' not in result and '总结内容' in result


def test_only_surviving_title_is_not_body():
    text = '# 课程\n\n## 正式习题\n\n题目。\n'
    candidates = inspect_content(text, tutor=True)
    with pytest.raises(ValueError, match='没有正文'):
        selected_content(text, candidates, [])


def test_within_response_source_order_cannot_backtrack(tmp_path):
    f = dp.Flow(tmp_path, 'convert', dp.allocate(tmp_path, dp.SKILLS['convert'])).create({'text': '水' * 1800, 'input_file': None})
    dp.advance(f)
    response = pairs('b001', 'b002', 'b001')
    with pytest.raises(ValueError, match='回退'):
        dp.advance(f, response)
    assert f.s['pairs'] == []


def test_invalid_input_does_not_unpause_or_change_state(tmp_path):
    from utils.scripts.speech_cli import CommandStateError
    f = dp.Flow(tmp_path, 'podcast', dp.allocate(tmp_path, dp.SKILLS['podcast'])).create({
        'text': '正文。', 'input_file': None, 'config': dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')})
    f.move('staging_input')
    f.s = f.store.pause(f.s, status='paused_error', error_code='synthetic', message='synthetic', resume_stage='staging_input')
    before = copy.deepcopy(f.s)
    with pytest.raises(CommandStateError):
        dp.advance(f, {'extra_candidates': []})
    assert f.store.load() == before


def test_tts_parameters_resources_and_audio_format_are_frozen(tmp_path, monkeypatch):
    conversion = ready_conversion(tmp_path)
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    cfg['tts']['backend'] = 'volcengine'
    f = dp.Flow(tmp_path, 'tts', dp.allocate(tmp_path, dp.SKILLS['tts'])).create({
        'dialogue_run_id': conversion.run_id, 'config': cfg, 'mock': True})
    module = dp.legacy('tts')
    original = module.resolve_voice
    attempts = []
    def resolve(speaker, resource_id, settings):
        attempts.append(resource_id)
        name, speaker_id, actual_resource = original(speaker, resource_id, settings)
        # Simulate mapping changes after the two preflight role resolutions.
        return name, speaker_id, actual_resource if len(attempts) <= 2 or resource_id else 'synthetic-changed-resource'
    monkeypatch.setattr(module, 'resolve_voice', resolve)
    dp.advance(f)
    assert f.s['status'] == 'completed'
    assert attempts[2:] == ['seed-icl-2.0', 'seed-icl-2.0']
    assert f.s['tts_settings']['audio']['format'] == 'wav'
    assert all(Path(t['audio_path']).suffix == '.wav' for t in f.s['audio_turns'])
    f.s['tts_settings']['audio']['speech_rate'] += 1
    f.store.save(f.s)
    with pytest.raises(ValueError, match='配置快照'):
        dp.verify(f)


def test_delivery_source_and_timestamp_filenames(tmp_path):
    f = ready_conversion(tmp_path)
    assert f.receipt()['artifacts']['source'] == f.s['source']
    events = list((f.log / 'events').glob('*.jsonl'))
    assert [p.stem for p in events] == [f.run_id]


def test_non_mock_execution_uses_frozen_wav_without_network(tmp_path, monkeypatch):
    from utils.scripts import tts_backend
    upstream = ready_conversion(tmp_path)
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    cfg['tts']['backend'] = 'volcengine'
    module = dp.legacy('tts')
    metadata = lambda *args, **kwargs: {'backend_version': 'synthetic-service', 'timestamp_granularity': 'word'}
    monkeypatch.setattr(tts_backend, 'check_backend', metadata)
    monkeypatch.setattr(module, 'check_backend', metadata)
    monkeypatch.setattr(module, 'create_client', lambda state: module.MockClient())
    original = module.load_config
    def config():
        result = original()
        result['audio']['format'] = 'pcm'
        return result
    monkeypatch.setattr(module, 'load_config', config)
    f = dp.Flow(tmp_path, 'tts', dp.allocate(tmp_path, dp.SKILLS['tts'])).create({
        'dialogue_run_id': upstream.run_id, 'config': cfg, 'mock': False})
    dp.advance(f)
    dp.verify(f)
    assert f.s['status'] == 'completed'
    assert all(Path(t['audio_path']).suffix == '.wav' for t in f.s['audio_turns'])


def test_fractional_millisecond_turns_do_not_accumulate_rounding_error(tmp_path, monkeypatch):
    import wave
    from types import SimpleNamespace
    from utils.scripts.file_transaction import file_sha256
    audio_path = tmp_path / 'synthetic.wav'
    with wave.open(str(audio_path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x00\x00' * 12012)
    child_folder = tmp_path / 'synthetic-child'
    write_json(child_folder / 'full.timestamps.json', {'items': [], 'sentences': [], 'pauses': []})
    monkeypatch.setattr(dp.legacy('tts'), 'state_store', lambda *args: SimpleNamespace(load=lambda: {'output_dir': str(child_folder)}))
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    f = dp.Flow(tmp_path, 'tts', dp.allocate(tmp_path, dp.SKILLS['tts'])).create({'config': cfg, 'mock': True})
    f.checkpoint(audio_turns=[{'turn_id': f't{i+1:04d}', 'role': 'questioner' if i % 2 == 0 else 'answerer',
        'run_id': str(i), 'audio_path': str(audio_path), 'sha256': file_sha256(audio_path)} for i in range(4)])
    dp.merge_dialogue_audio(f)
    assert f.s['expected_duration_ms'] == 2002
    assert read_json(f.log / 'timeline.json')['turns'][-1]['end_ms'] == 2002


@pytest.mark.parametrize("backend", ["edge-tts", "volcengine"])
def test_dialogue_context_decision_resume_interface(tmp_path, backend):
    import yaml
    code, receipt = call(tmp_path, 'convert', 'start', '--text', '他欣然地接受邀请。')
    assert code == 3
    response = tmp_path / 'pairs.json'
    write_json(response, {'schema_version': '1.0', 'pairs': [
        {'source_block_ids': ['b001'], 'question': '他怎么做？', 'answer': '他欣然地接受邀请。'}]})
    code, receipt = call(tmp_path, 'convert', 'resume', '--run-id', receipt['run_id'], '--input', response)
    assert code == 0, receipt
    cfg = dp.config_snapshot(dp.ROOT / 'skills/create-dialogue-podcast/config.yaml')
    cfg['tts']['backend'] = backend
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    code, receipt = call(tmp_path, 'tts', 'start', '--dialogue-run-id', receipt['run_id'], '--config', config, '--mock')
    assert code == 3 and receipt['status'] == 'synthesizing_turns', receipt
    run_id = receipt['run_id']
    flow = dp.Flow(tmp_path, 'tts', run_id).load()
    pending = flow.s['pending_decisions'][0]
    assert pending['child_status'] == 'awaiting_context_decisions'
    template_path = Path(pending['child_pending'][0]['template_path'])
    decisions = read_json(template_path)
    decisions['decisions'][0]['action'] = 'replace'
    decision_path = tmp_path / 'context-decisions.json'
    before = {p: p.read_bytes() for p in flow.log.rglob('*') if p.is_file()}
    invalid = copy.deepcopy(decisions)
    invalid['decisions'][0]['action'] = 'bad'
    write_json(decision_path, invalid)
    code, invalid_result = call(tmp_path, 'tts', 'resume', '--run-id', run_id, '--input', decision_path)
    assert code == 2, invalid_result
    assert before == {p: p.read_bytes() for p in flow.log.rglob('*') if p.is_file()}
    write_json(decision_path, decisions)
    code, result = call(tmp_path, 'tts', 'resume', '--run-id', run_id, '--input', decision_path)
    assert code == 0 and result['status'] == 'completed', result
    for command in ('status', 'verify', 'deliver'):
        assert call(tmp_path, 'tts', command, '--run-id', run_id)[0] == 0
    timeline = read_json(dp.Flow(tmp_path, 'tts', run_id).load().log / 'timeline.json')
    assert '地' in ''.join(item['text'] for item in timeline['items'])



def test_podcast_nested_context_response_and_invalid_input_are_recoverable(tmp_path):
    code, receipt = call(tmp_path, 'podcast', 'start', '--text', '他欣然地接受邀请。', '--mock')
    assert code == 3
    run_id = receipt['run_id']
    response = tmp_path / 'podcast-response.json'
    write_json(response, {'extra_candidates': []})
    code, receipt = call(tmp_path, 'podcast', 'resume', '--run-id', run_id, '--input', response)
    assert code == 3 and receipt['status'] == 'invoking_transcript'
    write_json(response, {'schema_version': '1.0', 'pairs': [
        {'source_block_ids': ['b001'], 'question': '他怎么做？', 'answer': '他欣然地接受邀请。'}]})
    code, receipt = call(tmp_path, 'podcast', 'resume', '--run-id', run_id, '--input', response)
    assert code == 3 and receipt['status'] == 'invoking_tts', receipt
    parent = dp.Flow(tmp_path, 'podcast', run_id).load()
    tts_parent = dp.Flow(tmp_path, 'tts', parent.s['children']['speech']).load()
    pending = tts_parent.s['pending_decisions'][0]
    decisions = read_json(Path(pending['child_pending'][0]['template_path']))
    before = {p: p.read_bytes() for folder in (parent.log, tts_parent.log) for p in folder.rglob('*') if p.is_file()}
    write_json(response, {'schema_version': 1, 'decisions': []})
    code, result = call(tmp_path, 'podcast', 'resume', '--run-id', run_id, '--input', response)
    assert code == 2, result
    assert before == {p: p.read_bytes() for folder in (parent.log, tts_parent.log) for p in folder.rglob('*') if p.is_file()}
    decisions['decisions'][0]['action'] = 'replace'
    write_json(response, decisions)
    code, result = call(tmp_path, 'podcast', 'resume', '--run-id', run_id, '--input', response)
    assert code == 0 and result['status'] == 'completed', result
    for command in ('status', 'verify', 'deliver'):
        assert call(tmp_path, 'podcast', command, '--run-id', run_id)[0] == 0
