import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import { deleteInvalidProject, deleteProject, extractionStatus, health, listProjects, readProject, saveProject, saveProjectOrder, startExtraction, timestamp } from './api';
import { assertTransition, codepoints, membership, normalizeText, paint, reassign, sampleHidden, sourceHash, spanIndices, type Difficulty, type MemorySpan, type Mode, type Phase, type ProjectRecord, type RecentProject } from './model';

type Selection = { start: number; end: number };
type Glyph = { value: string; start: number; end: number };
const phaseName: Record<Phase, string> = { recent_projects: '项目列表', creating_project: '创建项目', extracting_spans: '正在提取', reviewing_extraction: '核对结果', editor_editing: '编辑中', editor_practice: '练习中', opening_project: '打开项目', saving: '保存中', conflict_pending: '版本冲突', error: '错误' };
const extractionStageName: Record<string, string> = { validating_text: '校验原文', skill_start: '生成候选要点', awaiting_codex_selection: 'Codex 正在选择要点', skill_resume: '校验语义选择', verifying_result: '验证提取结果', imported: '提取完成' };

function glyphs(text: string): Glyph[] {
  const parts = [...new Intl.Segmenter('zh-CN', { granularity: 'grapheme' }).segment(text)];
  let offset = 0;
  return parts.map(part => {
    const start = offset;
    offset += codepoints(part.segment).length;
    return { value: part.segment, start, end: offset };
  });
}

function groupedLines(text: string): Glyph[][] {
  const lines: Glyph[][] = [[]];
  for (const glyph of glyphs(text)) {
    if (glyph.value === '\n') lines.push([]);
    else lines[lines.length - 1].push(glyph);
  }
  return lines;
}

function randomWidths(spans: MemorySpan[], text: string): Record<string, number> {
  const owners = membership(spans, codepoints(text).length);
  const widths: Record<string, number> = {};
  for (const glyph of glyphs(text)) {
    const owner = owners[glyph.start];
    if (owner && !/^\s+$/.test(glyph.value)) widths[`${owner}:${glyph.start}`] = 52 + Math.floor(Math.random() * 125);
  }
  return widths;
}

function Dialog({ children, onClose }: { children: React.ReactNode; onClose?: () => void }) {
  return <div className="dialog-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose?.(); }}><div className="dialog" role="dialog" aria-modal="true">{children}</div></div>;
}

