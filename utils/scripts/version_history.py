"""Release version synchronization, changelog rendering and deterministic checks."""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    from .commit_history import first_parent_commits, git_text, version_at, VERSION_LINK, collect_packet, assert_candidate
    from .structured_io import read_json, json_digest, validate_json_schema
except ImportError:
    from commit_history import first_parent_commits, git_text, version_at, VERSION_LINK, collect_packet, assert_candidate
    from structured_io import read_json, json_digest, validate_json_schema

# Existing commits predate the per-commit rule. This exact first-parent prefix is
# grandfathered; neither later commits nor unrelated repositories are exempt.
LEGACY_THROUGH = '78b5edbcbdf27003218a5e4b9825a7e0ac3e5367'
HISTORY_PATH = 'docs/更新历史.md'
TARGETS = ('VERSION', 'README.md', '.claude-plugin/marketplace.json', HISTORY_PATH)
NO_CORE = '本次无核心更新'


def validate_history_review(packet: dict, review: dict, schema: Path) -> None:
    """The generation and audit gates use exactly the same evidence contract."""
    validate_json_schema(review, schema)
    if review['packet_sha256'] != packet['packet_sha256']:
        raise ValueError('审查输入的差异指纹不匹配')
    if [r['version'] for r in review['commits']] != [r['version'] for r in packet['commits']]:
        raise ValueError('审查必须按模板覆盖所有版本，不得重复或跳过')
    for source, decision in zip(packet['commits'], review['commits']):
        available = {e['evidence_id'] for e in source['evidence']}
        covered = set()
        for update in decision['updates']:
            if not update['summary'].strip() or '\n' in update['summary'] or '\r' in update['summary']:
                raise ValueError('核心更新必须为单行非空短句')
            covered.update(update['evidence_ids'])
        skipped = [s['evidence_id'] for s in decision['skipped']]
        if len(set(skipped)) != len(skipped) or covered.intersection(skipped) or any(not s['reason'].strip() for s in decision['skipped']):
            raise ValueError('跳过证据不得重复、同时列为更新或缺少理由')
        covered.update(skipped)
        if covered != available:
            raise ValueError(f"{source['version']} 的证据未完整覆盖或引用了清单外证据")
        if not available and not decision['no_core_reason'].strip():
            raise ValueError('无可总结差异时须填写简短理由')


def review_binding(packet: dict, review: dict) -> dict:
    return {'schema_version': '1.0', 'mode': packet['mode'], 'head': packet['head'],
            'packet_sha256': packet['packet_sha256'], 'review_sha256': json_digest(review),
            'summaries': review['commits'],
            'commits': [{k: v for k, v in c.items() if k != 'evidence'} |
                        {'evidence': [{k: v for k, v in e.items() if k != 'patch'} for e in c['evidence']]}
                       for c in packet['commits']]}


def parse_changelog(text: str) -> list[dict]:
    lines = text.splitlines()
    if not lines or lines[0] != '# 更新历史':
        raise ValueError('更新历史须以 # 更新历史 开头')
    records, current = [], None
    for line in lines[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r'## (0\.1\.\d+)', line)
        if match:
            current = {'version': match[1], 'updates': []}
            records.append(current)
        elif current is not None and line.startswith('- ') and line[2:].strip():
            if re.match(r'^- (?:TODO\b|待办[：:]|当前工作区变更[：:]|执行日志[：:])', line, re.I):
                raise ValueError('更新列表不得保存待办、工作区进展或执行日志')
            current['updates'].append(line[2:].strip())
        else:
            raise ValueError('更新历史只允许版本标题及非空更新列表；不允许工作区章节或日期章节')
    if any(not r['updates'] for r in records):
        raise ValueError('每个版本须有列表；无核心更新时使用固定说明')
    if any(NO_CORE in r['updates'] and r['updates'] != [NO_CORE] for r in records):
        raise ValueError('无核心更新说明不能与核心更新混用')
    if len({r['version'] for r in records}) != len(records):
        raise ValueError('更新历史存在重复版本')
    return records


