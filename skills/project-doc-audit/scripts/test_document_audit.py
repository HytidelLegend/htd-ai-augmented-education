"""Regression checks for real documentation references and state coverage."""
import json
import importlib.util
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'utils/scripts'))
from document_audit import document_reference_findings, inline_code
from application_contract import audit_applications, _extract_phase_states

spec = importlib.util.spec_from_file_location('audit_cli', Path(__file__).resolve().parents[1]/'scripts/cli.py')
audit_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_cli)


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    return target


def test_app_relative_config_and_static_references(tmp_path):
    write(tmp_path, 'applications/demo/config.yaml', 'version: 1')
    write(tmp_path, 'applications/demo/application-audit.json', json.dumps({
        'documents': {'readme': 'applications/demo/README.md', 'prd': 'docs/demo.md'}
    }))
    write(tmp_path, 'docs/guide.md', '# Guide')
    readme = write(tmp_path, 'applications/demo/README.md', '`config.yaml` `../../docs/guide.md`')
    prd = write(tmp_path, 'docs/demo.md', '`config.yaml` `applications/demo/config.yaml`')
    assert document_reference_findings(tmp_path, [readme, prd]) == []


def test_missing_static_references_are_reported_once(tmp_path):
    doc = write(tmp_path, 'docs/demo.md', '`docs/missing.md` `docs/missing.md` `./missing.py` `references/missing.md`')
    assert {f['current'] for f in document_reference_findings(tmp_path, [doc])} == {
        'docs/missing.md', './missing.py', 'references/missing.md'
    }


def test_globs_match_existing_files_and_report_empty_patterns(tmp_path):
    write(tmp_path, 'utils/references/dictionary-entry.schema.json', '{}')
    doc = write(tmp_path, 'README.md', '`utils/references/dictionary-*.schema.json` `utils/references/absent-*.json`')
    findings = document_reference_findings(tmp_path, [doc])
    assert [f['current'] for f in findings] == ['utils/references/absent-*.json']


def test_generated_historical_and_display_examples_are_not_paths(tmp_path):
    doc = write(tmp_path, 'README.md', '\n'.join([
        '`knowledge-boundary.json` `source.txt` `stages/` `dicts/a.jsonl` `result.md` `SKILL.md` `.md`',
        '`outputs/demo/entries/` `logs/demo/state.json` `tmp/example.md`',
        '旧 `applications/legacy/README.md` 经迁移后清理。',
        '`Z%（XX/YY）`',
        '```text', '`docs/example-only.md`', '```',
        '```json', '{"path": "source.txt"}', '```',
    ]))
    assert document_reference_findings(tmp_path, [doc]) == []


def test_inline_spans_do_not_cross_lines_or_fences():
    text = '`first`\n```text\n`ignored`\n```\n``second``\n`unfinished\nprose/with/slash`'
    assert [token for token, _ in inline_code(text)] == ['first', 'second']


def test_skill_section_resolves_relative_static_reference(tmp_path):
    write(tmp_path, 'skills/demo/SKILL.md', '# Demo')
    write(tmp_path, 'skills/demo/tools/README.md', '# Tools')
    doc = write(tmp_path, 'docs/Skills.md', '### demo\n`tools/README.md` `tools/missing.md`\n### Other\n`tools/README.md`')
    findings = document_reference_findings(tmp_path, [doc])
    assert {f['current'] for f in findings} == {'tools/missing.md', 'tools/README.md'}


def test_identical_lines_do_not_share_skill_section_context(tmp_path):
    write(tmp_path, 'skills/one/SKILL.md', '# One')
    write(tmp_path, 'skills/two/SKILL.md', '# Two')
    write(tmp_path, 'skills/two/tools/README.md', '# Tools')
    doc = write(tmp_path, 'docs/Skills.md', '### one\n`tools/README.md`\n### two\n`tools/README.md`')
    assert [f['current'] for f in document_reference_findings(tmp_path, [doc])] == ['tools/README.md']


def test_fenced_headings_do_not_change_skill_context(tmp_path):
    write(tmp_path, 'skills/one/SKILL.md', '# One')
    write(tmp_path, 'skills/two/SKILL.md', '# Two')
    write(tmp_path, 'skills/one/tools/README.md', '# Tools')
    doc = write(tmp_path, 'docs/Skills.md', '### one\n~~~text\n### two\n~~~\n`tools/README.md`')
    assert document_reference_findings(tmp_path, [doc]) == []


def test_history_word_does_not_hide_current_reference(tmp_path):
    doc = write(tmp_path, 'README.md', '历史说明：现在读取 `docs/missing.md`。\n旧 `docs/legacy.md` 迁移后删除；现在读取 `docs/current.md`。')
    assert {f['current'] for f in document_reference_findings(tmp_path, [doc])} == {'docs/missing.md', 'docs/current.md'}


def test_removed_static_root_still_reports_json_reference(tmp_path):
    doc = write(tmp_path, 'README.md', '`utils/references/missing.json`')
    assert [f['current'] for f in document_reference_findings(tmp_path, [doc])] == ['utils/references/missing.json']


