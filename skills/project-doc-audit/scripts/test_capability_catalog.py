"""Regression coverage for catalog boundaries, drift and recoverable publication."""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts import capability_catalog as catalog, structured_io
from utils.scripts.catalog_audit import overview_findings, document_responsibility_findings, user_language_findings
from utils.scripts.skill_catalog import load_documented_category_occurrences


@pytest.fixture
def workspace(tmp_path):
    for relative in (catalog.DATA, catalog.GUIDE, 'README.md', 'AGENTS.md', 'SOURCE_OF_TRUTH.md',
                     'CODE_OF_CONDUCT.md', 'docs/architecture/README.md'):
        p = tmp_path/relative
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, p)
    for row in catalog.load_catalog(ROOT):
        p = tmp_path/row['document']
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/row['document'], p)
    return tmp_path


def test_catalog_counts_categories_and_application_scope(workspace):
    rows = catalog.load_catalog(workspace)
    assert sum(r['kind'] == 'skill' for r in rows) == 18
    assert sum(r['kind'] == 'application' for r in rows) == 4
    groups = load_documented_category_occurrences(workspace/catalog.GUIDE)
    assert len(groups) == 18 and all(len(g) == 1 for g in groups.values())
    assert groups['git-remote-diff'] == ['项目功能']
    assert '背书工具' not in groups
    assert overview_findings(workspace) == []


def test_check_does_not_write_or_create_logs(workspace):
    before = {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    assert catalog.generate(workspace, check=True) == {'status': 'verified'}
    assert {p: p.read_bytes() for p in workspace.rglob('*') if p.is_file()} == before
    assert not (workspace/'logs').exists()


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_subgroup', 'wrong_known_subgroup',
                                      'wrong_app_category', 'wrong_order', 'metadata_mismatch',
                                      'unknown_field', 'long_description'])
def test_invalid_directory_rejected(workspace, mutation):
    p = workspace/catalog.DATA
    rows = json.loads(p.read_text(encoding='utf-8'))
    if mutation == 'missing': rows.pop()
    elif mutation == 'duplicate': rows.append(rows[0])
    elif mutation == 'wrong_subgroup': rows[0]['subcategory'] = '英语'
    elif mutation == 'wrong_known_subgroup':
        next(r for r in rows if r['name'] == 'build-word-entry')['subcategory'] = '背诵与记忆'
    elif mutation == 'wrong_app_category': rows[-1]['category'] = '背诵与记忆'
    elif mutation == 'wrong_order': rows[0], rows[1] = rows[1], rows[0]
    elif mutation == 'metadata_mismatch':
        owner = workspace/rows[0]['document']
        owner.write_text(owner.read_text(encoding='utf-8').replace('category: project_function', 'category: common_tool'), encoding='utf-8')
    elif mutation == 'unknown_field': rows[0]['unknown'] = 'x'
    else: rows[0]['function'] = '一。二。三。四。'
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError): catalog.load_catalog(workspace)
    assert any(f['kind'] == 'catalog_data_invalid' for f in overview_findings(workspace))


def test_scope_headings_drift_and_retired_reference(workspace):
    p = workspace/catalog.GUIDE
    p.write_text(p.read_text(encoding='utf-8')+'\n## 正文覆盖与新课程协议\n\nscenario_examples:\n', encoding='utf-8')
    (workspace/'AGENTS.md').write_text('Skills'+'_说明书.md', encoding='utf-8')
    findings = overview_findings(workspace)
    assert {'catalog_structure_invalid', 'catalog_scope_violation', 'catalog_generated_mismatch', 'catalog_retired_reference'} <= {f['kind'] for f in findings}