def render_changelog(records: list[dict]) -> str:
    lines = ['# 更新历史', '']
    for record in sorted(records, key=lambda r: int(r['version'].rsplit('.', 1)[1]), reverse=True):
        lines.extend([f"## {record['version']}", ''])
        lines.extend('- ' + item for item in (record['updates'] or [NO_CORE]))
        lines.append('')
    result = '\n'.join(lines)
    parse_changelog(result)
    return result


def version_values(read) -> dict:
    version = (read('VERSION') or '').strip()
    readme = read('README.md') or ''
    matches = list(VERSION_LINK.finditer(readme))
    try:
        marketplace = json.loads(read('.claude-plugin/marketplace.json') or '{}')
        plugins = [p for p in marketplace.get('plugins', []) if p.get('name') == 'htd-ai-augmented-education']
        market = plugins[0].get('version') if len(plugins) == 1 else None
    except (ValueError, TypeError, AttributeError):
        market = None
    return {'VERSION': version, 'README.md': re.search(r'0\.1\.\d+', matches[0][0])[0] if len(matches) == 1 else None,
            '.claude-plugin/marketplace.json': market}


def generated_targets(root: Path, version: str, changelog: str, read=None) -> dict[str, str]:
    if not re.fullmatch(r'0\.1\.(?:0|[1-9]\d*)', version):
        raise ValueError('项目版本格式须为 0.1.n')
    read = read or (lambda path: (root/path).read_text(encoding='utf-8'))
    readme = read('README.md')
    if readme is None:
        raise ValueError('README 缺失')
    if len(list(VERSION_LINK.finditer(readme))) != 1:
        raise ValueError('README 当前版本入口缺失或不唯一')
    readme = VERSION_LINK.sub(lambda m: m[1] + version + m[2], readme)
    market = json.loads(read('.claude-plugin/marketplace.json') or '{}')
    plugins = [p for p in market['plugins'] if p.get('name') == 'htd-ai-augmented-education']
    if len(plugins) != 1:
        raise ValueError('marketplace 项目插件缺失或不唯一')
    plugins[0]['version'] = version
    return {'VERSION': version + '\n', 'README.md': readme,
            '.claude-plugin/marketplace.json': json.dumps(market, ensure_ascii=False, indent=2) + '\n',
            HISTORY_PATH: changelog}


def prior_history(root: Path, commits: list[str]) -> list[dict]:
    """Freeze committed entries; accept the explicit initial migration only."""
    expected = [version_at(i) for i in reversed(range(len(commits)))]
    try:
        base = parse_changelog(git_text(root, commits[-1], HISTORY_PATH) or '')
        if [r['version'] for r in base] != expected:
            raise ValueError('已提交更新历史不连续')
        return base
    except ValueError:
        if commits[-1] != LEGACY_THROUGH:
            raise ValueError('先修复已提交历史的缺漏，再准备下一次提交')
        base = parse_changelog((root/HISTORY_PATH).read_text(encoding='utf-8'))
        if base and base[0]['version'] == version_at(len(commits)):
            base = base[1:]
        if [r['version'] for r in base] != expected:
            raise ValueError('先通过 history 模式补齐当前已提交历史')
        return base


