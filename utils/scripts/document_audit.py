"""Deterministic project documentation and environment audit helpers."""
from __future__ import annotations
import ast, hashlib, json, os, re, subprocess, sys
from pathlib import Path
from typing import Any
from skill_catalog import ALLOWED_CATEGORIES, load_documented_categories, load_documented_category_occurrences, load_documented_scenarios, load_skill_metadata, validate_scenario

EXCLUDED = {'.git', 'logs', 'outputs', 'tmp', '__pycache__', 'node_modules', '.cache', '.pytest_cache'}

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()

def discover_docs(root: Path) -> list[Path]:
    wanted = {"AGENTS.md", "CODE_OF_CONDUCT.md", "README.md", "SOURCE_OF_TRUTH.md"}
    found = set()
    for p in root.rglob("*.md"):
        parts = set(p.relative_to(root).parts)
        if parts & EXCLUDED or '.backup' in parts or '.venv' in parts:
            continue
        rp = rel(root, p)
        if rp in wanted or rp.startswith("docs/") or p.name == "README.md":
            found.add(p)
    return sorted(found)

def active_skill_dirs(root: Path) -> set[str]:
    base = root / 'skills'
    return {p.name for p in base.iterdir() if p.is_dir() and p.name != '.backup' and (p / 'SKILL.md').is_file()} if base.is_dir() else set()

def add(findings, kind, path, suggestion, current=None, expected=None, evidence=None, confidence='deterministic', section=None):
    findings.append({'kind': kind, 'path': path, 'section': section, 'current': current, 'expected': expected,
                     'evidence': evidence or [], 'suggestion': suggestion, 'confidence': confidence, 'status': 'new'})

def parse_keys(path: Path) -> set[str]:
    if not path.is_file(): return set()
    return {m.group(1) for m in (re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line) for line in path.read_text(encoding='utf-8').splitlines()) if m}

def parse_requirements(path: Path) -> dict[str, str]:
    result = {}
    if not path.is_file(): return result
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line or line.startswith(('-', 'git+', 'http')): continue
        m = re.match(r'^([A-Za-z0-9_.-]+)\s*(.*)$', line)
        if m: result[m.group(1).lower().replace('_','-')] = m.group(2).strip()
    return result

