import { useCallback, useEffect, useRef, useState } from 'react';
import { action, configs, listProjects, listArchives, manageProject, readProject, saveConfig, uiConfig, version, type Archive, type Counts, type Config, type ConfigValue, type Edge, type Node, type Project, type ProjectSummary, type UiConfig } from './api';

type Kind = 'lesson' | 'unit';
type Selection = { kind: Kind; id: string } | null;
const defaults: UiConfig = { poll_interval_seconds: 5, graph: { min_zoom: 25, max_zoom: 400, prerequisite_color: '#8B5CF6', teaching_color: '#0891B2' } };
const statusName = (s: string) => s === 'completed' ? '已学习' : ['awaiting_answer', 'retry'].includes(s) ? '正在学习' : s === 'skipped' ? '已跳过' : '待学习';
const trackName = (track: string) => track === 'main' ? '主线' : track === 'branch' ? '支线' : '未分类';
function Icon({ kind }: { kind: 'projects' | 'graph' | 'settings' }) {
  return <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    {kind === 'projects' ? <path d="M3 6h7l2 2h9v12H3zM3 6V4h7l2 2" /> : kind === 'graph' ? <><circle cx="5" cy="5" r="3" /><circle cx="19" cy="7" r="3" /><circle cx="12" cy="19" r="3" /><path d="m8 5 8 1M6 8l5 8M17 10l-4 6" /></> : <><circle cx="12" cy="12" r="4" /><path d="m9 3 6 0 1 4 4 2v6l-4 2-1 4H9l-1-4-4-2V9l4-2z" /></>}
  </svg>;
}

function layout(nodes: Node[], edges: Edge[]) {
  const levels = new Map<string, number>(); const left = new Set(nodes.map(n => n.id));
  for (let pass = 0; pass <= nodes.length && left.size; pass++) {
    let changed = false;
    for (const id of [...left]) {
      const deps = edges.filter(e => e.successor_id === id).map(e => e.predecessor_id);
      if (deps.every(d => levels.has(d))) { levels.set(id, Math.max(-1, ...deps.map(d => levels.get(d)!)) + 1); left.delete(id); changed = true; }
    }
    if (!changed) { for (const id of left) levels.set(id, levels.size); break; }
  }
  const positions = new Map<string, { x: number; y: number }>(); const counts = new Map<number, number>();
  for (const n of nodes) { const level = levels.get(n.id) || 0; const col = counts.get(level) || 0; counts.set(level, col + 1); positions.set(n.id, { x: 115 + col * 235, y: 70 + level * 140 }); }
  return { positions, width: Math.max(650, Math.max(0, ...counts.values()) * 235), height: Math.max(530, (Math.max(0, ...levels.values()) + 1) * 140) };
}

// In a DAG, reverse traversal collects every edge on any root-to-target path.
export function upstreamEdges(nodes: Node[], edges: Edge[], target: string | null): Set<number> {
  const ids = new Set(nodes.map(n => n.id));
  const incoming = new Map<string, number[]>();
  edges.forEach((edge, index) => {
    if (!ids.has(edge.predecessor_id) || !ids.has(edge.successor_id)) return;
    const list = incoming.get(edge.successor_id) || [];
    list.push(index); incoming.set(edge.successor_id, list);
  });
  const result = new Set<number>(); const visited = new Set<string>();
  const pending = target && ids.has(target) ? [target] : [];
  while (pending.length) {
    const id = pending.pop()!;
    if (visited.has(id)) continue;
    visited.add(id);
    for (const index of incoming.get(id) || []) {
      result.add(index); pending.push(edges[index].predecessor_id);
    }
  }
  return result;
}

