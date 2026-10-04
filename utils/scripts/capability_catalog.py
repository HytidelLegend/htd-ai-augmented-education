"""Render the public capability tables from one short, validated catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts.structured_io import write_text_transaction
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory
from utils.scripts.skill_catalog import parse_front_matter

GUIDE = 'docs/Skills、应用说明书.md'
DATA = 'utils/references/capability-catalog.json'
SKILL_GROUPS = ('项目功能', '通用工具', 'AI 辅助学习', 'AI 辅助教学', 'AI 辅助科研')
LEARNING_GROUPS = ('背诵与记忆', '交互式学习', '英语', '绘图')
APP_GROUPS = LEARNING_GROUPS[:3]
CATEGORIES = dict(zip(SKILL_GROUPS, ('project_function', 'common_tool', 'ai_assisted_learning', 'ai_assisted_teaching', 'ai_assisted_research')))
HEADINGS = ('统一退出码', '时间戳', 'Skills 分类与调用', '应用分类与使用')
SKILL_LAYOUT = (
    ('项目功能', '', ('htd-ai-augmented-education', 'project-doc-audit', 'update-project-version', 'sensitive-commit-check', 'git-remote-diff')),
    ('通用工具', '', ('convert-copy-to-transcript', 'create-dialogue-podcast', 'format-conversion-master', 'run-speech-to-text', 'run-text-to-speech')),
    ('AI 辅助学习', '背诵与记忆', ('build-mnemonic-keywords', 'mark-memory-spans', 'schedule-ebbinghaus-plan')),
    ('AI 辅助学习', '交互式学习', ('build-curriculum-navigation', 'interactive-tutor')),
    ('AI 辅助学习', '英语', ('build-word-entry', 'en-writing-master')),
    ('AI 辅助学习', '绘图', ('render-handwritten-essay-card',)),
)
APP_LAYOUT = (('背诵与记忆', ('背书工具', '背图工具')), ('交互式学习', ('交互式学习',)), ('英语', ('词汇星图',)))
FIELDS = {'kind', 'name', 'category', 'subcategory', 'function', 'when', 'entry', 'document'}
STAGES = ('prepared', 'loading', 'validating', 'publishing', 'verifying', 'completed')
TRANSITIONS = {a: (b, 'paused_error') for a, b in zip(STAGES, STAGES[1:])}
TRANSITIONS.update(completed=(), paused_error=('loading',))


def markdown_heading_spans(text):
    """Return real headings and character offsets, ignoring fenced examples."""
    result, fence, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if marker:
            token, tail = marker.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not tail.strip():
                fence = None
        elif fence is None:
            heading = re.match(r'^ {0,3}(#{1,6}) +(.+?)\s*#*\s*$', line.rstrip('\r\n'))
            if heading:
                result.append((len(heading[1]), heading[2], offset, offset + len(line)))
        offset += len(line)
    return result


def markdown_section(text, level, title):
    """Locate one public section; ambiguous or absent headings cannot be written."""
    headings = markdown_heading_spans(text)
    matches = [h for h in headings if h[:2] == (level, title)]
    if len(matches) != 1:
        raise ValueError(f'文档标题缺失或不唯一：{title}')
    heading = matches[0]
    stop = next((h[2] for h in headings if h[2] > heading[2] and h[0] <= level), len(text))
    return heading[3], stop


def readme_table_region(text):
    """Locate the two capability tables by headings and contiguous table rows."""
    start, stop = markdown_section(text, 2, '功能表')
    feature = text[start:stop]
    headings = markdown_heading_spans(feature)
    if [(h[0], h[1]) for h in headings] != [(3, 'Skills'), (3, '应用')]:
        raise ValueError('README 功能表必须依次包含唯一的 Skills 和应用标题')
    tables = list(re.finditer(r'(?m)^\|[^\n]*\n(?:\|[^\n]*(?:\n|$))+', feature))
    if len(tables) != 2 or not (headings[0][3] <= tables[0].start() < tables[0].end() <= headings[1][2]
                               and headings[1][3] <= tables[1].start()):
        raise ValueError('README 功能表必须依次包含两张表格')
    for table_match, label in zip(tables, ('Skill', '应用')):
        header = [c.strip() for c in table_match.group().splitlines()[0].strip('|').split('|')]
        if header != ['分类', label, '当前功能']:
            raise ValueError('README 功能表表头不符合已确认模板')
    between = feature[tables[0].end():headings[1][2]]
    if between.strip() or feature[headings[0][3]:tables[0].start()].strip() or feature[headings[1][3]:tables[1].start()].strip():
        raise ValueError('README 功能表标题与表格之间存在额外内容')
    return start + headings[0][2], start + tables[1].end()


def markdown_headings(text):
    """Return the actual heading tree, ignoring fenced examples."""
    return [(h[0], h[1]) for h in markdown_heading_spans(text)]


def expected_headings():
    result = [(1, 'Skills、应用说明书'), *((2, h) for h in HEADINGS[:3]), (3, '概览')]
    for group in SKILL_GROUPS:
        result.append((3, group))
        if group == 'AI 辅助学习':
            result.extend((4, h) for h in LEARNING_GROUPS)
    return result + [(2, HEADINGS[3]), (3, '概览'), *((3, h) for h in APP_GROUPS)]


def load_catalog(root: Path = ROOT) -> list[dict]:
    rows = json.loads((root / DATA).read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise ValueError('能力目录必须为列表')
    seen = set()
    required = {('skill', name): (group, sub) for group, sub, names in SKILL_LAYOUT for name in names}
    required.update({('application', name): (group, '') for group, names in APP_LAYOUT for name in names})
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError('能力目录字段不符合已确认模板')
        if any(not isinstance(value, str) or '\n' in value for value in row.values()):
            raise ValueError('能力目录字段必须为单行字符串')
        kind, name = row['kind'], row['name']
        if kind not in ('skill', 'application') or not name or (kind, name) in seen:
            raise ValueError('能力类型、名称或唯一性无效')
        seen.add((kind, name))
        if (kind, name) in required and (row['category'], row['subcategory']) != required[(kind, name)]:
            raise ValueError(f'能力分类与已确认规则不一致：{name}')
        groups = SKILL_GROUPS if kind == 'skill' else APP_GROUPS
        if row['category'] not in groups or (row['subcategory'] not in LEARNING_GROUPS if row['category'] == 'AI 辅助学习' else bool(row['subcategory'])):
            raise ValueError('能力分类无效')
        if not row['function'] or not row['entry'] or (kind == 'skill' and not row['when']):
            raise ValueError('功能与入口不能为空')
        sentences = [s for s in re.split(r'[。！？]+', row['function']) if s.strip()]
        if not 1 <= len(sentences) <= 3:
            raise ValueError('功能描述应为 1～3 句话')
        expected = f'skills/{name}/SKILL.md' if kind == 'skill' else f'applications/{name}/README.md'
        if row['document'] != expected or not (root / expected).is_file():
            raise ValueError(f'能力文档无效：{name}')
        if kind == 'skill':
            metadata = parse_front_matter((root / expected).read_text(encoding='utf-8'))
            if metadata.get('category') != CATEGORIES[row['category']]:
                raise ValueError(f'Skill 分类与公开契约不一致：{name}')
    skills = {p.parent.name for p in (root / 'skills').glob('*/SKILL.md') if '.backup' not in p.parts}
    apps = {p.parent.name for p in (root / 'applications').glob('*/README.md')}
    if {n for k, n in seen if k == 'skill'} != skills or {n for k, n in seen if k == 'application'} != apps:
        raise ValueError('能力目录与活动 Skills/应用不一致')
    for kind in ('skill', 'application'):
        ordered = [key for key in required if key[0] == kind and key in seen]
        actual = [(r['kind'], r['name']) for r in rows if r['kind'] == kind and (kind, r['name']) in required]
        if actual != ordered:
            raise ValueError('能力成员顺序与已确认规则不一致')
    return rows


def table(headers, rows):
    def cell(value):
        return str(value).replace('|', '&#124;').replace('\n', '<br>')
    return '\n'.join('| ' + ' | '.join(map(cell, row)) + ' |' for row in [headers, ['---'] * len(headers), *rows]) + '\n'


def anchor(value):
    return value.lower().replace(' ', '-')


def overview(rows, kind, groups):
    result = ['### 概览', '']
    for group in groups:
        names = '、'.join(f"`{r['name']}`" for r in rows if r['kind'] == kind and r['category'] == group) or '暂无'
        result.append(f'- [{group}](#{anchor(group)})：{names}。')
    return '\n'.join(result) + '\n'


def guide_sections(rows):
    result = ['## Skills 分类与调用', '', overview(rows, 'skill', SKILL_GROUPS)]
    for group in SKILL_GROUPS:
        result.extend([f'### {group}', ''])
        subsets = LEARNING_GROUPS if group == 'AI 辅助学习' else ('',)
        for subgroup in subsets:
            if subgroup:
                result.extend([f'#### {subgroup}', ''])
            selected = [r for r in rows if r['kind'] == 'skill' and r['category'] == group and r['subcategory'] == subgroup]
            result.append(table(['Skill', '功能', '调用时机', 'CLI 示例'], [
                [f"[`{r['name']}`](../{r['document']})", r['function'], r['when'], f"`{r['entry']}`"] for r in selected]) if selected else '暂无\n')
    result.extend(['## 应用分类与使用', '', overview(rows, 'application', APP_GROUPS)])
    for group in APP_GROUPS:
        selected = [r for r in rows if r['kind'] == 'application' and r['category'] == group]
        result.extend([f'### {group}', '', table(['应用', '功能', '启动方式', '文档'], [
            [r['name'], r['function'], f"`{r['entry']}`", f"[使用说明](../{r['document']})"] for r in selected])])
    # Repeated application headings use GitHub's numbered anchors.
    text = '\n'.join(result)
    before, after = text.split('## 应用分类与使用', 1)
    for group in APP_GROUPS:
        after = after.replace(f'](#{anchor(group)})', f'](#{anchor(group)}-1)')
    return before + '## 应用分类与使用' + after


def readme_tables(rows):
    result = ['### Skills', '']
    skill_rows = [[r['category'] + (f" / {r['subcategory']}" if r['subcategory'] else ''),
                   f"[`{r['name']}`]({r['document']})", r['function']] for r in rows if r['kind'] == 'skill']
    skill_rows.extend([[g, '—', '暂无'] for g in SKILL_GROUPS[-2:]])
    result.extend([table(['分类', 'Skill', '当前功能'], skill_rows), '### 应用', '',
                   table(['分类', '应用', '当前功能'], [[r['category'], f"[{r['name']}]({r['document']})", r['function']] for r in rows if r['kind'] == 'application'])])
    return '\n'.join(result).rstrip() + '\n'


def generated_documents(root: Path = ROOT):
    rows = load_catalog(root)
    path = root / GUIDE
    guide = path.read_text(encoding='utf-8')
    prefix = guide.split('## Skills 分类与调用', 1)[0]
    if prefix == guide:
        raise ValueError('说明书缺少 Skills 分类与调用入口')
    readme_path = root / 'README.md'
    readme = readme_path.read_text(encoding='utf-8')
    # Old markers are accepted only as migration input and never re-emitted.
    readme = re.sub(r'(?m)^<!-- capability-tables:(?:start|end) -->\s*\n', '', readme)
    start, stop = readme_table_region(readme)
    updated_guide = prefix + guide_sections(rows)
    if markdown_headings(updated_guide) != expected_headings():
        raise ValueError('说明书公共约定包含越界标题，请先迁移详细内容')
    readme = readme[:start] + readme_tables(rows) + readme[stop:]
    readme = re.sub(r'(\*\*Skills：\*\* )\d+', lambda m: m[1] + str(sum(r['kind'] == 'skill' for r in rows)), readme)
    return {path: updated_guide, readme_path: readme}


def generate(root: Path = ROOT, *, check=False, run_dir=None):
    return publish_documents(generated_documents, root, check=check, run_dir=run_dir)


def publish_documents(builder, root=ROOT, *, check=False, run_dir=None,
                      workflow='project-doc-audit', run_name='catalog-generation'):
    """Shared recoverable publication for catalog and Skill contract generators."""
    root = Path(root).resolve()
    def validate(updates):
        from jsonschema.validators import validator_for
        from jsonschema.exceptions import SchemaError
        if not updates or any(not Path(p).resolve().is_relative_to(root) for p in updates):
            raise ValueError('文档发布目标必须位于当前项目内')
        for path, text in updates.items():
            if Path(path).suffix == '.json':
                value = json.loads(text)
                if Path(path).name.endswith('.schema.json'):
                    if not isinstance(value, (dict, bool)):
                        raise ValueError('生成 Schema 必须为对象或布尔值')
                    try:
                        validator_for(value).check_schema(value)
                    except SchemaError as error:
                        raise ValueError(f'生成 Schema 无效：{Path(path).name}') from error
    if check:
        updates = builder(root)
        validate(updates)
        if any(p.read_text(encoding='utf-8') != text for p, text in updates.items()):
            raise ValueError('生成文档与模板不一致')
        return {'status': 'verified'}
    logs = root / 'logs' / workflow / 'runs'
    run = (root / Path(run_dir)).resolve() if run_dir else create_run_directory(logs) / run_name
    if not run.is_relative_to(logs) or run.name != run_name or len(run.relative_to(logs).parts) != 2:
        raise ValueError(f'检查点必须位于 logs/{workflow}/runs/<run-id>/{run_name}/')
    flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
    try:
        if flow.state in ('prepared', 'paused_error'):
            flow.move('loading')
        updates = builder(root)
        digest = hashlib.sha256(json.dumps({p.relative_to(root).as_posix(): t for p, t in updates.items()}, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        if flow.details.get('input_sha256', digest) != digest:
            raise ValueError('生成输入已变化，请开启新运行')
        if flow.state == 'loading':
            flow.move('validating', input_sha256=digest)
        validate(updates)
        if flow.state == 'validating':
            flow.move('publishing')
        if flow.state == 'publishing':
            write_text_transaction(updates)
            flow.move('verifying')
        if any(p.read_text(encoding='utf-8') != text for p, text in updates.items()):
            raise ValueError('生成内容校验失败')
        if flow.state == 'verifying':
            flow.move('completed')
        return {'status': 'completed', 'run_dir': str(run)}
    except Exception:
        if 'paused_error' in TRANSITIONS[flow.state]:
            flow.move('paused_error')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成或校验 Skills 与应用概览')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.root, check=args.check, run_dir=args.run_dir), ensure_ascii=False))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(4 if args.check else 2)
    except OSError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(4 if args.check else 5)