def test_generation_rolls_back_recovers_and_is_idempotent(workspace, monkeypatch):
    readme = workspace/'README.md'
    guide = workspace/catalog.GUIDE
    readme.write_text(readme.read_text(encoding='utf-8').replace('比较本地仓库', '陈旧介绍'), encoding='utf-8')
    before = {p: p.read_bytes() for p in (readme, guide)}
    replace = structured_io.os.replace
    def fail(source, target):
        if Path(target) == readme:
            raise OSError('synthetic publication failure')
        return replace(source, target)
    monkeypatch.setattr(structured_io.os, 'replace', fail)
    with pytest.raises(OSError): catalog.generate(workspace)
    assert all(p.read_bytes() == data for p, data in before.items())
    states = list((workspace/'logs/project-doc-audit/runs').glob('*/catalog-generation/state.json'))
    assert len(states) == 1 and json.loads(states[0].read_text())['state'] == 'paused_error'
    monkeypatch.setattr(structured_io.os, 'replace', replace)
    catalog.generate(workspace, run_dir=states[0].parent)
    assert catalog.generate(workspace, check=True) == {'status': 'verified'}
    before = {p: p.read_bytes() for p in (readme, guide)}
    catalog.generate(workspace, run_dir=states[0].parent)
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(ValueError): catalog.generate(workspace, run_dir=workspace/'runtime/catalog-generation')