function Graph({ kind, project, selection, select, config, onAction }: { kind: Kind; project: Project; selection: Selection; select: (s: Selection) => void; config: UiConfig; onAction: (kind: Kind, id: string, skip: boolean) => Promise<void> }) {
  const nodes = kind === 'lesson' ? project.lessons : project.units; const edges = kind === 'lesson' ? project.lessonEdges : project.unitEdges;
  const { positions, width, height } = layout(nodes, edges);
  const highlighted = upstreamEdges(nodes, edges, selection?.kind === kind ? selection.id : null);
  const [zoom, setZoom] = useState(100); const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const [menu, setMenu] = useState<{ id: string; x: number; y: number; skipped: boolean } | null>(null);
  useEffect(() => { setZoom(100); setPan({ x: 0, y: 0 }); setMenu(null); }, [project.id]);
  useEffect(() => { if (!menu) return; const close = () => setMenu(null); window.addEventListener('click', close); return () => window.removeEventListener('click', close); }, [menu]);
  const related = new Map<string, Set<string>>();
  function mark(id: string, relation: string) { const set = related.get(id) || new Set(); set.add(relation); related.set(id, set); }
  if (selection && selection.kind !== kind) {
    if (selection.kind === 'lesson') {
      const lesson = project.lessons.find(n => n.id === selection.id);
      lesson?.prerequisites?.forEach(id => mark(id, 'prerequisite')); lesson?.teaches?.forEach(id => mark(id, 'teaching'));
    } else project.lessons.forEach(l => { if (l.prerequisites?.includes(selection.id)) mark(l.id, 'prerequisite'); if (l.teaches?.includes(selection.id)) mark(l.id, 'teaching'); });
  }
  const done = nodes.filter(n => n.status === 'completed').length;
  const active = nodes.filter(n => ['awaiting_answer', 'retry'].includes(n.status)).length;
  const skipped = nodes.filter(n => n.skip || n.status === 'skipped').length;
  const assessed = nodes.filter(n => n.mastery != null);
  const clamp = (z: number) => Math.min(config.graph.max_zoom, Math.max(config.graph.min_zoom, z));
  return <section className="graph-pane" aria-label={kind === 'lesson' ? '课程依赖图' : '知识点依赖图'}>
    <div className="graph-heading"><div><span className="eyebrow">{kind === 'lesson' ? 'COURSE MAP' : 'KNOWLEDGE MAP'}</span><h2>{kind === 'lesson' ? '课程依赖图' : '知识点依赖图'}</h2></div><strong>{nodes.length}<small> 个节点</small></strong></div>
    <div className="statistics"><span>已学 <b>{done}</b></span><span>在学 <b>{active}</b></span><span>待学 <b>{nodes.length - done - active - skipped}</b></span><span>跳过 <b>{skipped}</b></span><span>完成率 <b>{nodes.length ? Math.round(done / nodes.length * 100) : 0}%</b></span>{kind === 'unit' && <span>掌握度 <b>{assessed.length ? Math.round(assessed.reduce((sum, n) => sum + n.mastery!, 0) / assessed.length * 100) + '%' : '—'}</b></span>}</div>
    <div className="graph-canvas">
      {!nodes.length ? <div className="empty">课程尚未规划，请先开始交互式学习。</div> : <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="可交互依赖图"
        onWheel={e => { if (e.altKey) { e.preventDefault(); setZoom(z => clamp(z + (e.deltaY < 0 ? 10 : -10))); } }}
        onPointerDown={e => { if ((e.target as Element).closest('.graph-node')) return; drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; e.currentTarget.setPointerCapture(e.pointerId); }}
        onPointerMove={e => { if (drag.current) { const ratio = width / e.currentTarget.getBoundingClientRect().width; setPan({ x: drag.current.px + (e.clientX - drag.current.x) * ratio, y: drag.current.py + (e.clientY - drag.current.y) * ratio }); } }}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
        <defs><marker id={`arrow-${kind}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#73898c" /></marker><marker id={`gold-arrow-${kind}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#C59B23" /></marker></defs>
        <g transform={`translate(${pan.x} ${pan.y}) translate(${width / 2} ${height / 2}) scale(${zoom / 100}) translate(${-width / 2} ${-height / 2})`}>
          {edges.map((edge, i) => {
            const a = positions.get(edge.predecessor_id), b = positions.get(edge.successor_id);
            if (!a || !b) return null;
            const d = `M${a.x},${a.y + 43} C${a.x},${(a.y + b.y) / 2} ${b.x},${(a.y + b.y) / 2} ${b.x},${b.y - 49}`;
            return <g key={i} className="graph-edge" pointerEvents="none">
              <path d={d} fill="none" stroke="#73898c" strokeWidth="1.4" strokeDasharray={nodes.find(n => n.id === edge.successor_id)?.track === 'branch' ? '7 5' : undefined} markerEnd={`url(#arrow-${kind})`} />
              {highlighted.has(i) && <>
                <path d={d} className="upstream-edge-glow" />
                <path d={d} className="upstream-edge-flow" pathLength="100" markerEnd={`url(#gold-arrow-${kind})`} />
              </>}
            </g>;
          })}
          {nodes.map(n => {
            const p = positions.get(n.id)!; const key = kind + ':' + n.id; const pending = key in project.pending; const isSkipped = pending ? project.pending[key] : (n.skip || n.status === 'skipped');
            const rel = related.get(n.id); const dim = selection && selection.kind !== kind && !rel;
            const isActive = ['awaiting_answer', 'retry'].includes(n.status); const selected = selection?.kind === kind && selection.id === n.id;
            const fill = n.status === 'completed' ? '#D9F1DE' : isActive ? '#DCEFFB' : '#FBE0DD';
            const words = n.title.match(/.{1,13}/g) || [''];
            return <g className="graph-node" key={n.id} transform={`translate(${p.x} ${p.y})`} opacity={isSkipped ? .3 : dim ? .5 : 1} tabIndex={0} role="button" aria-pressed={selected} aria-label={`${n.number} ${n.title} ${trackName(n.track)} ${statusName(n.status)}`}
              onClick={() => select(selected ? null : { kind, id: n.id })} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(selected ? null : { kind, id: n.id }); } if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) { e.preventDefault(); const r = e.currentTarget.getBoundingClientRect(); setMenu({ id: n.id, x: Math.min(r.left, window.innerWidth - 190), y: Math.min(r.bottom, window.innerHeight - 80), skipped: isSkipped }); } }}
              onContextMenu={e => { e.preventDefault(); setMenu({ id: n.id, x: Math.min(e.clientX, window.innerWidth - 190), y: Math.min(e.clientY, window.innerHeight - 80), skipped: isSkipped }); }}>
              {rel?.has('prerequisite') && <rect x="-106" y="-51" width="212" height="102" rx="13" fill="none" stroke={config.graph.prerequisite_color} strokeWidth="4" />}
              {rel?.has('teaching') && <rect x={rel.has('prerequisite') ? -112 : -106} y={rel.has('prerequisite') ? -57 : -51} width={rel.has('prerequisite') ? 224 : 212} height={rel.has('prerequisite') ? 114 : 102} rx="15" fill="none" stroke={config.graph.teaching_color} strokeWidth="4" />}
              <rect x="-100" y="-45" width="200" height="90" rx="8" fill={fill} stroke={isActive ? '#C59B23' : '#171C1C'} strokeWidth={isActive || selected ? 3 : 1} strokeDasharray={n.track === 'branch' ? '7 4' : undefined} />
              {n.track === 'branch' && <g aria-hidden="true"><rect x="66" y="-39" width="27" height="24" rx="4" className="track-badge branch" /><text x="79.5" y="-22" textAnchor="middle" className="track-badge-text">支</text></g>}
              <text x="-85" y="-26" className="node-number">{n.number} · {n.group}</text>
              {words.slice(0, 2).map((line, i) => <text textAnchor="middle" y={i * 18 - 3} key={i} className="node-title">{line}{i === 1 && words.length > 2 ? '…' : ''}</text>)}
              <text textAnchor="middle" y="33" className="node-state">{pending ? '待同步 · ' : ''}{isSkipped ? '已屏蔽' : pending && n.status === 'skipped' ? '待恢复' : statusName(n.status)}</text>
              <title>{`${n.title} · ${trackName(n.track)}`}</title>
            </g>;
          })}
        </g>
      </svg>}
      <div className="graph-legend"><span><i className="track-line main" />主线</span><span><i className="track-line branch" />支线 · 支</span><span><i className="upstream-line" />金色流动边 · 到所选节点的上游路径</span><span><i style={{ background: config.graph.prerequisite_color }} />{kind === 'unit' ? '前置知识点' : '以此知识点为前置的课程'}</span><span><i style={{ background: config.graph.teaching_color }} />{kind === 'unit' ? '本课学习的知识点' : '讲解此知识点的课程'}</span><span><i style={{ background: '#D9F1DE' }} />已经学习</span><span><i style={{ background: '#DCEFFB' }} />正在学习</span><span><i style={{ background: '#FBE0DD' }} />待学习</span></div>
      <div className="zoom-controls"><input type="range" min={config.graph.min_zoom} max={config.graph.max_zoom} step="5" value={zoom} aria-label="缩放比例" onChange={e => setZoom(Number(e.target.value))} /><span>{zoom}%</span><button onClick={() => setZoom(z => clamp(z - 10))} disabled={zoom <= config.graph.min_zoom} aria-label="缩小">−</button><button onClick={() => setZoom(z => clamp(z + 10))} disabled={zoom >= config.graph.max_zoom} aria-label="放大">+</button><button onClick={() => { setZoom(100); setPan({ x: 0, y: 0 }); }}>适应</button></div>
    </div>
    {menu && <div className="context-menu" style={{ left: menu.x, top: menu.y }} role="menu"><button role="menuitem" disabled={!project.canEdit} onClick={() => { void onAction(kind, menu.id, !menu.skipped); setMenu(null); }}>{menu.skipped ? '恢复后续学习' : '屏蔽后续学习'}</button>{!project.canEdit && <small>请先建立学习项目</small>}</div>}
  </section>;
}

