"""Deterministic overview boundaries and cross-document catalog checks."""
from __future__ import annotations

import re
import os
from pathlib import Path

try:
    from .capability_catalog import GUIDE, markdown_headings, markdown_heading_spans, markdown_section, readme_table_region, expected_headings, load_catalog, generated_documents
except ImportError:
    from capability_catalog import GUIDE, markdown_headings, markdown_heading_spans, markdown_section, readme_table_region, expected_headings, load_catalog, generated_documents


def document_responsibility_findings(root: Path):
    """Check public document boundaries using existing finding fields and kinds."""
    result = []
    def add(path, current, expected, suggestion):
        result.append(dict(kind='catalog_scope_violation', path=path, current=current, expected=expected,
                           suggestion=suggestion, evidence=[path], confidence='deterministic', status='new', section=None))
    architecture = 'docs/architecture/README.md'
    files = {name: (root/name).read_text(encoding='utf-8') for name in
             ('AGENTS.md', 'SOURCE_OF_TRUTH.md', 'CODE_OF_CONDUCT.md', architecture) if (root/name).is_file()}
    agents = files.get('AGENTS.md', '')
    if 'AGENTS.md' in files:
        for target in ('SOURCE_OF_TRUTH.md', architecture):
            if not re.search(r'\[[^\]]+\]\(' + re.escape(target) + r'\)', agents):
                add('AGENTS.md', f'缺少 {target} 链接', '来源职责和详细文件架构使用链接引用', '补齐权威来源与详细文件架构链接')
        if re.search(r'^- `(?:CODE_OF_CONDUCT|SOURCE_OF_TRUTH)\.md` 规定', agents, re.M):
            add('AGENTS.md', '重复来源职责清单', 'SOURCE_OF_TRUTH.md 集中维护来源职责', '将来源职责清单合并到 SOURCE_OF_TRUTH.md，AGENTS.md 保留引用')
    for name in ('AGENTS.md', 'CODE_OF_CONDUCT.md'):
        if name in files and 'SOURCE_OF_TRUTH.md' not in files[name]:
            add(name, '未引用来源职责', 'SOURCE_OF_TRUTH.md', '引用集中维护的来源与文档职责')
    if architecture in files:
        text = files[architecture]
        headings = markdown_headings(text)
        if (2, '文件与目录') not in headings:
            add(architecture, '缺少文件与目录说明', '项目文件架构', '说明主要目录、根目录文件及其用途')
        if any(title in ('时间戳约定', '语音工作流') for _, title in headings) or re.search(r'\b(?:prepared|initialized)\s*→|paused_[a-z_]+|九字段|backend_resolved', text):
            add(architecture, '重复时间戳或 Skill 实现协议', '文件结构、用途与详细说明入口', '将有效实现规则迁入所属 Skill 已链接文档，删除重复时间戳章节')
    # Long identical rule lines indicate copied authority, not necessary short links.
    sources = files.get('SOURCE_OF_TRUTH.md', '')
    authority_lines = {line.strip() for line in sources.splitlines() if line.startswith('- ') and len(line) >= 50}
    for name in ('AGENTS.md', 'CODE_OF_CONDUCT.md'):
        repeated = authority_lines.intersection(line.strip() for line in files.get(name, '').splitlines())
        if repeated:
            add(name, sorted(repeated), '各文档只维护自身职责并引用权威来源', '去掉整段复制的来源规则，保留执行或维护要求及引用')
    return result


def user_language_findings(root: Path):
    """Collect bounded review cues from user-facing prose, not commands or developer sections."""
    result = []
    candidates = [root/'README.md', root/GUIDE, root/'applications/README.md']
    candidates.extend(sorted((root/'applications').glob('*/README.md')))
    terms = re.compile(r'\bSchema\b|状态机|任务路由|批次关系|依赖图|handoff|门禁|指纹绑定', re.I)
    technical = re.compile(r'实现|开发|协议|验证|测试|数据|目录|兼容|迁移')
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        # Only introduction and usage sections; dedicated implementation sections may use terms.
        spans = markdown_heading_spans(text)
        sections = [(0, spans[0][2] if spans else len(text), '')]
        for i, (level, title, start, end) in enumerate(spans):
            stop = spans[i+1][2] if i+1 < len(spans) else len(text)
            sections.append((end, stop, title))
        cues = []
        for start, stop, title in sections:
            if technical.search(title):
                continue
            body = text[start:stop]
            # Fenced commands and inline identifiers are exact interfaces, not prose.
            body = re.sub(r'(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1\s*$', '', body)
            body = re.sub(r'`[^`\n]*`|\]\([^)]*\)', '', body)
            for line in body.splitlines():
                matches = sorted(set(terms.findall(line)))
                # An explicit explanation is a reviewer's evidence, not an automatic ban.
                if matches and not re.search(r'指的是|也就是|即：|即，|是指|意思是', line):
                    cues.append({'section': title, 'terms': matches})
        if cues:
            relative = path.relative_to(root).as_posix()
            result.append(dict(kind='catalog_scope_violation', path=relative, current=cues[:3],
                               expected='功能与操作说明采用通俗语言，必要术语有简短解释',
                               suggestion='仅为语义复核线索：检查上下文，必要时改为通俗说明；不得仅凭关键词判定违规',
                               evidence=[relative], confidence='needs_review', status='new', section=None))
    return result