def test_explicit_relative_path_does_not_fall_back_to_root(tmp_path):
    write(tmp_path, 'config.yaml', 'version: 1')
    doc = write(tmp_path, 'docs/demo.md', '`./config.yaml`')
    assert [f['current'] for f in document_reference_findings(tmp_path, [doc])] == ['./config.yaml']


def test_state_union_accepts_both_quote_styles_and_ignores_comments():
    source = '// type Phase = \'fake\';\nexport type Phase = "new_state" | /* "comment_state" */ \'error\';'
    assert _extract_phase_states(source) == ['new_state', 'error']


def test_fence_info_line_cannot_close_an_open_fence():
    text = '```text\n```json\n`ignored`\n```\n`visible`'
    assert [token for token, _ in inline_code(text)] == ['visible']


def test_report_verification_rejects_tampering(tmp_path):
    report = {'schema_version': '1.0', 'findings': [],
              'summary': {'checked_documents': 1, 'findings': 0, 'unchanged_documents': 1}}
    run = 'regression'
    log, out = audit_cli.dirs(tmp_path, run)
    write(tmp_path, str(log.relative_to(tmp_path)/'state.json'), '{"status":"completed"}')
    write(tmp_path, str(log.relative_to(tmp_path)/'findings.json'), json.dumps(report))
    write(tmp_path, str(out.relative_to(tmp_path)/'findings.json'), json.dumps(report))
    markdown = write(tmp_path, str(out.relative_to(tmp_path)/'report.md'), audit_cli.render(report))
    assert audit_cli.verify_report(tmp_path, run)
    markdown.write_text('tampered', encoding='utf-8')
    assert not audit_cli.verify_report(tmp_path, run)
    for command in ('verify', 'deliver'):
        result = subprocess.run([sys.executable, str(Path(audit_cli.__file__)), command,
                                 '--root', str(tmp_path), '--run-id', run], capture_output=True)
        assert result.returncode == 4
        if command == 'deliver':
            assert result.stdout == b''
    markdown.write_text(audit_cli.render(report), encoding='utf-8')
    report['summary']['findings'] = 1
    for directory in (log, out):
        (directory/'findings.json').write_text(json.dumps(report), encoding='utf-8')
    markdown.write_text(audit_cli.render(report), encoding='utf-8')
    assert not audit_cli.verify_report(tmp_path, run)


def test_render_counts_unchanged_documents_without_claiming_skips():
    rendered = audit_cli.render({'findings': [], 'summary': {'checked_documents': 2, 'unchanged_documents': 2, 'findings': 0}})
    assert '内容未变化文档：2 个（仍执行检查）' in rendered
    assert '跳过' not in rendered


def test_legacy_report_format_is_preserved():
    rendered = audit_cli.render({'findings': [], 'summary': {'checked_documents': 2, 'skipped_documents': 2, 'findings': 0}})
    assert '跳过未变化文档：2 个' in rendered


def test_missing_run_verification_returns_exit_code_four(tmp_path):
    result = subprocess.run([sys.executable, str(Path(audit_cli.__file__)), 'verify',
                             '--root', str(tmp_path), '--run-id', 'missing'], capture_output=True)
    assert result.returncode == 4
    assert json.loads(result.stdout) == {'ok': False}


def test_unchanged_doc_still_detects_removed_target(tmp_path):
    target = write(tmp_path, 'docs/guide.md', '# Guide')
    doc = write(tmp_path, 'README.md', '`docs/guide.md`')
    assert document_reference_findings(tmp_path, [doc]) == []
    target.unlink()
    assert [f['current'] for f in document_reference_findings(tmp_path, [doc])] == ['docs/guide.md']


def test_states_follow_implementation_and_chains(tmp_path):
    write(tmp_path, 'applications/demo/package.json', '{"version":"1"}')
    write(tmp_path, 'applications/demo/README.md', '# Demo')
    write(tmp_path, 'applications/demo/src/model.ts',
          "export type Phase = 'extracting_spans' | 'reviewing_extraction' | 'new_state' | 'error';")
    write(tmp_path, 'applications/demo/application-audit.json', json.dumps({
        'documents': {'readme': 'applications/demo/README.md', 'prd': 'docs/demo.md'},
        'implementation': {'state_machine': 'applications/demo/src/model.ts',
                           'data_model': 'applications/demo/src/model.ts'}
    }))
    doc = write(tmp_path, 'docs/demo.md', '状态：`extracting_spans → reviewing_extraction → new_state`；进入error后恢复。')
    assert audit_applications(tmp_path)[0] == []
    doc.write_text('`extracting_spans → reviewing_extraction → new_state_extra`；进入error后恢复。', encoding='utf-8')
    findings = audit_applications(tmp_path)[0]
    assert len(findings) == 1
    assert findings[0]['kind'] == 'application_state_machine_mismatch'
    assert findings[0]['current'] == ['new_state']