const fieldLabels: Record<string, string> = { assessment: '入学诊断', ordering: '候选路线', max_multiple_choice_questions: '选择题上限', max_candidate_orders: '候选路线数量上限', weights: '评分权重', goal_match: '学习目标匹配', background_match: '学习背景匹配', difficulty_smoothness: '难度平缓', topic_continuity: '主题连续', downstream_unlock: '后续解锁', lesson: '每课限制', reference_chars_min: '课程讲解正文参考下限（偏短复核）', reference_chars_max: '课程讲解正文参考上限（不强制拆课）', max_open_ended_questions: '问答题上限', max_new_units: '知识点上限', mastery: '掌握分类', good_min: '良好最低分', medium_min: '中等最低分', podcast: '课程播客', enabled: '生成播客版本' };
function ConfigFields({ value, update, prefix = [] }: { value: Record<string, ConfigValue>; update: (path: string[], value: number | boolean) => void; prefix?: string[] }) {
  return <>{Object.entries(value).map(([key, v]) => typeof v === 'object' ? <fieldset key={key}><legend>{fieldLabels[key] || key}</legend><ConfigFields value={v} update={update} prefix={[...prefix, key]} /></fieldset> : <label key={key}>{fieldLabels[key] || key}{typeof v === 'boolean' ? <input type="checkbox" checked={v} onChange={e => update([...prefix, key], e.target.checked)} /> : <input type="number" min={key.endsWith('_min') || prefix.includes('weights') ? 0 : 1} step={key.endsWith('_min') || prefix.includes('weights') ? '0.05' : '1'} value={v} onChange={e => update([...prefix, key], Number(e.target.value))} />}</label>)}</>;
}
function Settings() {
  const [data, setData] = useState<Record<string, Config>>({}); const [message, setMessage] = useState(''); const [busy, setBusy] = useState(false);
  const reload = () => configs().then(setData).catch(e => setMessage(String(e)));
  useEffect(() => { void reload(); }, []);
  const update = (skill: string, path: string[], value: number | boolean) => setData(current => { const next = structuredClone(current); let node = next[skill].config; for (const p of path.slice(0, -1)) node = node[p] as Record<string, ConfigValue>; node[path[path.length - 1]] = value; return next; });
  const save = async (skill: string) => { setBusy(true); try { const value = await saveConfig(skill, data[skill]); setData(current => ({ ...current, [skill]: value })); setMessage('配置已保存。本次运行在下一个安全检查点采用新参数，已发布课程保留原记录。'); } catch (e) { setMessage(String(e)); } finally { setBusy(false); } };
  return <div className="page-content"><span className="eyebrow">PREFERENCES</span><h1>设置</h1><p>参数会同步保存到对应技能的配置文件。</p><div className="settings-columns">{Object.entries(data).map(([skill, value]) => <section className="settings-card" key={skill}><h2>{skill.includes('navigation') ? '学习导航' : '交互式导师'}</h2><ConfigFields value={value.config} update={(path, value) => update(skill, path, value)} /><button className="primary" disabled={busy} onClick={() => void save(skill)}>保存配置</button></section>)}</div><p role="status">{message}</p><button onClick={() => void reload()}>重新加载配置</button></div>;
}