def overview_findings(root: Path, on_stage=None):
    stage = on_stage or (lambda name: None)
    stage('checking_document_scope')
    result = []
    result.extend(document_responsibility_findings(root))
    result.extend(user_language_findings(root))
    def finding(kind, path, current, expected, suggestion):
        result.append(dict(kind=kind, path=path, current=current, expected=expected, suggestion=suggestion,
                           evidence=[path], confidence='deterministic', status='new', section=None))
    path = root / GUIDE
    if not path.is_file():
        finding('catalog_structure_invalid', GUIDE, None, expected_headings(), '恢复 Skills、应用说明书及规定标题层级')
        stage('checking_application_catalog')
        stage('checking_catalog_consistency')
        return result
    text = path.read_text(encoding='utf-8')
    actual = markdown_headings(text)
    if actual != expected_headings():
        finding('catalog_structure_invalid', GUIDE, actual, expected_headings(), '修复完整标题树，子分类必须位于规定父级下')
    if re.search(r'scenario_examples:|schema\.json|技术路线|实现细节|正文覆盖与新课程协议|paused_[a-z_]+', text):
        finding('catalog_scope_violation', GUIDE, '详细场景或实现协议', '功能、时机、入口和文档链接', '将实现和详细协议迁至对应 Skill 或应用文档')
    stage('checking_application_catalog')
    try:
        rows = load_catalog(root)
    except (ValueError, OSError, KeyError, TypeError) as error:
        finding('catalog_data_invalid', 'utils/references/capability-catalog.json', str(error), '已确认的八字段目录', '修复目录字段、分类和成员完整性')
        stage('checking_catalog_consistency')
        return result
    stage('checking_catalog_consistency')
    try:
        updates = generated_documents(root)
    except (ValueError, OSError) as error:
        finding('catalog_generated_mismatch', GUIDE, str(error), '完整标题树及两张功能表', '修复生成入口和总览文档边界')
        updates = {}
    for target, expected in updates.items():
        if target.read_text(encoding='utf-8') != expected:
            finding('catalog_generated_mismatch', target.relative_to(root).as_posix(), '内容与目录数据不一致', '共享模板生成内容', '运行 utils/scripts/capability_catalog.py 同步能力表格')
    count = len([r for r in rows if r['kind'] == 'skill'])
    readme_path = root / 'README.md'
    readme = readme_path.read_text(encoding='utf-8') if readme_path.is_file() else ''
    if f'**Skills：** {count} ' not in readme:
        finding('catalog_generated_mismatch', 'README.md', 'Skills 数量未对齐', count, '同步 README 的 Skills 数量')
    if re.search(r'<!--\s*capability-tables:', readme):
        finding('catalog_scope_violation', 'README.md', '残留功能表生成注释', '无内部生成标记的用户文档', '运行共享生成器移除旧标记')
    forbidden = [title for _, title in markdown_headings(readme)
                 if re.search(r'状态机|Schema|技术路线|详细协议|时间戳约定', title, re.I)]
    if forbidden:
        finding('catalog_scope_violation', 'README.md', forbidden, '项目概览和使用入口', '将专用实现章节迁入所属 Skill 的详细文档')
    if re.search(r'(?m)^```(?:yaml|json)\b|scenario_examples:|\b(?:prepared|initialized)\s*→', readme):
        finding('catalog_scope_violation', 'README.md', 'README 中包含详细状态或结构化协议', '项目功能和使用入口', '将详细状态机及 YAML/JSON 协议迁入所属 Skill 文档')
    try:
        start, stop = markdown_section(readme, 2, '功能表')
        table_start, table_stop = readme_table_region(readme)
        extra = readme[start:table_start] + readme[table_stop:stop]
        if re.search(r'(?m)^\||^```(?:yaml|json)|scenario_examples:|schema\.json|paused_[a-z_]+|技术路线|实现细节', extra):
            raise ValueError('功能表外存在重复表格或详细协议')
    except ValueError as error:
        finding('catalog_scope_violation', 'README.md', str(error), '两张生成表格及简短使用入口', '修复标题与表格边界，移走详细协议')
    # Source-only scan avoids local reports and dependencies; retired names are composed here.
    old_name = 'Skills' + '_说明书.md'
    candidates = [root/'AGENTS.md', root/'CODE_OF_CONDUCT.md', root/'SOURCE_OF_TRUTH.md', root/'README.md']
    for base in ('skills', 'utils', 'applications', 'docs'):
        for directory, dirs, files in os.walk(root/base):
            dirs[:] = [d for d in dirs if d not in {'node_modules', 'dist', '__pycache__', '.backup'}]
            candidates.extend(Path(directory)/f for f in files if Path(f).suffix in ('.md', '.py', '.json'))
    for candidate in candidates:
        if candidate.is_file() and old_name in candidate.read_text(encoding='utf-8'):
            finding('catalog_retired_reference', candidate.relative_to(root).as_posix(), '旧说明书路径', GUIDE, '更新旧文件引用')
    return result