def test_podcast_generator_respects_overview_boundary():
    import importlib.util
    path = ROOT/'skills/create-dialogue-podcast/scripts/render_contracts.py'
    spec = importlib.util.spec_from_file_location('podcast_contracts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    updates = module.generated_docs()
    assert updates[ROOT/catalog.GUIDE] == (ROOT/catalog.GUIDE).read_text(encoding='utf-8')
    assert 'scenario_examples:' not in updates[ROOT/catalog.GUIDE]
    assert 'scenario_examples:' in updates[ROOT/'skills/create-dialogue-podcast/SKILL.md']


def test_subgroups_cannot_move_to_another_parent(workspace):
    path = workspace/catalog.GUIDE
    text = path.read_text(encoding='utf-8')
    start = text.index('#### 背诵与记忆')
    end = text.index('### AI 辅助教学')
    subgroups = text[start:end]
    text = text[:start] + text[end:]
    text = text.replace('### AI 辅助科研', subgroups + '### AI 辅助科研', 1)
    path.write_text(text, encoding='utf-8')
    assert any(f['kind'] == 'catalog_structure_invalid' for f in overview_findings(workspace))


def test_readme_duplicate_tables_and_protocols_are_rejected(workspace):
    path = workspace/'README.md'
    text = path.read_text(encoding='utf-8')
    text = text.replace('各 Skill 的适用时机', '| Skill | duplicate |\n\n```yaml\nscenario_examples: []\n```\n\n各 Skill 的适用时机')
    path.write_text(text, encoding='utf-8')
    assert any(f['kind'] == 'catalog_scope_violation' and f['path'] == 'README.md' for f in overview_findings(workspace))


def test_missing_readme_does_not_crash_audit(workspace):
    (workspace/'README.md').unlink()
    assert any(f['path'] == 'README.md' for f in overview_findings(workspace))


def test_heading_examples_do_not_change_outline():
    text = '# Real\n~~~text\n## Example\n~~~python\n## Still an example\n~~~\n## Public\n'
    assert catalog.markdown_headings(text) == [(1, 'Real'), (2, 'Public')]


def test_readme_count_is_generated(workspace):
    path = workspace/'README.md'
    path.write_text(path.read_text(encoding='utf-8').replace('**Skills：** 18', '**Skills：** 7'), encoding='utf-8')
    catalog.generate(workspace)
    assert '**Skills：** 18' in path.read_text(encoding='utf-8')


def test_common_generator_rejects_changed_inputs_during_resume(workspace, monkeypatch):
    target = workspace/'README.md'
    replace = structured_io.os.replace
    def fail(source, destination):
        if Path(destination) == target: raise OSError('synthetic interruption')
        return replace(source, destination)
    monkeypatch.setattr(structured_io.os, 'replace', fail)
    with pytest.raises(OSError): catalog.generate(workspace)
    state = next((workspace/'logs/project-doc-audit/runs').glob('*/catalog-generation/state.json'))
    data = workspace/catalog.DATA
    rows = json.loads(data.read_text(encoding='utf-8'))
    rows[0]['function'] = '已修改的功能介绍'
    data.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(structured_io.os, 'replace', replace)
    before = target.read_bytes()
    with pytest.raises(ValueError, match='输入已变化'): catalog.generate(workspace, run_dir=state.parent)
    assert target.read_bytes() == before


def test_podcast_contract_publication_rolls_back_and_recovers(workspace, monkeypatch):
    import importlib.util
    source = ROOT/'skills/create-dialogue-podcast/scripts/render_contracts.py'
    spec = importlib.util.spec_from_file_location('recoverable_podcast_contracts', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', workspace)
    for relative in ('utils/references/speech-handoff-v1.schema.json', '.claude-plugin/plugin.json'):
        target = workspace/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
    updates = module.build()
    before = {p: p.read_bytes() if p.exists() else None for p in updates}
    replace = structured_io.os.replace
    def fail(source, target):
        if Path(target) == workspace/'README.md': raise OSError('synthetic contract publication failure')
        return replace(source, target)
    monkeypatch.setattr(structured_io.os, 'replace', fail)
    def publish(**kwargs):
        return catalog.publish_documents(lambda root: module.build(), workspace,
                                         workflow='create-dialogue-podcast', run_name='contract-generation', **kwargs)
    with pytest.raises(OSError): publish()
    assert all((p.read_bytes() if p.exists() else None) == data for p, data in before.items())
    state = next((workspace/'logs/create-dialogue-podcast/runs').glob('*/contract-generation/state.json'))
    monkeypatch.setattr(structured_io.os, 'replace', replace)
    publish(run_dir=state.parent)
    assert publish(check=True) == {'status': 'verified'}


def test_invalid_schema_never_publishes(workspace):
    target = workspace/'utils/references/synthetic.schema.json'
    def builder(root): return {target: '{"type": 123}'}
    with pytest.raises(ValueError, match='Schema 无效'):
        catalog.publish_documents(builder, workspace)
    assert not target.exists()
    state = next((workspace/'logs/project-doc-audit/runs').glob('*/catalog-generation/state.json'))
    assert json.loads(state.read_text())['state'] == 'paused_error'


def test_marker_free_generation_preserves_surrounding_sections(workspace):
    path = workspace/'README.md'
    original = path.read_text(encoding='utf-8')
    prefix = original.split('## 功能表', 1)[0]
    suffix = original.split('各 Skill 的适用时机', 1)[1]
    assert '<!-- capability-tables:' not in original
    catalog.generate(workspace)
    catalog.generate(workspace)
    assert path.read_text(encoding='utf-8') == original
    assert original.startswith(prefix) and original.endswith(suffix)


def test_legacy_markers_are_migrated_and_reported(workspace):
    path = workspace/'README.md'
    text = path.read_text(encoding='utf-8')
    text = text.replace('### Skills', '<!-- capability-tables:start -->\n\n### Skills', 1)
    text = text.replace('各 Skill 的适用时机', '<!-- capability-tables:end -->\n\n各 Skill 的适用时机', 1)
    path.write_text(text, encoding='utf-8')
    assert any(f['kind'] == 'catalog_scope_violation' for f in overview_findings(workspace))
    catalog.generate(workspace)
    assert '<!-- capability-tables:' not in path.read_text(encoding='utf-8')
    assert catalog.generate(workspace, check=True) == {'status': 'verified'}


@pytest.mark.parametrize('mutation', ['missing_heading', 'duplicate_heading', 'missing_table', 'duplicate_table'])
def test_ambiguous_readme_never_publishes(workspace, mutation):
    path = workspace/'README.md'
    text = path.read_text(encoding='utf-8')
    if mutation == 'missing_heading': text = text.replace('## 功能表', '## 功能列表', 1)
    elif mutation == 'duplicate_heading': text += '\n## 功能表\n'
    elif mutation == 'missing_table':
        start, stop = catalog.readme_table_region(text)
        text = text[:start] + '### Skills\n\n### 应用\n' + text[stop:]
    else: text = text.replace('### 应用', '| 分类 | Skill | 当前功能 |\n| --- | --- | --- |\n\n### 应用', 1)
    path.write_text(text, encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError): catalog.generate(workspace)
    assert path.read_bytes() == before
    assert any(f['kind'] == 'catalog_scope_violation' for f in overview_findings(workspace))


def test_fenced_titles_do_not_change_readme_region(workspace):
    path = workspace/'README.md'
    original = path.read_text(encoding='utf-8')
    example = '```text\n## 功能表\n### Skills\n```\n\n'
    path.write_text(example + original, encoding='utf-8')
    catalog.generate(workspace)
    assert path.read_text(encoding='utf-8') == example + original


def test_document_responsibilities_and_references(workspace):
    assert document_responsibility_findings(workspace) == []
    path = workspace/'AGENTS.md'
    text = path.read_text(encoding='utf-8').replace('(docs/architecture/README.md)', '(docs/missing.md)')
    text += '\n- `CODE_OF_CONDUCT.md` 规定贡献者和 Agent 的行为边界。\n'
    path.write_text(text, encoding='utf-8')
    architecture = workspace/'docs/architecture/README.md'
    architecture.write_text('# 架构文档\n\n## 时间戳约定\n\nprepared → paused_error\n', encoding='utf-8')
    findings = document_responsibility_findings(workspace)
    assert {f['path'] for f in findings} == {'AGENTS.md', 'docs/architecture/README.md'}
    assert len(findings) == 4


def test_copied_authority_rule_is_reported(workspace):
    source = workspace/'SOURCE_OF_TRUTH.md'
    rule = next(line for line in source.read_text(encoding='utf-8').splitlines() if line.startswith('- ') and len(line) >= 50)
    path = workspace/'CODE_OF_CONDUCT.md'
    path.write_text(path.read_text(encoding='utf-8') + '\n' + rule + '\n', encoding='utf-8')
    assert any(f['path'] == 'CODE_OF_CONDUCT.md' for f in document_responsibility_findings(workspace))


@pytest.mark.parametrize('section', ['## 状态机\n\nprepared → completed', '## 配置\n\n```json\n{}\n```'])
def test_readme_scope_is_checked_outside_capability_tables(workspace, section):
    path = workspace/'README.md'
    path.write_text(path.read_text(encoding='utf-8')+'\n'+section+'\n', encoding='utf-8')
    assert any(f['path'] == 'README.md' and f['confidence'] == 'deterministic'
               and f['kind'] == 'catalog_scope_violation' for f in overview_findings(workspace))


def test_user_language_cues_are_bounded_and_not_verdicts(workspace):
    path = workspace/'README.md'
    text = path.read_text(encoding='utf-8')
    text += '\n## 使用提醒\n\n' + '\n'.join(['通过任务路由和门禁选择流程。'] * 6)
    path.write_text(text, encoding='utf-8')
    cues = [f for f in user_language_findings(workspace) if f['path'] == 'README.md']
    assert len(cues) == 1 and cues[0]['confidence'] == 'needs_review'
    assert len(cues[0]['current']) == 3
    assert '不得仅凭关键词判定违规' in cues[0]['suggestion']


def test_language_review_preserves_commands_and_developer_explanations(workspace):
    path = workspace/'README.md'
    text = path.read_text(encoding='utf-8')
    text += '\n## 使用提醒\n\n`Schema`\n\n```text\n任务路由 门禁\n```\n\n状态机是指按规定顺序执行的流程。\n\n## 开发说明\n\nSchema 与状态机。\n'
    path.write_text(text, encoding='utf-8')
    assert not any(f['path'] == 'README.md' for f in user_language_findings(workspace))


def test_context_generator_does_not_restore_application_protocols():
    from utils.scripts.render_learning_teaching_context_contract import build_updates
    updates = build_updates(ROOT)
    app = ROOT/'applications/交互式学习/README.md'
    text = updates[app]
    assert text == app.read_text(encoding='utf-8')
    start, stop = catalog.markdown_section(text, 2, '讲解中的引用与步骤')
    summary = text[start:stop]
    assert 'Schema' not in summary and '状态机' not in summary and '| 字段 |' not in summary
    assert '../../skills/interactive-tutor/SKILL.md' in summary
    assert '<!-- teaching-context-v7:' not in text
    assert '## 材料上下文与步骤复核（v7）' in updates[ROOT/'skills/interactive-tutor/SKILL.md']