const phaseNames: Record<string, string> = { navigation_ready: '尚未开始教学', backup_required: '等待材料备份', lesson_plan_required: '等待课程规划确认', ready: '可以开始下一课', lesson_decision_required: '正在准备讲解', podcast_required: '正在准备播客', awaiting_answer: '等待作答', review_decision_required: '等待批改', processing_learning_feedback: '正在处理学习反馈', awaiting_feedback_questions: '等待反馈答疑', adaptation_decision_required: '等待下一课调整', awaiting_questions: '等待课后答疑', awaiting_route_choice: '等待选择路线', awaiting_branch_continuation: '等待确认支线学习', main_completed: '主线已完成，支线待学习', completed: '项目已完成' };
const nextActionNames: Record<string, string> = { navigation_ready: '在交互式导师中建立学习项目', backup_required: '确认材料备份后继续', lesson_plan_required: '确认课程规划后继续', ready: '开始下一课', lesson_decision_required: '继续导师流程以发布讲解', podcast_required: '完成播客生成与验证后学习本课', awaiting_answer: '填写课程作答区，或在对话中按题号回答', review_decision_required: '继续导师流程以批改答案', processing_learning_feedback: '继续处理学习反馈', awaiting_feedback_questions: '提出反馈问题，或确认继续', adaptation_decision_required: '确认下一课调整', awaiting_questions: '提出课后问题，或确认继续', awaiting_route_choice: '选择接下来的学习路线', awaiting_branch_continuation: '确认是否继续支线学习', main_completed: '选择待学习的支线课程', completed: '查看学习总结，按需复习' };
function ProgressSummary({ label, value }: { label: string; value: Counts }) {
  return <div className="detail-progress"><strong>{label}</strong><span>已学 {value.completed} · 在学 {value.learning} · 待学 {value.pending} · 跳过 {value.skipped}</span><progress max={1} value={value.completionRate || 0} /><small>完成率 {value.completionRate === null ? '—' : `${Math.round(value.completionRate * 100)}%`}</small></div>;
}
function ProjectDetail({ project: p, onClose, onGraph, onDelete, onRename, saving }: { project: Project; onClose: () => void; onGraph: () => void; onDelete: () => void; onRename: (title: string) => Promise<void>; saving: boolean }) {
  const [title, setTitle] = useState(p.title);
  useEffect(() => setTitle(p.title), [p.id, p.title]);
  const details = p.details;
  const unitName = (id: string) => p.units.find(u => u.id === id)?.title || id;
  const lessonNumber = (id: string) => p.lessons.find(l => l.id === id)?.number || id;
  return <aside className="project-details" aria-label="项目详情">
    <div className="detail-header"><button className="detail-close" onClick={onClose} aria-label="关闭项目详情">×</button><label htmlFor="project-title">项目名称</label><div className="detail-name"><input id="project-title" maxLength={200} value={title} disabled={saving} onChange={e => setTitle(e.target.value)} /><button disabled={saving || !title.trim() || title === p.title} onClick={() => void onRename(title)}>保存</button></div><div className="detail-actions"><button onClick={onGraph}>跳转到图谱页</button><button className="danger" disabled={saving} onClick={onDelete}>删除</button></div></div>
    <div className="detail-body"><h2>基础信息</h2><dl><dt>项目 ID</dt><dd>{p.id}</dd><dt>项目状态</dt><dd>{p.status === 'completed' ? '已完成' : p.status === 'main_completed' ? '主线已完成 · 支线待学习' : '进行中'}</dd><dt>创建时间</dt><dd>{details.createdAt || '未知'}</dd><dt>最后一次学习时间</dt><dd>{details.lastLearningAt || '尚未学习'}</dd><dt>项目目录</dt><dd className="detail-path">{details.directory}</dd><dt>学习材料</dt><dd>{details.materials.map(m => m.title).join('、') || '无独立材料记录'}</dd></dl>
      <h2>当前进度</h2><p className="detail-phase">{phaseNames[p.state] || '等待导师流程继续'}</p><p>{details.currentLesson ? `当前课程：${details.currentLesson.number} ${details.currentLesson.title}` : details.kind === 'navigation' ? '课程尚未规划' : '当前没有进行中的课程'}</p><p>下一步：{nextActionNames[p.state] || '继续交互式导师流程'}</p>
      <ProgressSummary label="课程" value={details.statistics.lessons} /><ProgressSummary label="知识点" value={details.statistics.units} /><ProgressSummary label="主线课程" value={details.statistics.main} /><ProgressSummary label="支线课程" value={details.statistics.branch} /><p>有评分证据的平均掌握度：{details.statistics.mastery === null ? '—' : `${Math.round(details.statistics.mastery * 100)}%`}</p>
      <details><summary>知识点与前置关系（{p.units.length}）</summary><div className="detail-table"><table><thead><tr><th>知识点</th><th>前置知识点</th><th>状态</th><th>讲解课程</th></tr></thead><tbody>{p.units.map(u => <tr key={u.id}><td>{u.title}</td><td>{p.unitEdges.filter(e => e.successor_id === u.id).map(e => unitName(e.predecessor_id)).join('、') || '无'}</td><td>{statusName(u.status)}</td><td>{p.lessons.filter(l => l.teaches?.includes(u.id)).map(l => l.number).join('、') || '尚未规划'}</td></tr>)}</tbody></table></div></details>
      <details><summary>课程与前置关系（{p.lessons.length}）</summary>{!p.lessons.length ? <p>尚未规划课程。</p> : <div className="detail-table"><table><thead><tr><th>编号 / 标题</th><th>安排</th><th>前置课程</th><th>状态</th></tr></thead><tbody>{p.lessons.map(l => <tr key={l.id}><td>{l.number} {l.title}</td><td>{trackName(l.track)}</td><td>{p.lessonEdges.filter(e => e.successor_id === l.id).map(e => lessonNumber(e.predecessor_id)).join('、') || '无'}</td><td>{statusName(l.status)}</td></tr>)}</tbody></table></div>}</details>
      <details><summary>材料覆盖（{details.coverage.length}）</summary>{!details.coverage.length ? <p>暂无材料覆盖记录。</p> : <div className="detail-table"><table><thead><tr><th>要点</th><th>安排</th><th>知识点</th><th>课程 / 教学块 / 覆盖</th></tr></thead><tbody>{details.coverage.map(c => <tr key={c.point_id}><td>{c.summary}</td><td>{trackName(c.track)}</td><td>{c.unit_ids.map(unitName).join('、')}</td><td>{c.lessons.map(l => `${l.number} · ${l.block_ids.join('、') || '待发布'} · ${l.degree}`).join('；') || '尚未规划课程'}</td></tr>)}</tbody></table></div>}</details>
    </div>
  </aside>;
}

