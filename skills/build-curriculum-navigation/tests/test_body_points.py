"""Title-free materials must be divided by their complete body content."""
from pathlib import Path
import copy
import pytest
from test_initial_navigation import runner, filled_decisions
from navigation_test_support import finish_ordering
from utils.scripts.structured_io import read_json, write_json
import json


@pytest.mark.parametrize('has_heading', [True, False])
def test_v3_points_source_and_global_interfaces(tmp_path, has_heading):
    source = tmp_path/'source.md'
    source.write_text(('# 合成材料\n\n' if has_heading else '') + '正文中的第一个主题。\n第二个独立主题。\n需要辨析的补充主张。\n', encoding='utf-8')
    request = tmp_path/'request.json'
    runner.init_request(request)
    value = read_json(request); value.update(input_paths=[str(source)], navigation_title='正文要点测试')
    write_json(request, value)
    assert value['schema_version'] == '3.0'
    result = runner.prepare(tmp_path, request); run = Path(result['run_dir'])
    assert result['status'] == 'point_division_required'
    assert runner.main(['status', '--run-dir', str(run)]) == 0
    assert runner.main(['resume', '--run-dir', str(run)]) == 0
    inventory = read_json(run/'source-inventory.json'); sid = inventory[0]['source_id']
    directory = run/'source-work'/sid; packet = read_json(directory/'body-fragments.json')
    division = read_json(directory/'point-division.template.json')
    fs = packet['fragments']
    division['ignored_fragments'] = [{'fragment_id': fs[0]['fragment_id'], 'reason': '仅标题，正文内容另行划分。'}] if has_heading else []
    division['points'] = [{'summary': x['text'], 'fragment_ids': [x['fragment_id']],
                           'track': 'branch' if i == 2 else 'main', 'reason': '补充辨析' if i == 2 else '目标相关'} for i, x in enumerate(fs[1:] if has_heading else fs)]
    bad = copy.deepcopy(division); bad['points'].pop()
    f = run/'division.json'; write_json(f, bad)
    assert runner.main(['resolve-points', '--run-dir', str(run), '--source-id', sid, '--decisions', str(f)]) == 2
    assert read_json(run/'run-state.json')['status'] == 'paused_error'
    write_json(f, division)
    assert runner.main(['resume', '--run-dir', str(run)]) == 0
    template = read_json(run/'semantic-decisions.template.json')
    decisions = filled_decisions(template)
    # All body points remain real units; delayed execution is represented as a branch.
    source_decisions = read_json(directory/'source-decisions.template.json')
    source_decisions.update(covered_candidate_ids=[x['candidate_id'] for x in decisions['items']], items=decisions['items'])
    write_json(run/'source-decision.json', source_decisions)
    assert runner.main(['resolve-source', '--run-dir', str(run), '--source-id', sid, '--decisions', str(run/'source-decision.json')]) == 0
    write_json(run/'global.json', decisions)
    result = finish_ordering(runner, run, runner.resolve(run, run/'global.json'))
    published = runner.commit(run, confirmed_by='合成测试用户', preview_sha256=result['preview_sha256'])
    nav = read_json(Path(published['navigation_json']))
    assert len(nav['units']) == len(nav['material_points']) == 3
    assert sum(u['track'] == 'branch' for u in nav['units']) == 1
    assert all(u['source']['content_span'] for u in nav['units'])
    assert runner.verify(tmp_path, Path(published['navigation_json']))['unit_count'] == 3
    assert source.read_text(encoding='utf-8').startswith('# 合成材料' if has_heading else '正文中的第一个主题')
    from utils.scripts.learning_navigation_bundle import load_navigation_bundle
    navigation_path = Path(published['navigation_json'])
    archived = navigation_path.with_suffix('.sources') / sid / 'point-division.json'
    assert read_json(archived) == division
    nav['material_points'].pop()
    write_json(navigation_path, nav)
    from utils.scripts.learning_navigation import render_navigation_markdown
    navigation_path.with_suffix('.md').write_text(render_navigation_markdown(nav), encoding='utf-8')
    with pytest.raises(ValueError, match='原文要点|正文划分'):
        load_navigation_bundle(tmp_path, navigation_path)


def test_public_prepare_with_legacy_request_still_requires_body_points(tmp_path, capsys):
    source = tmp_path/'body.md'; source.write_text('没有标题的独立要点。', encoding='utf-8')
    request = tmp_path/'request.json'; runner.init_request(request)
    value = read_json(request); value.update(schema_version='2.0', input_paths=[str(source)], navigation_title='合成兼容请求')
    write_json(request, value)
    assert runner.main(['prepare', '--root', str(tmp_path), '--request', str(request)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'point_division_required'
    run = Path(result['run_dir'])
    assert read_json(run/'run-state.json')['body_point_protocol'] is True
    assert runner.main(['resume', '--run-dir', str(run)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'point_division_required'