def matching_review(root: Path, mode: str, head: str, records: list[dict], read) -> bool:
    """Use durable local review records as evidence, not merely version headings."""
    schema = Path(__file__).resolve().parents[2]/'skills/project-doc-audit/references/history-review.schema.json'
    paths = list((root/'logs/project-doc-audit/runs').glob('*/history/state.json'))
    paths += list((root/'logs/update-project-version/runs').glob('*/state.json'))
    for path in paths:
        try:
            state = read_json(path)
            if not isinstance(state, dict):
                continue
            if state.get('mode') != mode or state.get('status') != 'completed':
                continue
            journal = read_json(path.parent/'publication.json')
            if json_digest(journal) != state.get('publication_sha256'):
                continue
            binding = journal['binding']
            if binding['head'] != head or binding['mode'] != mode:
                continue
            review = read_json(path.parent/'review.json')
            if json_digest(review) != binding['review_sha256'] or review['commits'] != binding.get('summaries'):
                continue
            packet = read_json(path.parent/'packet.json')
            if packet['packet_sha256'] != state['packet_sha256']:
                continue
            if packet.get('scope') == 'staged_and_untracked':
                assert_candidate(root, packet, staged_only=True)
            elif collect_packet(root, mode) != packet:
                continue
            validate_history_review(packet, review, schema)
            if binding != review_binding(packet, review) or read_json(path.parent/'packet.json') != packet:
                continue
            if state.get('workflow') == 'update-project-version':
                from utils.scripts.version_workflow import verify_confirmation, digest_bytes, diff_text
                verify_confirmation(path.parent, state)
                output = root/'outputs/update-project-version/runs'/state['run_id']
                if digest_bytes((output/'proposal.md').read_bytes()) != state.get('proposal_sha256'):
                    continue
                if (output/'diff.md').read_text(encoding='utf-8') != diff_text(packet):
                    continue
            else:
                output = root/'outputs/project-doc-audit/runs'/state['run_id']/'history'
            if read_json(output/'bindings.json') != binding:
                continue
            if parse_changelog(journal['updates'][HISTORY_PATH]) != records:
                continue
            if mode == 'staged' and any((read(p) or '').replace('\r\n', '\n') != journal['updates'][p] for p in TARGETS):
                continue
            return True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            continue
    return False