def imported_modules(root: Path) -> set[str]:
    mods = set()
    for base in (root/'skills', root/'utils', root/'runtime'):
        if not base.exists(): continue
        for p in base.rglob('*.py'):
            parts = set(p.relative_to(root).parts)
            if parts & EXCLUDED or '.backup' in parts or '.venv' in parts: continue
            try: tree = ast.parse(p.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError): continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import): mods.update(a.name.split('.')[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0: mods.add(node.module.split('.')[0])
    return mods

def installed_versions(root: Path) -> dict[str, str]:
    exe = root/'runtime/.venv/Scripts/python.exe' if os.name == 'nt' else root/'runtime/.venv/bin/python'
    if not exe.is_file(): return {}
    code = 'import importlib.metadata as m, json; print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}))'
    try:
        out = subprocess.check_output([str(exe), '-c', code], text=True, stderr=subprocess.STDOUT, timeout=20)
        return json.loads(out)
    except Exception: return {}

def version_ok(version: str, spec: str) -> bool:
    if not spec: return True
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        return Version(version) in SpecifierSet(spec)
    except Exception:
        return True

def audit(root: Path, previous: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    findings=[]; facts={}
    docs=discover_docs(root); facts['documents']={rel(root,p):sha(p) for p in docs}
    # Check explicit repository-relative paths in inline code spans.
    for p in docs:
        text=p.read_text(encoding='utf-8')
        for token in re.findall(r'`([^`]+)`', text):
            token=token.strip().replace('\\','/')
            if not token or token.startswith(('http://','https://','<','python ','runtime/.venv/Scripts/python.exe ')) or any(ch.isspace() for ch in token) or '<' in token or token in {'SKILL.md','README.md'}: continue
            candidate=root/Path(token)
            if ('/' in token or token.endswith(('.md','.json','.py','.txt','.yaml','.yml'))) and not candidate.exists():
                add(findings,'missing_document_reference',rel(root,p),'更新失效的项目路径引用',token,'现有路径',[rel(root,p)],'deterministic')
    version=(root/'VERSION').read_text(encoding='utf-8').strip() if (root/'VERSION').is_file() else None
    marketplace=root/'.claude-plugin/marketplace.json'; plugin=root/'.claude-plugin/plugin.json'
    try: market=json.loads(marketplace.read_text(encoding='utf-8'))
    except Exception: market={}
    try: manifest=json.loads(plugin.read_text(encoding='utf-8'))
    except Exception: manifest={}
    market_version=((market.get('plugins') or [{}])[0]).get('version')
    if version is None: add(findings,'missing_version','VERSION','创建只包含版本号的 VERSION 文件')
    elif market_version != version: add(findings,'version_mismatch','.claude-plugin/marketplace.json','使 marketplace 版本与 VERSION 一致',market_version,version,['VERSION'])
    registered={Path(str(x).removeprefix('./')).name for x in manifest.get('skills',[])}
    active=active_skill_dirs(root)
    if registered != active: add(findings,'skills_registry_mismatch','.claude-plugin/plugin.json','使注册 Skills 与 skills/ 下活动目录一致',sorted(registered),sorted(active),['skills','.claude-plugin/plugin.json'])
    metadata = load_skill_metadata(root)
    for skill_name in sorted(active):
        item = metadata.get(skill_name, {})
        category = item.get('category')
        if not category:
            add(findings, 'skill_category_missing', f'skills/{skill_name}/SKILL.md', '在 YAML front matter 中声明 category', None, sorted(ALLOWED_CATEGORIES), [f'skills/{skill_name}/SKILL.md'])
        elif category not in ALLOWED_CATEGORIES:
            add(findings, 'skill_category_invalid', f'skills/{skill_name}/SKILL.md', '将 category 改为允许的分类值', category, sorted(ALLOWED_CATEGORIES), [f'skills/{skill_name}/SKILL.md'])
        if item.get('name') and item['name'] != skill_name:
            add(findings, 'skill_metadata_name_mismatch', f'skills/{skill_name}/SKILL.md', '使 front matter 的 name 与 Skill 目录名一致', item['name'], skill_name, [f'skills/{skill_name}/SKILL.md'])
    catalog_path = root / 'docs/Skills_说明书.md'
    documented_categories = load_documented_categories(catalog_path) if catalog_path.is_file() else {}
    category_occurrences = load_documented_category_occurrences(catalog_path) if catalog_path.is_file() else {}
    category_labels = {
        'project_function': '项目功能',
        'ai_assisted_learning': 'AI 辅助学习',
        'ai_assisted_teaching': 'AI 辅助教学',
        'ai_assisted_research': 'AI 辅助科研',
        'common_tool': '常用工具（与 AI 辅助教育无关）',
    }
    for skill_name in sorted(active):
        metadata_category = metadata.get(skill_name, {}).get('category')
        documented_category = documented_categories.get(skill_name)
        expected_label = category_labels.get(metadata_category)
        occurrences = category_occurrences.get(skill_name, [])
        if len(occurrences) > 1:
            add(findings, 'skill_catalog_duplicate', 'docs/Skills_说明书.md', '确保每个 Skill 只出现在一个分类概览表中', occurrences, [expected_label] if expected_label else [], ['docs/Skills_说明书.md'])
        if documented_category and expected_label and documented_category != expected_label:
            add(findings, 'skill_category_mismatch', 'docs/Skills_说明书.md', '使说明书分类与 SKILL.md front matter 的 category 一致', documented_category, expected_label, ['docs/Skills_说明书.md', f'skills/{skill_name}/SKILL.md'])
        if not documented_category:
            add(findings, 'skill_catalog_missing', 'docs/Skills_说明书.md', '在分类概览表中补充活动 Skill', None, expected_label or sorted(category_labels.values()), ['docs/Skills_说明书.md', f'skills/{skill_name}/SKILL.md'])
    documented = load_documented_scenarios(catalog_path) if catalog_path.is_file() else {}
    for skill_name in sorted(active):
        examples = documented.get(skill_name, [])
        if not examples:
            add(findings, 'skill_scenario_missing', 'docs/Skills_说明书.md', '在对应 Skill 详细章节添加至少一个具体场景示例', [], ['id', 'user_request', 'when_to_call', 'invocation', 'expected_output'], ['docs/Skills_说明书.md', f'skills/{skill_name}/SKILL.md'])
            continue
        for example in examples:
            missing = validate_scenario(example)
            if missing:
                add(findings, 'skill_scenario_invalid', 'docs/Skills_说明书.md', '补齐具体场景示例的结构化字段', missing, [], ['docs/Skills_说明书.md', f'skills/{skill_name}/SKILL.md'])
    documented_names = {name for name in documented if name in active}
    for name in sorted(set(documented) - active):
        if name not in {'AI 辅助学习', 'AI 辅助教学', 'AI 辅助科研', '常用工具（与 AI 辅助教育无关）', '项目功能'}:
            add(findings, 'skill_scenario_unknown', 'docs/Skills_说明书.md', '删除未注册 Skill 的场景章节或注册对应 Skill', name, sorted(active), ['docs/Skills_说明书.md'])
    example_keys=parse_keys(root/'.env.example')
    env_files=sorted(p for p in root.glob('.env*') if p.is_file() and p.name != '.env.example')
    env_key_map={rel(root,p):sorted(parse_keys(p)) for p in env_files}
    for env_path, keys in env_key_map.items():
        unknown=sorted(set(keys)-example_keys)
        if unknown: add(findings,'env_key_not_declared',env_path,'删除未在 .env.example 声明的键或补充示例声明',unknown,sorted(example_keys),['.env.example'])
    req=parse_requirements(root/'runtime/.venv/requirements.txt'); mods=imported_modules(root)
    map_path=root/'utils/references/python-package-map.json'
    try: mapping=json.loads(map_path.read_text(encoding='utf-8'))
    except Exception: mapping={'yaml':'pyyaml','jsonschema':'jsonschema'}
    std=set(getattr(sys,'stdlib_module_names',())) | {'__future__'}
    local={p.stem for base in (root/'skills',root/'utils',root/'runtime') if base.exists() for p in base.rglob('*.py')}
    mapping={str(k).lower():str(v).lower() for k,v in mapping.items()}
    third={mapping.get(m,m) for m in mods if m not in std and m not in local and m not in {'skills','utils','runtime'}}
    undeclared=sorted(third-set(req))
    if undeclared: add(findings,'undeclared_import','runtime/.venv/requirements.txt','声明活动 Python 脚本使用的第三方包',undeclared,sorted(req),['skills','utils','runtime'])
    installed=installed_versions(root)
    if (root/'runtime/.venv').exists() and not installed: add(findings,'environment_unreadable','runtime/.venv','使用可用的虚拟环境 Python 读取实际安装版本',None,'可读取包版本',['runtime/.venv'])
    missing=sorted(set(req)-set(installed)) if installed else []
    if missing: add(findings,'dependency_not_installed','runtime/.venv/requirements.txt','安装 requirements 中缺失的依赖',missing,sorted(installed),['runtime/.venv/requirements.txt'])
    mismatch=sorted(k for k,spec in req.items() if k in installed and not version_ok(installed[k],spec))
    if mismatch: add(findings,'dependency_version_mismatch','runtime/.venv/requirements.txt','调整 requirements 或安装满足约束的版本',{k:installed[k] for k in mismatch},{k:req[k] for k in mismatch},['runtime/.venv'])
    facts.update({'documents':facts['documents'],'version':version,'marketplace_version':market_version,'registered_skills':sorted(registered),'active_skills':sorted(active),'skill_metadata':metadata,'documented_categories':documented_categories,'documented_scenarios':documented,'requirements':req,'imports':sorted(third),'installed':installed,'env_keys':env_key_map})
    cache={'schema_version':'1.0','checker_version':'1.1','facts':facts,'documents':facts['documents'],'finding_count':len(findings)}
    return {'schema_version':'1.0','findings':findings,'summary':{'checked_documents':len(docs),'findings':len(findings),'status':'differences_found' if findings else 'consistent'}}, cache