export default function App() {
  const [page, setPage] = useState<'projects' | 'graph' | 'settings'>('projects');
  const [items, setItems] = useState<ProjectSummary[]>([]); const [project, setProject] = useState<Project | null>(null);
  const [selection, select] = useState<Selection>(null); const [config, setConfig] = useState(defaults); const [error, setError] = useState('');
  const current = useRef(project); current.current = project;
  const requestId = useRef(0); const busy = useRef(false);
  const [split, setSplit] = useState(50);
  const splitContainer = useRef<HTMLDivElement>(null);
  const [splitState, setSplitState] = useState<'idle' | 'dragging'>('idle');
  const [showDetails, setShowDetails] = useState(false);
  const [archives, setArchives] = useState<Archive[]>([]);
  const [managing, setManaging] = useState(false);
  const [archiveView, setArchiveView] = useState(false);
  const [canRetryManagement, setCanRetryManagement] = useState(false);
  const lastManagement = useRef<{ id: string; revision: string; operation: 'rename' | 'archive' | 'restore' | 'open-directory'; title?: string; actionId: string } | null>(null);
  const splitPointer = useRef<number | null>(null);
  const splitOffset = useRef(0);
  const adjustSplit = (value: number) => setSplit(Math.min(75, Math.max(25, value)));
  const moveSplit = (clientX: number) => {
    const bounds = splitContainer.current?.getBoundingClientRect();
    if (bounds && bounds.width > 10) adjustSplit((clientX - bounds.left - splitOffset.current) / (bounds.width - 10) * 100);
  };
  const refresh = useCallback(async () => {
    if (busy.current) return; busy.current = true;
    const selected = current.current; const token = requestId.current;
    try {
      const listed = await listProjects(); setItems(listed);
      setArchives(await listArchives());
      if (selected && !listed.some(i => i.id === selected.id)) { if (token === requestId.current) { setProject(null); setShowDetails(false); setPage(p => p === 'graph' ? 'projects' : p); } }
      else if (selected) { const nextVersion = await version(selected.id); if (token === requestId.current && nextVersion.revision !== selected.revision) { const next = await readProject(selected.id); if (token === requestId.current) { setProject(next); select(s => s && !(s.kind === 'lesson' ? next.lessons : next.units).some(n => n.id === s.id) ? null : s); } } }
      setError('');
    } catch (e) { setError(String(e)); } finally { busy.current = false; }
  }, []);
  useEffect(() => { void refresh(); uiConfig().then(setConfig).catch(e => setError(String(e))); }, [refresh]);
  useEffect(() => { const tick = () => { if (!document.hidden) void refresh(); }; const timer = window.setInterval(tick, Math.max(1, config.poll_interval_seconds) * 1000); window.addEventListener('focus', tick); return () => { clearInterval(timer); window.removeEventListener('focus', tick); }; }, [refresh, config.poll_interval_seconds]);
  const open = async (id: string, graph = false) => { const token = ++requestId.current; try { const p = await readProject(id); if (token === requestId.current) { setProject(p); select(null); setPage(graph ? 'graph' : 'projects'); setShowDetails(!graph); setError(''); } } catch (e) { setError(String(e)); } };
  const runManagement = async (request: NonNullable<typeof lastManagement.current>) => {
    lastManagement.current = request; setManaging(true); setCanRetryManagement(false);
    try { await manageProject(request.id, request.revision, request.operation, request.title, request.actionId); if (request.operation === 'archive' && current.current?.id === request.id) { ++requestId.current; current.current = null; setProject(null); setShowDetails(false); select(null); } else if (request.operation === 'rename' && current.current?.id === request.id) { const next = await readProject(request.id); current.current = next; setProject(next); } lastManagement.current = null; await refresh(); setError(''); }
    catch (e) { setError(String(e)); setCanRetryManagement(!String(e).includes('版本冲突')); } finally { setManaging(false); }
  };
  const manage = async (id: string, operation: 'rename' | 'archive' | 'open-directory', title?: string) => {
    if (managing) return;
    if (operation === 'archive' && !window.confirm('将此项目移入可恢复归档？原始材料和共享导航会保留。')) return;
    try { const p = current.current?.id === id ? current.current : await readProject(id); await runManagement({ id, revision: p.revision, operation, title, actionId: crypto.randomUUID() }); }
    catch (e) { setError(String(e)); }
  };
  const restoreArchive = async (item: Archive) => { if (!managing) await runManagement({ id: item.id, revision: item.revision, operation: 'restore', actionId: crypto.randomUUID() }); };
  const toggle = async (kind: Kind, id: string, skip: boolean) => { const p = current.current; if (!p) return; try { const next = await action(p, kind, id, skip ? 'skip' : 'restore'); if (current.current?.id === p.id) setProject(next); setError(''); } catch (e) { setError(String(e)); void refresh(); } };
  const cards = (status: string) => items.filter(i => status === 'in_progress' ? i.status !== 'completed' : i.status === status).map(i => <article className={`project-card ${showDetails && project?.id === i.id ? 'selected' : ''}`} key={i.id} onClick={() => void open(i.id)}><button className="card-body" aria-label={`查看 ${i.title} 详情`} onClick={e => { e.stopPropagation(); void open(i.id); }}><span>{i.status === 'completed' ? '✓ 已完成' : i.status === 'main_completed' ? '主线已完成 · 支线待学习' : '◷ 进行中'}</span><strong>{i.title}</strong><small>{i.lessonCount} 节课程 · {i.unitCount} 个知识点</small><time>创建时间：{i.createdAt?.replace('T', ' ') || '未知'}</time><time>最后一次学习时间：{i.lastLearningAt?.replace('T', ' ') || '尚未学习'}</time></button><div className="card-actions" onClick={e => e.stopPropagation()}><button onClick={() => void open(i.id)}>详情</button><button className="danger" disabled={managing} onClick={() => void manage(i.id, 'archive')}>删除</button><button className="wide" disabled={managing} onClick={() => void manage(i.id, 'open-directory')}>打开项目目录</button><button className="wide" onClick={() => void open(i.id, true)}>跳转到图谱页</button></div></article>);
  return <div className="app-shell"><nav className="page-rail" aria-label="页面导航"><div className="rail-brand">学<span>交互式学习</span></div><button className={page === 'projects' ? 'active' : ''} onClick={() => setPage('projects')} aria-label="项目页"><Icon kind="projects" /><span>项目页</span></button>{project && <button className={page === 'graph' ? 'active' : ''} onClick={() => setPage('graph')} aria-label="图谱页"><Icon kind="graph" /><span>图谱页</span></button>}<button className={`rail-settings ${page === 'settings' ? 'active' : ''}`} onClick={() => setPage('settings')} aria-label="设置"><Icon kind="settings" /><span>设置</span></button></nav>
    <main className="app-main"><header className="topbar"><span>{project?.title || '本地学习空间'}</span><small>点击节点查看上游路径与跨图关系 · 拖动分隔条调节宽度 · 右键屏蔽或恢复后续学习</small><button onClick={() => void refresh()}>刷新</button></header>{(error || canRetryManagement) && <div className="error-banner" role="alert">{error || '项目操作尚未完成'}{canRetryManagement && <button disabled={managing} onClick={() => { if (lastManagement.current) void runManagement(lastManagement.current); }}>重试操作</button>}</div>}
      {page === 'projects' && <div className={`projects-layout ${showDetails && project ? 'with-details' : ''}`}><div className="page-content"><span className="eyebrow">YOUR LEARNING SPACE</span><div className="projects-title"><h1>学习项目</h1><button onClick={() => setArchiveView(!archiveView)}>可恢复归档（{archives.length}）</button></div><p>点击项目查看详情，继续你的学习路线。</p>{archiveView && <section className="archive-list" aria-label="可恢复归档"><h2>可恢复归档</h2>{!archives.length && <p>暂无归档项目。</p>}{archives.map(a => <div key={a.id}><span><strong>{a.title}</strong><small>{a.kind === 'navigation' ? '学习导航' : '导师项目'} · {a.archivedAt}</small></span><button disabled={managing} onClick={() => void restoreArchive(a)}>恢复</button></div>)}</section>}<h2>进行中</h2><div className="project-grid">{cards('in_progress')}</div>{!items.some(i => i.status !== 'completed') && <p className="muted">暂无进行中的项目。先通过学习导航建立项目。</p>}<hr /><h2>已完成</h2><div className="project-grid">{cards('completed')}</div>{!items.some(i => i.status === 'completed') && <p className="muted">完成的学习项目会保留在这里。</p>}</div>{showDetails && project && <ProjectDetail project={project} onClose={() => setShowDetails(false)} onGraph={() => setPage('graph')} onDelete={() => void manage(project.id, 'archive')} onRename={title => manage(project.id, 'rename', title)} saving={managing} />}</div>}
      {page === 'settings' && <Settings />}
      {page === 'graph' && project && <div ref={splitContainer} className={`dual-graphs ${splitState}`} style={{ gridTemplateColumns: `minmax(0, ${split}fr) 10px minmax(0, ${100 - split}fr)` }}>
        <Graph kind="lesson" project={project} selection={selection} select={select} config={config} onAction={toggle} />
        <div className="graph-splitter" role="separator" aria-label="调节双图宽度" aria-orientation="vertical" aria-valuemin={25} aria-valuemax={75} aria-valuenow={Math.round(split)} tabIndex={0}
          onPointerDown={e => { if (e.button !== 0 || splitPointer.current !== null) return; e.preventDefault(); splitOffset.current = e.clientX - e.currentTarget.getBoundingClientRect().left; splitPointer.current = e.pointerId; setSplitState('dragging'); e.currentTarget.setPointerCapture(e.pointerId); }}
          onPointerMove={e => { if (splitPointer.current === e.pointerId) moveSplit(e.clientX); }}
          onPointerUp={e => { if (splitPointer.current !== e.pointerId) return; splitPointer.current = null; setSplitState('idle'); e.currentTarget.releasePointerCapture(e.pointerId); }}
          onPointerCancel={() => { splitPointer.current = null; setSplitState('idle'); }} onLostPointerCapture={() => { splitPointer.current = null; setSplitState('idle'); }}
          onDoubleClick={() => adjustSplit(50)}
          onKeyDown={e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); adjustSplit(split + (e.key === 'ArrowLeft' ? -2 : 2)); } }} />
        <Graph kind="unit" project={project} selection={selection} select={select} config={config} onAction={toggle} />
      </div>}
    </main></div>;
}