def history_findings(root: Path, scope: str = 'worktree', on_stage=None) -> tuple[list[dict], dict]:
    stage = on_stage or (lambda _: None)
    result, facts = [], {'scope': scope}
    def add(kind, path, current, expected, suggestion, evidence=None):
        result.append(dict(kind=kind, path=path, current=current, expected=expected, suggestion=suggestion,
                           evidence=evidence or [path], confidence='deterministic', status='new', section=None))
    stage('checking_commit_history')
    try:
        commits = first_parent_commits(root)
        facts.update(head=commits[-1], commit_count=len(commits))
    except (ValueError, RuntimeError) as exc:
        commits = []
        add('commit_history_unavailable', '.', str(exc), '完整第一父链提交历史', '补齐仓库历史后重新检查')
    read = (lambda path: git_text(root, '', path)) if scope == 'staged' else (
        lambda path: (root/path).read_text(encoding='utf-8') if (root/path).is_file() else None)
    stage('checking_versions')
    expected = version_at(len(commits) if scope == 'staged' else len(commits)-1) if commits else None
    values = version_values(read)
    facts.update(expected_version=expected, versions=values)
    for path, actual in values.items():
        if actual is None or actual == '' or (expected and actual != expected):
            add('version_mismatch', path, actual, expected or values['VERSION'], '由版本历史生成入口同步版本，勿分别手工升版', ['VERSION', 'README.md', '.claude-plugin/marketplace.json'])
    # Check historical version blobs only beyond the agreed migration prefix.
    legacy_end = commits.index(LEGACY_THROUGH) if LEGACY_THROUGH in commits else -1
    for index, commit in enumerate(commits):
        if index <= legacy_end:
            continue
        historical = version_values(lambda path: git_text(root, commit, path))
        bad = {p: v for p, v in historical.items() if v != version_at(index)}
        if bad:
            add('commit_version_mismatch', 'VERSION', {'commit': commit, 'versions': bad}, version_at(index), '该已提交版本不符合递增规则；报告差异，不自动重写 Git 历史', [commit])
    facts['legacy_through'] = LEGACY_THROUGH if legacy_end >= 0 else None
    stage('checking_changelog')
    for path, required in {
        'SOURCE_OF_TRUTH.md': ('版本与提交更新历史', 'docs/更新历史.md', 'VERSION', '第一父链'),
        'AGENTS.md': ('版本与提交维护', 'SOURCE_OF_TRUTH.md', 'history-start', 'scope staged'),
        'CODE_OF_CONDUCT.md': ('SOURCE_OF_TRUTH.md', '更新历史', '未提交', '同次提交'),
    }.items():
        content = read(path) or ''
        absent = [rule for rule in required if rule not in content]
        if absent:
            add('changelog_scope_violation', path, absent, '已确认的版本、执行及维护职责声明', '按各文档分工补齐已确认的版本与更新历史约束')
    if re.search(r'^## 0\.1\.\d+', read('README.md') or '', re.M):
        add('changelog_scope_violation', 'README.md', 'README 包含逐版本明细章节', '当前版本与更新历史入口', '将逐次提交明细移入更新历史')
    try:
        text = read(HISTORY_PATH)
        if text is None:
            raise ValueError('更新历史缺失')
        records = parse_changelog(text)
        wanted = [version_at(i) for i in reversed(range(len(commits) + (scope == 'staged')))] if commits else None
        actual = [r['version'] for r in records]
        if wanted is not None and actual != wanted:
            add('changelog_mismatch', HISTORY_PATH, actual, wanted, '按第一父链补齐连续版本并倒序展示')
        if text.replace('\r\n', '\n') != render_changelog(records):
            add('changelog_mismatch', HISTORY_PATH, '格式与生成模板不一致', '版本标题及核心更新列表', '使用历史生成入口渲染')
        facts['changelog_versions'] = actual
        if scope == 'staged' and commits and not matching_review(root, 'staged', commits[-1], records, read):
            add('changelog_review_missing', HISTORY_PATH, '无匹配当前暂存区及生成文件的已完成审查',
                '当前差异指纹、短摘要与发布记录一致', '重新运行 staged 历史生成、审查及发布，将生成文件暂存后再检查')
        if scope == 'worktree' and commits:
            available = False
            for state_path in (root/'logs/project-doc-audit/runs').glob('*/history/state.json'):
                try:
                    state = read_json(state_path)
                    available = available or (state.get('mode') == 'history' and state.get('status') == 'completed'
                        and read_json(state_path.parent/'packet.json').get('head') == commits[-1])
                except (OSError, ValueError, AttributeError):
                    continue
            facts['local_history_review_available'] = available
            if available and not matching_review(root, 'history', commits[-1], records, read):
                add('changelog_review_missing', HISTORY_PATH, '历史条目与本地已完成审查不一致',
                    '对应提交的已审查核心更新', '按提交差异重新复核历史条目并由生成入口更新')
        if scope == 'staged' and commits:
            old_text = git_text(root, commits[-1], HISTORY_PATH)
            try:
                old_records = parse_changelog(old_text or '')
            except ValueError:
                old_records = None
            if old_records is not None and records[1:] != old_records:
                add('changelog_mismatch', HISTORY_PATH, '既有版本条目被改写', '只新增本次提交条目', '历史勘误须逐条复核提交证据，不将未提交内容回填旧版本')
        for index, commit in enumerate(commits):
            if index <= legacy_end:
                continue
            try:
                past = parse_changelog(git_text(root, commit, HISTORY_PATH) or '')
                if [r['version'] for r in past] != [version_at(i) for i in reversed(range(index + 1))]:
                    raise ValueError('提交中更新历史版本不连续或未包含本次条目')
            except ValueError as exc:
                add('changelog_mismatch', HISTORY_PATH, {'commit': commit, 'error': str(exc)}, version_at(index), '报告已提交历史的不一致，不自动修改 Git 历史', [commit])
    except ValueError as exc:
        add('changelog_scope_violation', HISTORY_PATH, str(exc), '已提交版本的核心更新列表', '移除工作区进展、重复版本及按工作时间拆分的条目')
    return result, facts