export default function App() {
  const [phase, setPhase] = useState<Phase>('recent_projects');
  const phaseRef = useRef<Phase>('recent_projects');
  const move = useCallback((next: Phase) => { assertTransition(phaseRef.current, next); phaseRef.current = next; setPhase(next); }, []);
  const [recent, setRecent] = useState<RecentProject[]>([]);
  const [listAction, setListAction] = useState<'idle' | 'renaming' | 'deleting'>('idle');
  const [orderDragState, setOrderDragState] = useState<'idle' | 'dragging' | 'persisting' | 'error'>('idle');
  const draggedProjectId = useRef<string | null>(null);
  const [record, setRecord] = useState<ProjectRecord | null>(null);
  const recordRef = useRef<ProjectRecord | null>(null);
  const [mode, setMode] = useState<Mode>('edit');
  const [selection, setSelection] = useState<Selection | null>(null);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [notice, setNotice] = useState('');
  const [newDialog, setNewDialog] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftText, setDraftText] = useState('');
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [ratio, setRatio] = useState(50);
  const [difficulty, setDifficulty] = useState<Difficulty>('easy');
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [widths, setWidths] = useState<Record<string, number>>({});
  const [extractError, setExtractError] = useState('');
  const [extractionStage, setExtractionStage] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState('');
  const strokeRef = useRef<{ action: 'add' | 'remove'; targetId?: string; touched: Set<number> } | null>(null);
  const creatingRef = useRef(false);
  const selectingRef = useRef<number | null>(null);
  const history = useRef<ProjectRecord[]>([]);
  const future = useRef<ProjectRecord[]>([]);

  const message = useCallback((value: string) => { setNotice(value); window.setTimeout(() => setNotice(current => current === value ? '' : current), 4500); }, []);
  const refresh = useCallback(async () => setRecent(await listProjects()), []);
  useEffect(() => { void health().then(value => setWorkspace(value.workspace)).then(refresh).catch(error => message((error as Error).message)); }, [refresh, message]);
  useEffect(() => { recordRef.current = record; }, [record]);
  useEffect(() => { dirtyRef.current = dirty; }, [dirty]);
  useEffect(() => {
    const finish = () => { strokeRef.current = null; selectingRef.current = null; };
    window.addEventListener('pointerup', finish);
    window.addEventListener('blur', finish);
    return () => { window.removeEventListener('pointerup', finish); window.removeEventListener('blur', finish); };
  }, []);

  const updateRecord = useCallback((change: (current: ProjectRecord) => ProjectRecord, recordHistory = true) => {
    const previous = recordRef.current;
    if (!previous || !['editor_editing', 'editor_practice', 'reviewing_extraction'].includes(phaseRef.current)) return;
    const next = change(previous);
    if (JSON.stringify(next) === JSON.stringify(previous)) return;
    if (recordHistory) {
      history.current.push(structuredClone(previous));
      if (history.current.length > 100) history.current.shift();
    }
    future.current = [];
    dirtyRef.current = true;
    recordRef.current = next;
    setRecord(next);
    setDirty(true);
  }, []);

  const save = useCallback(async (): Promise<boolean> => {
    const current = recordRef.current;
    if (!current || !['editor_editing', 'editor_practice', 'reviewing_extraction'].includes(phaseRef.current)) return false;
    const oldPhase = phaseRef.current;
    move('saving');
    try {
      const saved = await saveProject(current.project, current.text);
      setRecord(saved);
      recordRef.current = saved;
      setDirty(false);
      dirtyRef.current = false;
      history.current = [];
      future.current = [];
      move(mode === 'practice' ? 'editor_practice' : 'editor_editing');
      void refresh().catch(error => message(`方案已保存，但列表刷新失败：${(error as Error).message}`));
      message('方案已保存到项目目录');
      return true;
    } catch (error) {
      const text = (error as Error).message;
      if (text.includes('PROJECT_REVISION_CONFLICT')) move('conflict_pending');
      else { move(oldPhase === 'reviewing_extraction' ? 'reviewing_extraction' : mode === 'practice' ? 'editor_practice' : 'editor_editing'); message(text); }
      return false;
    }
  }, [move, mode, refresh, message]);

  const undo = useCallback(() => {
    if (!['editor_editing', 'editor_practice'].includes(phaseRef.current)) return;
    const previous = history.current.pop(), current = recordRef.current;
    if (!previous || !current) return;
    future.current.push(structuredClone(current));
    recordRef.current = previous;
    setRecord(previous);
    setDirty(true);
    dirtyRef.current = true;
  }, []);
  const redo = useCallback(() => {
    if (!['editor_editing', 'editor_practice'].includes(phaseRef.current)) return;
    const next = future.current.pop(), current = recordRef.current;
    if (!next || !current) return;
    history.current.push(structuredClone(current));
    recordRef.current = next;
    setRecord(next);
    setDirty(true);
    dirtyRef.current = true;
  }, []);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      const target = event.target as HTMLElement;
      if ((target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') && event.key.toLowerCase() !== 's') return;
      if (event.key.toLowerCase() === 's') { event.preventDefault(); void save(); }
      if (event.key.toLowerCase() === 'z') { event.preventDefault(); undo(); }
      if (event.key.toLowerCase() === 'y') { event.preventDefault(); redo(); }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [save, undo, redo]);

  const current = record?.project;
  const controlsBusy = ['saving', 'extracting_spans', 'opening_project', 'conflict_pending'].includes(phase) || listAction !== 'idle' || orderDragState === 'persisting';
  const workspaceName = workspace.split(/[\\/]/).filter(Boolean).pop() || '';
  const chars = useMemo(() => codepoints(record?.text || ''), [record?.text]);
  const lines = useMemo(() => groupedLines(record?.text || ''), [record?.text]);
  const owners = useMemo(() => membership(current?.memorySpans || [], chars.length), [current?.memorySpans, chars.length]);
  const spanById = useMemo(() => new Map((current?.memorySpans || []).map(span => [span.id, span])), [current?.memorySpans]);
  const selectedOwner = selection && owners[selection.start];
  const selectedSpan = selectedOwner ? spanById.get(selectedOwner) : undefined;
  const selectedIndices = selection ? Array.from({ length: selection.end - selection.start }, (_, i) => selection.start + i).filter(i => chars[i] && !/\s/.test(chars[i])) : [];

  const beginNew = (force = false) => {
    if (['saving', 'extracting_spans', 'opening_project', 'conflict_pending'].includes(phaseRef.current)) return;
    if (dirtyRef.current && !force) { setPendingAction('new'); return; }
    setDraftTitle(''); setDraftText(''); setNewDialog(true);
    if (phaseRef.current === 'recent_projects' || phaseRef.current.startsWith('editor_')) move('creating_project');
  };
  const open = async (id: string, force = false) => {
    if (['saving', 'extracting_spans', 'opening_project', 'conflict_pending'].includes(phaseRef.current)) return;
    if (recordRef.current?.project.projectId === id) return;
    if (dirtyRef.current && !force) { setPendingAction(id); return; }
    const previousPhase = phaseRef.current;
    move('opening_project');
    try {
      const loaded = await readProject(id);
      setRecord(loaded); recordRef.current = loaded;
      setSelection(null); setMode('edit'); setHidden(new Set()); setDirty(false); dirtyRef.current = false;
      history.current = []; future.current = [];
      move('editor_editing');
    } catch (error) {
      message((error as Error).message);
      move(previousPhase === 'recent_projects' ? 'recent_projects' : previousPhase === 'editor_practice' ? 'editor_practice' : 'editor_editing');
    }
  };
  const proceedPending = async (choice: 'save' | 'discard' | 'cancel') => {
    const target = pendingAction;
    setPendingAction(null);
    if (!target || choice === 'cancel') return;
    if (choice === 'save' && !(await save())) return;
    if (target === 'new') beginNew(true); else await open(target, true);
  };

  const pollExtraction = async (id: string) => {
    for (let attempt = 0; attempt < 240; attempt++) {
      await new Promise(resolve => window.setTimeout(resolve, 1500));
      const status = await extractionStatus(id);
      setExtractionStage(status.stage);
      if (status.status === 'completed') {
        const previous = recordRef.current;
        if (!previous) throw new Error('提取结果没有可更新的项目');
        const next = { ...previous, project: { ...previous.project, memorySpans: status.spans || [], extraction: status.extraction || null } };
        recordRef.current = next;
        setRecord(next);
        setExtractError(''); setJobId(null); setExtractionStage(''); setDirty(true); dirtyRef.current = true;
        move('reviewing_extraction'); move('editor_editing');
        message(`已提取 ${status.spans?.length || 0} 个记忆要点，请检查后保存`);
        return;
      }
      if (status.status === 'failed') throw new Error(status.error || '提取失败');
    }
    throw new Error('提取超时，请重试');
  };
  const extract = async (text: string) => {
    move('extracting_spans'); setExtractError(''); setExtractionStage('validating_text');
    try {
      const job = await startExtraction(text);
      setJobId(job.jobId);
      await pollExtraction(job.jobId);
    } catch (error) {
      const value = (error as Error).message;
      setExtractError(value); setJobId(null); setExtractionStage(''); move('editor_editing'); message(`自动提取失败：${value}；可以手工标记`);
    }
  };
  const create = async () => {
    if (creatingRef.current || phaseRef.current !== 'creating_project') return;
    const text = normalizeText(draftText);
    if (!text.trim()) { message('请粘贴要背诵的纯文本'); return; }
    if (text.length > 100000) { message('原文不可超过 100000 字符'); return; }
    creatingRef.current = true;
    try {
      const stamp = (await timestamp()).timestamp;
      const project = { schemaVersion: 1 as const, projectId: crypto.randomUUID(), title: draftTitle.trim() || text.trim().slice(0, 28), revision: 0,
        lastWriterId: crypto.randomUUID(), createdAt: stamp, updatedAt: stamp, lastOpenedAt: stamp,
        source: { path: 'source.txt' as const, sha256: await sourceHash(text) }, extraction: null, memorySpans: [] as MemorySpan[] };
      const next = { project, text };
      setRecord(next); recordRef.current = next; setNewDialog(false); setSelection(null); setMode('edit'); setHidden(new Set());
      setDirty(true); dirtyRef.current = true; history.current = []; future.current = [];
      void extract(text);
    } catch (error) { message((error as Error).message); }
    finally { creatingRef.current = false; }
  };
  const editSpans = (change: (spans: MemorySpan[]) => MemorySpan[], recordHistory = true) => updateRecord(previous => ({ ...previous, project: { ...previous.project, memorySpans: change(previous.project.memorySpans) } }), recordHistory);
  const glyphIndices = (glyph: Glyph) => Array.from({ length: glyph.end - glyph.start }, (_, i) => glyph.start + i);
  const onGlyphDown = (event: ReactPointerEvent<HTMLElement>, glyph: Glyph) => {
    if (mode !== 'edit' || phaseRef.current !== 'editor_editing') return;
    if (event.button === 2) {
      event.preventDefault();
      const action = owners[glyph.start] ? 'remove' : 'add';
      const before = current?.memorySpans || [];
      const after = paint(before, glyphIndices(glyph), action, record?.text || '');
      const targetId = action === 'add' && after.length > before.length ? after[after.length - 1].id : undefined;
      strokeRef.current = { action, targetId, touched: new Set(glyphIndices(glyph)) };
      editSpans(() => after);
      setSelection({ start: glyph.start, end: glyph.end });
    } else if (event.button === 0) {
      selectingRef.current = glyph.start;
      setSelection({ start: glyph.start, end: glyph.end });
    }
  };
  const onGlyphEnter = (glyph: Glyph) => {
    if (mode !== 'edit' || phaseRef.current !== 'editor_editing') return;
    const stroke = strokeRef.current;
    if (stroke) {
      const indices = glyphIndices(glyph).filter(i => !stroke.touched.has(i));
      if (!indices.length) return;
      indices.forEach(i => stroke.touched.add(i));
      editSpans(spans => paint(spans, indices, stroke.action, record?.text || '', stroke.targetId), false);
    } else if (selectingRef.current !== null) {
      const anchor = selectingRef.current;
      setSelection({ start: Math.min(anchor, glyph.start), end: Math.max(anchor + 1, glyph.end) });
    }
  };
  const toggle = (id: string) => setHidden(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const startPractice = () => {
    if (!current) return;
    setHidden(sampleHidden(current.memorySpans, ratio));
    setWidths(randomWidths(current.memorySpans, record?.text || ''));
    message('练习已开始，点击遮挡可查看答案');
  };
  const switchMode = (next: Mode) => {
    if (!record || next === mode || !['editor_editing', 'editor_practice'].includes(phaseRef.current)) return;
    setMode(next); setSelection(null); setHidden(new Set());
    if (next === 'practice') setWidths(randomWidths(record.project.memorySpans, record.text));
    move(next === 'edit' ? 'editor_editing' : 'editor_practice');
  };
  const renameProject = async (item: RecentProject) => {
    if (controlsBusy || item.error) return;
    if (current?.projectId === item.projectId && dirtyRef.current) { message('当前项目有未保存修改，请先保存再重命名'); return; }
    const title = window.prompt('项目新标题', item.title)?.trim();
    if (title === undefined || title === item.title) return;
    if (!title || title.length > 200) { message('标题必须为 1 至 200 个字符'); return; }
    setListAction('renaming');
    try {
      const loaded = await readProject(item.projectId);
      const saved = await saveProject({ ...loaded.project, title }, loaded.text);
      if (recordRef.current?.project.projectId === item.projectId) {
        recordRef.current = saved; setRecord(saved); setDirty(false); dirtyRef.current = false;
        history.current = []; future.current = [];
      }
      await refresh(); message('项目已重命名');
    } catch (error) { message((error as Error).message); }
    finally { setListAction('idle'); }
  };
  const removeProject = async (item: RecentProject) => {
    if (controlsBusy) return;
    const active = recordRef.current?.project.projectId === item.projectId;
    const extra = active && dirtyRef.current ? '当前未保存的修改也会丢失。' : '';
    if (!window.confirm(`确定删除“${item.title}”及其本地项目文件吗？${extra}`)) return;
    setListAction('deleting');
    try {
      if (item.error) await deleteInvalidProject(item.directoryName);
      else await deleteProject(item.projectId);
      if (active) {
        setRecord(null); recordRef.current = null; setSelection(null); setDirty(false); dirtyRef.current = false;
        setHidden(new Set()); history.current = []; future.current = []; move('recent_projects');
      }
      await refresh(); message('项目已删除');
    } catch (error) { message((error as Error).message); }
    finally { setListAction('idle'); }
  };
  const reorderProjects = async (targetId: string) => {
    const sourceId = draggedProjectId.current;
    if (!sourceId || sourceId === targetId) return;
    const next = [...recent];
    const from = next.findIndex(item => item.projectId === sourceId);
    const to = next.findIndex(item => item.projectId === targetId);
    if (from < 0 || to < 0) return;
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setRecent(next); setOrderDragState('persisting');
    try {
      await saveProjectOrder(next.filter(item => item.projectId).map(item => item.projectId));
      setOrderDragState('idle');
    } catch (error) {
      setOrderDragState('error'); setRecent(recent); message((error as Error).message);
      try { await refresh(); } catch { /* Keep the last confirmed local order. */ }
      finally { setOrderDragState('idle'); }
    }
  };

  const renderLine = (line: Glyph[], lineIndex: number) => {
    const nodes: React.ReactNode[] = [];
    for (let position = 0; position < line.length;) {
      const glyph = line[position];
      const owner = owners[glyph.start];
      const span = owner ? spanById.get(owner) : undefined;
      const concealed = mode === 'practice' && !!owner && hidden.has(owner);
      if (concealed && difficulty !== 'easy' && span && !/^\s+$/.test(glyph.value)) {
        let end = position + 1;
        while (end < line.length && owners[line[end].start] === owner && line[end].start === line[end - 1].end && !/^\s+$/.test(line[end].value)) end++;
        const count = end - position;
        const width = difficulty === 'medium' ? Math.max(25, count * 24) : widths[`${owner}:${glyph.start}`] || 90;
        nodes.push(<span key={glyph.start} role="button" tabIndex={0} aria-label="显示记忆要点答案" className="mask-rectangle" style={{ '--span-color': span.color, width } as CSSProperties} onClick={() => toggle(owner)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(owner); } }} />);
        position = end;
        continue;
      }
      if (/^\s+$/.test(glyph.value)) {
        nodes.push(<span key={glyph.start} className="text-space">{glyph.value === '\t' ? '　' : glyph.value}</span>);
        position++;
        continue;
      }
      const selected = selection && glyph.start < selection.end && glyph.end > selection.start;
      nodes.push(<span key={glyph.start} role="button" tabIndex={mode === 'edit' ? 0 : owner ? 0 : -1}
        aria-label={concealed ? '显示记忆要点答案' : glyph.value}
        className={`glyph ${span ? 'marked' : ''} ${selected && mode === 'edit' ? 'selected' : ''} ${concealed ? 'concealed' : ''}`}
        style={{ '--span-color': span?.color || '#cf725d' } as CSSProperties}
        onPointerDown={event => onGlyphDown(event, glyph)} onPointerEnter={() => onGlyphEnter(glyph)}
        onClick={() => { if (mode === 'practice' && owner) toggle(owner); }}
        onKeyDown={event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          if (mode === 'practice' && owner) toggle(owner);
          else if (mode === 'edit') setSelection({ start: glyph.start, end: glyph.end });
        }}><span className={concealed ? 'hidden-letter' : undefined}>{glyph.value}</span></span>);
      position++;
    }
    return <div className="text-line" key={lineIndex}>{nodes.length ? nodes : <br />}</div>;
  };

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><div className="brand-icon">記</div><div><div className="eyebrow">RECALL STUDIO</div><strong>背书工具</strong></div></div>
      <div className="title-area"><label htmlFor="project-title">项目标题</label><input id="project-title" value={current?.title || ''} disabled={controlsBusy || !current || !['editor_editing', 'editor_practice'].includes(phase)} placeholder="选择或新建项目" onChange={event => updateRecord(previous => ({ ...previous, project: { ...previous.project, title: event.target.value } }))} /><span className={`save-indicator ${dirty ? 'dirty' : ''}`}>{phase === 'saving' ? '保存中' : dirty ? '未保存' : current ? '已保存' : ''}</span></div>
      <div className="top-actions"><span className="phase-pill">{phaseName[phase]}</span><button className="button primary" disabled={controlsBusy || !current || !['editor_editing', 'editor_practice'].includes(phase)} onClick={() => void save()}>保存方案 <kbd>Ctrl S</kbd></button></div>
    </header>
    <main className="workspace">
      <aside className="sidebar left-sidebar">
        <div className="sidebar-head"><div><span className="eyebrow">LIBRARY</span><h2>项目列表</h2></div><button className="round-add" title="新建项目" disabled={controlsBusy} onClick={() => beginNew()}>＋</button></div>
        <button className="new-project" disabled={controlsBusy} onClick={() => beginNew()}>＋ 新建背诵项目</button>
        <div className="sidebar-caption">项目保存在 outputs/背书工具/</div>
        <div className="recent-list">{recent.length ? recent.map(item =>
          <div key={item.directoryName} data-project-id={item.projectId || undefined} className={`project-card ${current?.projectId === item.projectId ? 'active' : ''} ${item.error ? 'invalid' : ''}`}
            title={item.error || undefined}>
            <button className="project-open" disabled={controlsBusy || !!item.error} onClick={() => void open(item.projectId)} aria-label={`打开${item.title}`}>
            <span className="project-avatar">{item.error ? '!' : item.title.slice(0, 1)}</span>
            <span className="project-meta"><strong>{item.title}</strong><small>{item.error ? `项目文件无效：${item.error}` : `${item.spanCount} 个要点 · ${item.updatedAt.slice(5, 16).replace('T', ' ')}`}</small></span>
            </button>
            <div className="project-actions"><button disabled={controlsBusy || !!item.error} title={item.error ? '项目文件无效，无法重命名' : undefined} onClick={() => void renameProject(item)}>重命名</button><button disabled={controlsBusy} onClick={() => void removeProject(item)}>删除</button></div>
            <span className="project-drag-handle" role="button" tabIndex={item.projectId && !controlsBusy ? 0 : -1} title="按住拖动排序；也可用上下方向键" aria-label={`调整${item.title}的顺序`}
              onPointerDown={event => { if (event.button !== 0 || controlsBusy || !item.projectId) return; event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); draggedProjectId.current = item.projectId; setOrderDragState('dragging'); }}
              onPointerUp={event => { if (draggedProjectId.current !== item.projectId) return; const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-project-id]')?.dataset.projectId; if (target) void reorderProjects(target); else setOrderDragState('idle'); draggedProjectId.current = null; }}
              onPointerCancel={() => { draggedProjectId.current = null; setOrderDragState('idle'); }}
              onKeyDown={event => { if (controlsBusy || !item.projectId || !['ArrowUp', 'ArrowDown'].includes(event.key)) return; event.preventDefault(); const index = recent.findIndex(value => value.projectId === item.projectId); const target = recent[index + (event.key === 'ArrowUp' ? -1 : 1)]; if (!target?.projectId) return; draggedProjectId.current = item.projectId; void reorderProjects(target.projectId); draggedProjectId.current = null; }}>⋮⋮</span>
          </div>) : <div className="empty-list">还没有项目。<br />从一段需要背诵的文字开始。</div>}</div>
        <div className="sidebar-foot"><i />本地工作区<span>LOCAL</span></div>
      </aside>
      <section className="editor-column">
        <div className="editor-toolbar"><div className="mode-tabs"><button className={mode === 'edit' ? 'active' : ''} onClick={() => switchMode('edit')} disabled={!record}>编辑模式</button><button className={mode === 'practice' ? 'active' : ''} onClick={() => switchMode('practice')} disabled={!record}>练习模式</button></div>{mode === 'practice' && record ? <div className="practice-controls"><button className="button subtle" onClick={() => setHidden(new Set())}>显示所有答案</button><button className="button subtle" onClick={() => setHidden(new Set(current?.memorySpans.map(span => span.id)))}>隐藏所有答案</button><label>难度<select value={difficulty} onChange={event => setDifficulty(event.target.value as Difficulty)}><option value="easy">简单</option><option value="medium">中等</option><option value="hard">困难</option></select></label><label className="ratio-control">隐藏比例<input type="range" min="0" max="100" value={ratio} onChange={event => setRatio(Number(event.target.value))} /><span className="ratio-number"><input aria-label="隐藏比例百分比" type="number" min="0" max="100" value={ratio} onChange={event => setRatio(Math.max(0, Math.min(100, Number(event.target.value) || 0)))} />%</span></label><button className="button primary" onClick={startPractice}>开始练习</button></div> : <div className="edit-help">左键选字 · 右键标记 · 右键拖动批量修改</div>}</div>
        <div className="stage"><div className="paper">{record ? <><div className="paper-top"><span className="eyebrow">MEMORY SHEET / {String(current?.memorySpans.length || 0).padStart(2, '0')} SPANS</span><span>{mode === 'edit' ? '编辑方案' : '主动回忆'}</span></div><h1>{current?.title}</h1><div className="paper-rule" />{extractError && <div className="error-banner">自动提取失败：{extractError}<button onClick={() => void extract(record.text)}>重试提取</button></div>}{jobId && <div className="loading-banner"><span className="spinner" />{extractionStageName[extractionStage] || '正在提取记忆要点'}…</div>}<div className="text-body" onContextMenu={event => event.preventDefault()}>{lines.map(renderLine)}</div><div className="paper-end">— END OF MATERIAL —</div></> : <div className="empty-stage"><div className="empty-symbol">「　」</div><span className="eyebrow">BEGIN WITH A PASSAGE</span><h1>把想记住的内容<br />变成可练习的文字。</h1><p>粘贴材料，自动提取要点，再用自己的节奏反复背诵。</p><button className="button primary" onClick={() => beginNew()}>新建项目</button></div>}</div></div>
        <div className="editor-footer"><span>{record ? `${chars.length} 字符 · ${current?.memorySpans.length || 0} 个记忆要点` : '等待项目'}</span><span className="footer-spacer" /><span>Ctrl+S 保存 · Ctrl+Z 撤销 · Ctrl+Y 重做</span></div>
      </section>
      <aside className="sidebar right-sidebar"><div className="sidebar-head"><div><span className="eyebrow">INSPECTOR</span><h2>属性</h2></div><span className="inspector-badge">{selectedSpan ? '要点' : selection ? '文字' : '项目'}</span></div>{record && mode === 'edit' && selection ? <div className="inspector"><div className="section"><span className="section-kicker">当前选择</span><div className="selection-preview">{chars.slice(selection.start, selection.end).join('')}</div><small>位置 {selection.start}–{selection.end} · {selectedIndices.length} 字符</small></div>{selectedSpan ? <><div className="section"><span className="section-kicker">记忆要点</span><div className="span-id">要点 ID：{selectedSpan.id.slice(0, 8)}</div><div className="segment-list">{selectedSpan.segments.map(segment => <span key={segment.start}>{chars.slice(segment.start, segment.end).join('')}</span>)}</div><label className="field-label">圆形和占位颜色<input type="color" value={selectedSpan.color} onChange={event => editSpans(spans => spans.map(span => span.id === selectedSpan.id ? { ...span, color: event.target.value } : span))} /></label><label className="field-label">要点说明<input value={selectedSpan.role} onChange={event => editSpans(spans => spans.map(span => span.id === selectedSpan.id ? { ...span, role: event.target.value } : span))} /></label></div><div className="section"><span className="section-kicker">分组</span><p>可把所选文字移入已有要点。相隔的文字也能联动显隐。</p><select className="property-select" defaultValue="" key={selectedSpan.id + selection.start} onChange={event => { if (event.target.value) editSpans(spans => reassign(spans, selectedIndices, event.target.value)); }}><option value="">选择目标要点…</option>{current?.memorySpans.map(span => <option key={span.id} value={span.id}>{span.id === selectedSpan.id ? '【当前要点】' : ''}{span.segments.map(segment => chars.slice(segment.start, segment.end).join('')).join(' / ').slice(0, 32)}</option>)}</select><button className="button outline full" onClick={() => editSpans(spans => paint(spans, selectedIndices, 'remove', record.text))}>从要点中移除所选文字</button></div></> : <div className="section"><span className="section-kicker">未标记文字</span><p>右键点击或拖动可创建记忆要点，也可将所选文字加入已有要点。</p><button className="button primary full" onClick={() => editSpans(spans => paint(spans, selectedIndices, 'add', record.text))}>新建记忆要点</button><select className="property-select" defaultValue="" onChange={event => { if (event.target.value) editSpans(spans => reassign(spans, selectedIndices, event.target.value)); }}><option value="">加入已有要点…</option>{current?.memorySpans.map(span => <option key={span.id} value={span.id}>{span.segments.map(segment => chars.slice(segment.start, segment.end).join('')).join(' / ').slice(0, 32)}</option>)}</select></div>}</div> : <div className="inspector"><div className="section"><span className="section-kicker">项目概览</span>{record ? <><div className="stat"><strong>{current?.memorySpans.length || 0}</strong><span>记忆要点</span></div><div className="stat"><strong>{chars.length}</strong><span>原文字符</span></div><p>原文固定。编辑模式中可增删要点、调整分组和颜色。</p>{current?.directoryName && <button className="button danger full" onClick={() => { const item = recent.find(value => value.projectId === current.projectId); if (item) void removeProject(item); }}>删除项目</button>}</> : <p>选择左侧项目，或新建一个背诵项目。</p>}</div>{workspaceName && <div className="workspace-path">{workspaceName}</div>}</div>}</aside>
    </main>
    {newDialog && <Dialog onClose={() => { setNewDialog(false); move(record ? mode === 'edit' ? 'editor_editing' : 'editor_practice' : 'recent_projects'); }}><span className="eyebrow">NEW PROJECT</span><h2>新建背诵项目</h2><p>粘贴纯文本材料。创建后会自动调用 Codex 提取记忆要点。</p><label className="field-label">项目标题<input value={draftTitle} maxLength={200} onChange={event => setDraftTitle(event.target.value)} placeholder="留空时使用原文开头" /></label><label className="field-label">背诵材料<textarea value={draftText} onChange={event => setDraftText(event.target.value)} rows={9} placeholder="在这里粘贴需要背诵的纯文本…" /></label><div className="dialog-actions"><button className="button outline" onClick={() => { setNewDialog(false); move(record ? mode === 'edit' ? 'editor_editing' : 'editor_practice' : 'recent_projects'); }}>取消</button><button className="button primary" onClick={() => void create()}>创建并提取</button></div></Dialog>}
    {pendingAction && <Dialog><span className="eyebrow">UNSAVED CHANGES</span><h2>当前方案尚未保存</h2><p>切换项目之前，请选择如何处理未保存的修改。</p><div className="dialog-actions"><button className="button outline" onClick={() => void proceedPending('cancel')}>取消</button><button className="button danger" onClick={() => void proceedPending('discard')}>放弃修改</button><button className="button primary" onClick={() => void proceedPending('save')}>保存并继续</button></div></Dialog>}
    {phase === 'conflict_pending' && <Dialog><span className="eyebrow">REVISION CONFLICT</span><h2>项目版本已变化</h2><p>磁盘中的项目比当前页面更新。请保留当前内容，或重新打开磁盘版本。</p><div className="dialog-actions"><button className="button outline" onClick={() => { move(mode === 'practice' ? 'editor_practice' : 'editor_editing'); message('当前编辑仍在页面中，请先复制需要保留的内容'); }}>保留当前编辑</button><button className="button danger" onClick={() => { if (current) { move('opening_project'); void readProject(current.projectId).then(loaded => { setRecord(loaded); recordRef.current = loaded; setDirty(false); dirtyRef.current = false; setMode('edit'); setSelection(null); setHidden(new Set()); history.current = []; future.current = []; move('editor_editing'); }).catch(error => { message((error as Error).message); move('error'); }); } }}>重新打开磁盘版本</button></div></Dialog>}
    {notice && <div className="toast" role="status">{notice}</div>}
    <span className="sr-only">当前状态：{phaseName[phase]}</span>
  </div>;
}
