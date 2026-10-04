"""Generate the material-quote schema and shared contract documentation."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts.structured_io import write_text_transaction, json_digest
from utils.scripts.workflow_checkpoint import WorkflowCheckpoint, create_run_directory
from utils.scripts.capability_catalog import markdown_section, markdown_headings
START = '<!-- teaching-context-v7:start -->'
END = '<!-- teaching-context-v7:end -->'


TRANSITIONS = {
    'prepared': ('preparing',), 'preparing': ('validating', 'paused_error'),
    'validating': ('publishing', 'paused_error'), 'publishing': ('verifying', 'paused_error'),
    'verifying': ('completed', 'paused_error'), 'completed': (), 'paused_error': ('preparing',),
}


def build_updates(root):
    schema = json.loads((root/'utils/references/interactive-tutor-lesson-decision-v6.schema.json').read_text(encoding='utf-8'))
    schema['properties']['schema_version']['const'] = '7.0'
    variants = schema['properties']['teaching_points']['items']['properties']['blocks']['items']['properties']['parts']['items']['oneOf']
    variants.append({
        'type': 'object', 'additionalProperties': False,
        'required': ['type', 'evidence_id', 'edited'],
        'properties': {'type': {'const': 'material_quote'}, 'evidence_id': {'type': 'string', 'minLength': 1},
                       'edited': {'type': 'boolean'}, 'edited_text': {'type': 'string', 'minLength': 1},
                       'resolved': {'type': 'object', 'additionalProperties': False,
                                    'required': ['text', 'source', 'evidence'],
                                    'properties': {'text': {'type': 'string', 'minLength': 1},
                                                   'source': {'type': 'string', 'minLength': 1},
                                                   'evidence': {'type': 'object'}}}},
        'allOf': [{'if': {'properties': {'edited': {'const': True}}},
                   'then': {'required': ['edited_text']},
                   'else': {'not': {'required': ['edited_text']}}}]
    })
    path = root/'utils/references/interactive-tutor-lesson-decision-v7.schema.json'
    updates = {path: json.dumps(schema, ensure_ascii=False, indent=2)+'\n'}
    rows = [('type', '`material_quote`'), ('evidence_id', '脚本预填的原材料证据 ID'),
            ('edited', 'false：脚本提取原文；true：提供删改正文'),
            ('edited_text', '仅 edited=true 时填写；不手写标记或出处'),
            ('resolved', '脚本派生的正文、来源及证据快照；输入值不被信任')]
    table = '\n'.join(['| 字段 | 契约 |', '|---|---|'] + [f'| {a} | {b} |' for a, b in rows])
    section = f'''{START}
## 材料上下文与步骤复核（v7）

新课入口要求 `interactive-tutor-lesson-decision-v7.schema.json`；历史 v4/v5/v6 继续读取，不自动重写。默认学习者没有读过材料。解释材料中的观点、人物或案例前，交代必要背景，按上下文需要插入材料引用块，再解释其与学习目标的关系；不要求每课机械插入引用。引用本身不能单独算作 explained，讲解块的短正文疑点统计排除引文，整课正文计数仍包含引文。

`prepare-lesson` 在证据包预填 `material_quote_candidates`（原文、出处、定位及小 part 模板）与 `ordered_list_template`。Agent 选择候选，必要时只填少量删改文字、背景及解释。材料引用保留原意；引用讲者的历史举例不能冒充已核实事实。普通解释以清楚的短句表达材料性质与核实范围，不机械拼接分号，不全局替换原文标点。

{table}

删改引用的正文开头由 Python 添加 `（有删改）`，出处自动展示材料名称、章节与行号。去掉原文时间戳、删除句子或改写用语均使用 edited=true。保留现有通用 quote，用于其他注明来源的引文。材料引用验证来源文件 SHA-256、定位切片、证据 ID 与本知识点的关联；发布与 verify 复核相同派生正文。

步骤使用 ordered_list，示例另起 paragraph；并列使用 unordered_list。脚本扫描段落与列表项中的材料指代、连续步骤和材料性质/核实范围的分号疑点；每个材料引用（含原文引用）复核背景是否足够、是否支持当前讲解，删改引用额外复核原意。Agent 仅填写逐块 sufficient/needs_expansion 与具体理由；合理不引用或不拆列表可说明依据。沿用 `learning-teaching-review-v1.schema.json`，short_complete_reason 在本复核中留空。

共享实现为 `utils/scripts/learning_teaching_context.py`，子状态机为 prepared → validating → checking_context_layout → review_required（有疑点）→ validating_review → completed；无疑点直接 completed，需修订进入 revision_required，异常进入 paused_error。需要修改时必须修改教学正文或引用再复核；改变块 ID、覆盖标签、题目或配置不能绕过修订。检查点、候选、模板及结果写入 `logs/interactive-tutor/runs/<run-id>/teaching-context/<lesson-id>/<输入指纹>/`；输入变化使旧判断失效。非法或过期复核不改检查点，重复调用保留已填模板，中断恢复重新校验来源，并重放已归档判断。JSON 原子写入；损坏的既有依据拒绝覆盖。新复核依据冻结 policy_version=2；已发布且未保存该字段的 v7 记录按原规则校验，历史课程不自动重写。

`publish-lesson --decision <JSON> [--layout-review <小复核JSON>] [--quality-review <讲解复核JSON>]` 返回 teaching_layout_review_required/teaching_layout_revision_required 时退出码 3，保持 lesson_decision_required，不发布、不启动播客、不接受作答。来源校验与确定性排版后执行上下文复核，再继续覆盖、讲解质量、习题质量和播客门禁。正式课程保存复核依据与判断；verify 检查来源、复核、正文、历史版本及独立归档的 review.json。

契约由 `runtime/.venv/Scripts/python.exe utils/scripts/render_learning_teaching_context_contract.py` 生成；状态机为 prepared → preparing → validating → publishing → verifying → completed，异常进入 paused_error。检查点写入 logs/interactive-tutor/runs/<run-id>/contract-generation/，时间与 run ID 由共享 utils/scripts/timestamp.py 生成；使用 --run-dir 恢复，同检查点绑定同一生成输入。Schema 与各文档通过共享文件事务同时更新，失败回滚。回归覆盖原文/删改引用、来源损坏、上下文与步骤复核、过期/重复/中断恢复、引用单独冒充讲解、CLI 发布/verify、历史 v6/v7 读取、元数据绕过拒绝及契约生成回滚。
{END}'''
    for relative in ('skills/interactive-tutor/SKILL.md', 'docs/PRDs/交互式学习.md'):
        target = root/relative
        if relative.startswith('docs/PRDs/') and not target.is_file():
            continue
        text = target.read_text(encoding='utf-8')
        text = text.replace('新发布课程使用 `interactive-tutor-lesson-decision-v6.schema.json`。',
                            'v6 历史课程使用 `interactive-tutor-lesson-decision-v6.schema.json`。新课以文末 v7 契约为准。')
        text = text.replace('新课程的公共 `publish-lesson` 入口要求 v6 parts', '新课程的公共 `publish-lesson` 入口要求 v7 parts')
        text = text.replace('新发布使用 v6 parts', '新发布使用 v7 parts')
        text = text.replace('显式重新发布使用 v6 并保留旧版本', '显式重新发布使用 v7 并保留旧版本')
        text = text.replace('## 新课程讲解排版（v6）', '## 课程讲解排版（v6 基础兼容）')
        text = text.replace('verify 复核 v5/v6 教学块', 'verify 复核 v5/v6/v7 教学块')
        text = text.replace('v5/v6 发布前', 'v5/v6/v7 发布前')
        text = text.replace('新发布入口采用 v6 parts', '新发布入口采用 v7 parts')
        text = text.replace('新发布 v6；保留历史 v4/v5 读取', '新发布 v7；保留历史 v4/v5/v6 读取')
        if START in text:
            start = text.index(START); end = text.index(END, start)+len(END)
            text = text[:start]+section+text[end:]
        else: text = text.rstrip()+'\n\n'+section+'\n'
        updates[target] = text
    # The application README is a user entry point, not a second detailed contract.
    target = root/'applications/交互式学习/README.md'
    text = target.read_text(encoding='utf-8')
    summary = ('## 讲解中的引用与步骤\n\n'
               '课程会先交代必要背景，再引用和解释材料；引用如有删改会明确标注。'
               '步骤按顺序列出，示例与步骤分开，避免混淆。讲解检查通过后才发布课程并开始答题。'
               '历史课程保持原有记录。\n\n'
               '完整实现与历史兼容说明见 [导师 Skill](../../skills/interactive-tutor/SKILL.md)。\n')
    if START in text:
        start = text.index(START); end = text.index(END, start)+len(END)
        tail = text[end:].lstrip('\n')
        text = text[:start] + summary + ('\n' + tail if tail else '')
    elif (2, '讲解中的引用与步骤') in markdown_headings(text):
        start, end = markdown_section(text, 2, '讲解中的引用与步骤')
        text = text[:start] + summary.split('\n', 1)[1] + ('\n' if end < len(text) else '') + text[end:]
    else:
        text = text.rstrip() + '\n\n' + summary
    updates[target] = text
    return updates


def generate(root=ROOT, *, run_dir=None):
    """Recoverable contract generation; prepare all files and publish them in one transaction."""
    root = Path(root).resolve()
    logs_root = root/'logs/interactive-tutor/runs'
    if run_dir:
        run = (root/Path(run_dir)).resolve()
        if not run.is_relative_to(logs_root) or run.name != 'contract-generation' or len(run.relative_to(logs_root).parts) != 2:
            raise ValueError('契约生成检查点必须位于 logs/interactive-tutor/runs/<run-id>/contract-generation/')
    else:
        run = create_run_directory(logs_root)/'contract-generation'
    flow = WorkflowCheckpoint(TRANSITIONS, run, resume=True, restart_completed=False)
    try:
        if flow.state in ('prepared', 'paused_error'): flow.move('preparing')
        updates = build_updates(root)
        fingerprint = json_digest({p.relative_to(root).as_posix(): text for p, text in updates.items()})
        if flow.details.get('input_sha256', fingerprint) != fingerprint:
            raise ValueError('契约生成输入已变化，请开启新运行，不复用旧检查点')
        if flow.state == 'preparing': flow.move('validating', input_sha256=fingerprint)
        from jsonschema import Draft202012Validator
        schema_path = root/'utils/references/interactive-tutor-lesson-decision-v7.schema.json'
        Draft202012Validator.check_schema(json.loads(updates[schema_path]))
        if flow.state == 'validating': flow.move('publishing')
        if flow.state == 'publishing':
            write_text_transaction(updates)
            flow.move('verifying')
        if any(p.read_text(encoding='utf-8') != text for p, text in updates.items()):
            raise ValueError('契约生成结果与确定性模板不一致')
        if flow.state == 'verifying': flow.move('completed')
        return {'status': 'completed', 'run_dir': str(run), 'input_sha256': fingerprint}
    except Exception:
        if 'paused_error' in TRANSITIONS[flow.state]: flow.move('paused_error')
        raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='使用状态机与文件事务生成 v7 契约')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.root, run_dir=args.run_dir), ensure_ascii=False))
