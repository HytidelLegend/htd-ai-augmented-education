import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { BookOpen, CalendarDays, FolderOpen, Languages, Pin, Settings, Star, Waypoints } from 'lucide-react';
import { addToStage, bootstrap, candidate, createProject, deleteProject, graph, jobStatus, openFavorite, projectLearning, projects, renameProject, updateProject, resumeRun, runStatus, saveFavorite, saveLevel, saveUi, startWord, wordPage, wordSummaries } from './api';
import GraphView from './GraphView';
import WordView from './WordView';
import { DictionaryPage, ProjectsPage } from './Pages';
import { StudyPage, StudyCalendarPage } from './StudyPage';
import SettingsPage from './SettingsPage';
import type { Bootstrap, FavoritesState, GraphData, LearningState, Level, RelationType, Tab, UiState, WordPage } from './model';
import { relationLabels, relationTypes } from './model';

const isPreview = (tab: Tab) => (tab.kind === 'word' || tab.kind === 'candidate') && !tab.pinned;
function openedTabs(tabs: Tab[], tab: Tab) {
  if (tabs.some(item => item.tabId === tab.tabId)) return tabs;
  if (!isPreview(tab)) return [...tabs, tab];
  const index = tabs.findIndex(isPreview);
  return index < 0 ? [...tabs, tab] : tabs.flatMap((item, i) => i === index ? [tab] : isPreview(item) ? [] : [item]);
}

type SaveStatus = 'saved' | 'saving' | 'error';
type Section = 'projects' | 'graph' | 'dictionary' | 'flashcards' | 'calendar' | 'favorites';
type CandidateInfo = Awaited<ReturnType<typeof candidate>>;

function themeVariables(config: Bootstrap['config']): CSSProperties {
  return {
    '--bg': config.ui.background, '--surface': config.ui.surface, '--raised': config.ui.surfaceRaised,
    '--text': config.ui.text, '--muted': config.ui.mutedText, '--border': config.ui.border,
    '--hover': config.ui.hover, '--selected': config.ui.selected, '--disabled': config.ui.disabled,
    '--error': config.ui.error, '--success': config.ui.success, '--focus': config.controls.focus,
    '--control-active': config.controls.activeText, '--control-inactive': config.controls.inactiveText,
    '--slider-track': config.controls.sliderTrack,
    '--star': config.favorites.star, '--family': config.graph.family, '--synonym': config.graph.relations.synonym,
    '--near': config.graph.relations.near_synonym, '--antonym': config.graph.relations.antonym,
    '--spelling': config.graph.relations.spelling_similar,
  } as CSSProperties;
}

function CandidateView({ lemma, onRefresh, onOpenWord }: { lemma: string; onRefresh: () => void; onOpenWord: (id: string) => void }) {
  const [info, setInfo] = useState<CandidateInfo | null>(null);
  const [error, setError] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<Awaited<ReturnType<typeof runStatus>> | null>(null);
  const [decision, setDecision] = useState('');
  const statusLabels: Record<string, string> = { pending: '待核验', pending_collection: '待采集',
    pending_relation_review: '关系待核验', candidate: '待核验', agent_reviewed: '审查通过',
    agent_passed: '审查通过', automatic_passed: '自动通过', linked: '已关联' };
  useEffect(() => { candidate(lemma).then(data => { setInfo(data); if (data.existingRunId) setRunId(data.existingRunId); }).catch(exc => setError(String(exc))); }, [lemma]);
  useEffect(() => {
    if (!jobId) return;
    const timer = window.setInterval(() => jobStatus(jobId).then(job => {
      if (job.runId) setRunId(job.runId);
      if (job.status !== 'running') { setJobId(null); if (job.status === 'error') setError(job.error || '词条构建失败'); else if (job.status === 'completed') {
        bootstrap().then(data => { onRefresh(); const found = data.words.find(word => word.lemma.toLocaleLowerCase() === lemma.toLocaleLowerCase()); if (found) onOpenWord(found.wordId); }).catch(exc => setError(String(exc)));
      } }
    }).catch(exc => { setJobId(null); setError(String(exc)); }), 1800);
    return () => window.clearInterval(timer);
  }, [jobId, lemma, onRefresh, onOpenWord]);
  useEffect(() => { if (runId) runStatus(runId).then(setRun).catch(exc => setError(String(exc))); }, [runId, jobId]);
  const begin = async () => {
    setError('');
    try { const result = await startWord(lemma); setJobId(result.jobId); setRunId(result.runId); }
    catch (exc) { setError(String(exc)); }
  };
  const resume = async () => {
    if (!runId) return;
    setError('');
    try { const parsed = decision.trim() ? JSON.parse(decision) as object : undefined; const job = await resumeRun(runId, parsed); setJobId(job.jobId); }
    catch (exc) { setError(String(exc)); }
  };
  return <div className="candidate-page"><span className="eyebrow">UNBUILT / 待采集</span><h1>{lemma}</h1>
    <p>这个单词尚未有正式词条。释义与关系需要通过词条构建流程核验。</p>
    {!!info?.associations.length && <div className="candidate-card"><h2>关联线索</h2>{info.associations.map((item, index) => <div key={index} className="candidate-line"><strong>{item.sourceLemma}</strong><span>{item.type === 'synonym_or_near_synonym' ? '近义词 · 按规则分类' : relationLabels[item.type as keyof typeof relationLabels] || '候选关系'} · {item.status ? statusLabels[item.status] || item.status : '待核验'}</span><small>{item.sourceSenseText || item.sourceSenseId || '词条级'}</small></div>)}</div>}
    {runId ? <div className="candidate-card"><h2>构建进度</h2><p>运行 ID：<code>{runId}</code></p><p>当前状态：{jobId ? '正在处理…' : run?.status || '正在读取…'}</p>
      {run?.current_stage && <p>当前步骤：{run.current_stage}</p>}{run?.error?.message && <p className="error-note" role="alert">暂停原因：{run.error.message}</p>}
      {!!run?.pending_decisions?.length && <p>待审查判断：{run.pending_decisions.length} 项</p>}
      {run && run.status !== 'completed' && !jobId && <><label className="decision-label">结构化审查响应（需审查时填写 JSON）<textarea value={decision} onChange={event => setDecision(event.target.value)} placeholder="按照 skill 提供的审查模板填写；浏览器操作完成后可留空继续。" /></label><button type="button" className="primary-button" onClick={resume}>从检查点继续</button></>}
    </div> : <button type="button" className="primary-button" disabled={!!jobId} onClick={begin}>新建词条 ↗</button>}
    {jobId && <p className="progress-note">正在运行词条状态机，请保持服务开启。</p>}
    {error && <p className="error-note" role="alert">{error}</p>}</div>;
}

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [ui, setUi] = useState<UiState | null>(null);
  const [learning, setLearning] = useState<LearningState | null>(null);
  const [favorites, setFavorites] = useState<FavoritesState | null>(null);
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [page, setPage] = useState<WordPage | null>(null);
  const pageCache = useRef<Record<string, WordPage>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [section, setSection] = useState<Section>('graph');
  const [resetToken, setResetToken] = useState(0);
  const [showOthers, setShowOthers] = useState(false);
  const [showOutside, setShowOutside] = useState(false);
  const [notice, setNotice] = useState('');
  const [retryAction, setRetryAction] = useState<(() => void) | null>(null);
  const [conflict, setConflict] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('saved');
  const [loadingError, setLoadingError] = useState('');
  const uiRef = useRef<UiState | null>(null);
  const savedUiRef = useRef<UiState | null>(null);
  const uiQueue = useRef(Promise.resolve());
  const favoritesRef = useRef<FavoritesState | null>(null);
  const learningRef = useRef<LearningState | null>(null);
  const learningSaved = useRef<Record<string, LearningState>>({});
  const learningPending = useRef<Record<string, Record<string, { level: Level; token: number }>>>({});
  const learningQueue = useRef(Promise.resolve());
  const learningToken = useRef(0);
  const draggedTab = useRef<string | null>(null);
  const [splitDrag, setSplitDrag] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const contentScrollRef = useRef<HTMLDivElement>(null);
  const scrollPositions = useRef<Record<string, number>>({});

  const refresh = useCallback(() => {
    const startedUi = uiRef.current;
    return bootstrap().then(data => {
    if (!Array.isArray(data.projects) || !data.ui?.activeProjectId) {
      throw new Error('当前词汇星图服务仍在运行旧版代码；请停止旧服务并重新运行 run.ps1。');
    }
    setBoot(data);
    if (!startedUi || uiRef.current === startedUi) {
      const previews = data.ui.tabs.filter(isPreview);
      const keep = previews.find(tab => tab.tabId === data.ui.activeTabId) || previews[previews.length - 1];
      data.ui.tabs = data.ui.tabs.filter(tab => !isPreview(tab) || tab === keep);
      setUi(data.ui); uiRef.current = data.ui; savedUiRef.current = data.ui;
      const saved = learningSaved.current[data.ui.activeProjectId];
      const latest = saved && saved.revision > data.learning.revision ? saved : data.learning;
      learningSaved.current[data.ui.activeProjectId] = latest;
      const next = { ...latest, words: { ...latest.words } };
      Object.entries(learningPending.current[data.ui.activeProjectId] || {}).forEach(([id, change]) => { next.words[id] = change.level; });
      setLearning(next); learningRef.current = next;
      setSelected(data.ui.tabs.find(tab => tab.tabId === data.ui.activeTabId)?.wordId || null);
    }
    setFavorites(data.favorites); favoritesRef.current = data.favorites;
    pageCache.current = {};
    setLoadingError('');
    void projects().then(items => setBoot(current => current ? { ...current, projects: items } : current))
      .catch(exc => setNotice(String(exc)));
  }).catch(exc => setLoadingError(String(exc)));
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!ui?.activeProjectId) return;
    let live = true;
    const projectId = ui.activeProjectId;
    projectLearning(projectId).then(value => {
      if (!live || uiRef.current?.activeProjectId !== projectId) return;
      const saved = learningSaved.current[projectId];
      if (saved && value.revision < saved.revision) return;
      learningSaved.current[projectId] = value;
      const pending = learningPending.current[projectId] || {};
      const next = { ...value, words: { ...value.words } };
      Object.entries(pending).forEach(([id, change]) => { next.words[id] = change.level; });
      learningRef.current = next; setLearning(next);
    })
      .catch(exc => { if (live) setNotice(String(exc)); });
    return () => { live = false; };
  }, [ui?.activeProjectId]);
  const saveFailure = (message: string, exc: unknown, retry: () => void) => {
    const detail = String(exc);
    const hasConflict = detail.includes('版本冲突');
    setNotice(`${message}：${detail}`);
    setConflict(hasConflict);
    setRetryAction(() => hasConflict ? () => { void refresh(); } : retry);
    setSaveStatus('error');
  };
  useEffect(() => {
    if (!ui) return;
    let live = true;
    if (section === 'projects' || section === 'dictionary') return;
    const project = section === 'graph' && ui.activeTabId !== 'favorites' ? ui.activeProjectId : undefined;
    wordSummaries(ui.activeStageId, project).then(words => { if (live) setBoot(current => current ? { ...current, words } : current); }).catch(exc => { if (live) setNotice(String(exc)); });
    return () => { live = false; };
  }, [ui?.activeStageId, ui?.activeProjectId, ui?.activeTabId, section]);
  useEffect(() => { if (section === 'graph' && boot?.config.graph.initializeOnEnter) setResetToken(value => value + 1); }, [section, !!boot, ui?.activeProjectId]);

  const commitUi = useCallback((transform: (current: UiState) => UiState) => {
    if (!uiRef.current) return;
    const next = transform(uiRef.current);
    uiRef.current = next; setUi(next); setSaveStatus('saving');
    uiQueue.current = uiQueue.current.then(async () => {
      const draft = uiRef.current!;
      let base = savedUiRef.current!;
      let saved: UiState | null = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try { saved = await saveUi(transform(base), base.revision); break; }
        catch (exc) {
          if (!String(exc).includes('版本冲突') || attempt === 2) throw exc;
          base = (await bootstrap()).ui;
        }
      }
      if (!saved) throw new Error('界面状态未保存');
      savedUiRef.current = saved;
      if (uiRef.current === draft) { uiRef.current = saved; setUi(saved); setSaveStatus('saved'); }
    }).catch(exc => { saveFailure('界面状态未保存', exc, () => commitUi(current => ({ ...current }))); });
  }, []);
  const activeTab = ui?.tabs.find(tab => tab.tabId === ui.activeTabId) || ui?.tabs[0];
  useLayoutEffect(() => {
    if (!activeTab || !contentScrollRef.current) return;
    if (activeTab.kind === 'word' && page?.entry.wordId !== activeTab.wordId) return;
    contentScrollRef.current.scrollTop = scrollPositions.current[activeTab.tabId] || 0;
  }, [activeTab?.tabId, page?.entry.wordId]);
  const activeWordId = activeTab?.kind === 'word' ? activeTab.wordId : null;
  useEffect(() => {
    if (!activeWordId || !ui) { setPage(null); return; }
    const key = `${ui.activeStageId}:${activeWordId}`;
    if (pageCache.current[key]) { setPage(pageCache.current[key]); return; }
    setPage(null);
    let live = true;
    wordPage(activeWordId, ui.activeStageId).then(data => { if (live) { pageCache.current[key] = data; setPage(data); } }).catch(exc => { if (live) setNotice(String(exc)); });
    return () => { live = false; };
  }, [activeWordId, ui?.activeStageId]);

  useEffect(() => {
    if (!ui || section !== 'graph') return;
    const params = new URLSearchParams({ stage: ui.activeStageId, project: ui.activeProjectId, others: showOthers ? '1' : '0', outside: showOutside ? '1' : '0', search });
    if (selected) params.set('selected', selected);
    relationTypes.forEach(kind => params.set(kind, ui.graph.visibleRelations[kind] ? '1' : '0'));
    let live = true;
    graph(params).then(data => { if (live) setGraphData(data); }).catch(exc => { if (live) setNotice(String(exc)); });
    return () => { live = false; };
  }, [ui?.activeStageId, ui?.activeProjectId, ui?.graph.visibleRelations, selected, showOthers, showOutside, search, section]);

  useEffect(() => {
    if (!splitDrag) return;
    const move = (event: MouseEvent) => {
      if (!shellRef.current) return;
      const bounds = shellRef.current.getBoundingClientRect();
      const value = Math.min(75, Math.max(25, ((event.clientX - bounds.left) / bounds.width) * 100));
      if (uiRef.current) { const draft = { ...uiRef.current, split: { ...uiRef.current.split, leftWidthPercent: value } }; uiRef.current = draft; setUi(draft); }
    };
    const up = () => {
      const width = uiRef.current?.split.leftWidthPercent;
      setSplitDrag(false);
      if (width !== undefined) commitUi(current => ({ ...current, split: { ...current.split, leftWidthPercent: width } }));
    };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
  }, [splitDrag, commitUi]);

  const openTab = (tab: Tab) => commitUi(current => {
    const tabs = openedTabs(current.tabs, tab);
    return { ...current, tabs, activeTabId: tab.tabId, split: { ...current.split, enabled: tab.kind !== 'graph' ? current.split.enabled : false } };
  });
  const openWord = (id: string, projectId?: string) => {
    setSection('graph');
    setSelected(id); setShowOthers(false);
    commitUi(current => {
      const tab: Tab = { tabId: `word:${id}`, kind: 'word', wordId: id, lemma: null, pinned: false };
      return { ...current, activeProjectId: projectId || current.activeProjectId,
        tabs: openedTabs(current.tabs, tab),
        activeTabId: tab.tabId, split: { ...current.split, enabled: true },
        graph: { ...current.graph, zoomPercent: Math.max(125, current.graph.zoomPercent) } };
    });
    const current = favoritesRef.current;
    if (current?.words[id]) openFavorite(id, current.revision).then(result => { favoritesRef.current = result; setFavorites(result); }).catch(exc => saveFailure('最后打开时间未保存', exc, () => openWord(id, projectId)));
  };
  const openCandidate = (lemma: string, projectId?: string) => { setSection('graph');
    const nodeId = graphData?.nodes.find(node => node.lemma.toLowerCase() === lemma.toLowerCase())?.wordId || null;
    if (nodeId) { setSelected(nodeId); setShowOthers(false); }
    if (projectId) commitUi(current => ({ ...current, activeProjectId: projectId }));
    openTab({ tabId: `candidate:${lemma}`, kind: 'candidate', wordId: nodeId, lemma, pinned: false });
  };
  const closeTab = (id: string) => commitUi(current => {
    const tab = current.tabs.find(item => item.tabId === id);
    if (!tab || tab.pinned) return current;
    const tabs = current.tabs.filter(item => item.tabId !== id);
    return { ...current, tabs, activeTabId: current.activeTabId === id ? tabs[tabs.length - 1].tabId : current.activeTabId };
  });
  const togglePin = (id: string) => commitUi(current => {
    const target = current.tabs.find(tab => tab.tabId === id);
    if (!target || id === 'graph') return current;
    const changed = { ...target, pinned: !target.pinned };
    const tabs = current.tabs.flatMap(tab => tab.tabId === id ? [changed] : isPreview(changed) && isPreview(tab) ? [] : [tab]);
    return { ...current, tabs, activeTabId: tabs.some(tab => tab.tabId === current.activeTabId) ? current.activeTabId : id };
  });
  const reorderTab = (target: string) => {
    const source = draggedTab.current;
    if (!source || source === target || source === 'graph' || target === 'graph') return;
    commitUi(current => {
      const tabs = current.tabs.slice();
      const from = tabs.findIndex(item => item.tabId === source), to = tabs.findIndex(item => item.tabId === target);
      if (from < 0 || to < 0) return current;
      tabs.splice(to, 0, tabs.splice(from, 1)[0]);
      return { ...current, tabs };
    });
    draggedTab.current = null;
  };
  const changeLevel = (id: string, level: Level) => {
    const projectId = uiRef.current?.activeProjectId;
    if (!projectId || !learningRef.current) return;
    const token = ++learningToken.current;
    const pending = learningPending.current[projectId] ||= {};
    pending[id] = { level, token };
    const draft = { ...learningRef.current, words: { ...learningRef.current.words, [id]: level } };
    learningRef.current = draft; setLearning(draft); setSaveStatus('saving');
    learningQueue.current = learningQueue.current.then(async () => {
      let base = learningSaved.current[projectId] || await projectLearning(projectId);
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const saved = await saveLevel(id, level, base.revision, projectId);
          learningSaved.current[projectId] = saved;
          if (learningPending.current[projectId]?.[id]?.token === token) delete learningPending.current[projectId][id];
          if (uiRef.current?.activeProjectId === projectId) {
            const next = { ...saved, words: { ...saved.words } };
            Object.entries(learningPending.current[projectId] || {}).forEach(([wordId, change]) => { next.words[wordId] = change.level; });
            learningRef.current = next; setLearning(next);
          }
          setSaveStatus('saved');
          return;
        } catch (exc) {
          if (!String(exc).includes('版本冲突') || attempt === 2) throw exc;
          base = await projectLearning(projectId);
          learningSaved.current[projectId] = base;
        }
      }
    }).catch(exc => saveFailure('熟悉度未保存', exc, () => changeLevel(id, level)));
  };
  const toggleFavorite = async (id: string) => {
    const current = favoritesRef.current;
    if (!current) return;
    const next = !current.words[id];
    try { const saved = await saveFavorite(id, next, current.revision); favoritesRef.current = saved; setFavorites(saved); }
    catch (exc) { saveFailure('收藏未保存', exc, () => { void toggleFavorite(id); }); }
  };
  const includeInStage = async (id: string) => {
    if (!ui || !boot || ui.activeStageId === 'all') return;
    const stage = boot.stages.find(item => item.stageId === ui.activeStageId);
    if (!stage) return;
    setSaveStatus('saving');
    try {
      await addToStage(stage.stageId, id, stage.revision);
      await refresh();
      const updated = await wordPage(id, stage.stageId);
      pageCache.current[`${stage.stageId}:${id}`] = updated;
      setPage(updated);
      setSaveStatus('saved');
    } catch (exc) { saveFailure('加入学龄段未保存', exc, () => { void includeInStage(id); }); }
  };
  const updateGraph = (change: Partial<UiState['graph']>) => commitUi(current => ({ ...current, graph: { ...current.graph, ...change } }));
  const toggleRelation = (kind: RelationType) => commitUi(current => ({ ...current, graph: { ...current.graph,
    visibleRelations: { ...current.graph.visibleRelations, [kind]: !current.graph.visibleRelations[kind] } } }));
  const selectNode = (id: string | null) => {
    if (!id) { setSelected(null); setShowOthers(false); return; }
    openWord(id);
  };
  const zoom = (value: number) => updateGraph({ zoomPercent: Math.min(400, Math.max(25, Math.round(value / 5) * 5)) });
  const setPositions = (positions: Record<string, { x: number; y: number }>) => commitUi(current => ({ ...current, graph: { ...current.graph,
    positions: { ...current.graph.positions, ...positions } } }));
  const switchProject = (id: string) => { setSelected(null); setShowOthers(false); setGraphData(null); setSection('graph'); commitUi(current => ({ ...current, activeProjectId: id, activeTabId: 'graph' })); };
  const addProject = async (name: string, content: string, format: string) => {
    const created = await createProject(name, content, format);
    await refresh();
    switchProject(created.projectId);
  };
  const saveProject = async (project: Bootstrap['projects'][number], name: string, content: string, replan: boolean) => {
    const updated = await updateProject(project.projectId, name, content, project.revision, replan);
    setBoot(current => current ? { ...current, projects: current.projects.map(item => item.projectId === updated.projectId ? updated : item) } : current);
    if (project.projectId === uiRef.current?.activeProjectId) setGraphData(null);
  };
  const removeProject = async (project: Bootstrap['projects'][number]) => {
    await deleteProject(project.projectId, project.revision);
    await refresh();
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { setSelected(null); setShowOthers(false); } };
    window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!boot || !ui || !learning || !favorites) return <div className="loading-screen"><div className="loading-mark">英</div><h1>词汇星图</h1><p>正在整理词与词之间的线索…</p>{loadingError && <><p className="error-note">{loadingError}</p><button type="button" onClick={refresh}>重试连接</button></>}</div>;
  const graphProject = boot.projects.find(project => project.projectId === ui.activeProjectId);
  const renameGraphProject = async (name: string) => {
    if (!graphProject) throw new Error('项目仍在加载，请稍后重试');
    const updated = await renameProject(graphProject.projectId, name, graphProject.revision);
    setBoot(current => current ? { ...current, projects: current.projects.map(item => item.projectId === updated.projectId ? updated : item) } : current);
  };
  const graphView = <GraphView projectName={graphProject?.name || ''} onProjectRename={renameGraphProject} key={ui.activeProjectId} data={graphData} theme={boot.config} selected={selected} zoom={ui.graph.zoomPercent} positions={ui.graph.positions}
    visible={ui.graph.visibleRelations} levels={learning.words} search={search} showOthers={showOthers} showOutside={showOutside} stageId={ui.activeStageId}
    onSearch={setSearch} onSelect={selectNode} onToggle={toggleRelation} onShowOthers={() => setShowOthers(!showOthers)}
    onOpenCandidate={openCandidate} onShowOutside={() => setShowOutside(!showOutside)} onZoom={zoom} onPositions={setPositions}
    resetToken={resetToken} onInitialize={() => setResetToken(value => value + 1)} />;
  const content = activeTab?.kind === 'word' && page && page.entry.wordId === activeTab.wordId ? <WordView page={page}
    level={learning.words[page.entry.wordId] || 'unfamiliar'} favorited={!!favorites.words[page.entry.wordId]}
    onLevel={level => changeLevel(page.entry.wordId, level)} onFavorite={() => toggleFavorite(page.entry.wordId)} onOpenWord={openWord} onOpenCandidate={openCandidate}
    onAddToStage={() => includeInStage(page.entry.wordId)} />
    : activeTab?.kind === 'favorites' ? <div className="utility-page"><span className="eyebrow">PERSONAL COLLECTION</span><h1>收藏夹</h1><p>把值得回看的词，留在这里。</p>
      <div className="favorite-list">{Object.entries(favorites.words).sort((a, b) => b[1].favoritedAt.localeCompare(a[1].favoritedAt)).map(([id, stamp]) => {
        const item = boot.words.find(word => word.wordId === id);
        if (!item) return null;
        return <button key={id} className="favorite-row" onClick={() => openWord(id)}><span className="favorite-glyph">★</span><span><strong>{item.lemma}</strong><small>{item.core.map(s => s.text).join('；') || '释义待完善'}</small><small>{item.more.map(s => s.text).join('；')}</small></span><span className="favorite-times">收藏 {stamp.favoritedAt}<br />上次打开 {stamp.lastOpenedAt}</span><span>↗</span></button>;
      })}{!Object.keys(favorites.words).length && <div className="empty-content">还没有收藏词条。在单词页点击星标即可加入。</div>}</div></div>
    : activeTab?.kind === 'settings' ? <SettingsPage stages={boot.stages} activeStageId={ui.activeStageId}
        onStageChange={id => commitUi(current => ({ ...current, activeStageId: id }))}
        onApplied={config => setBoot(current => current ? { ...current, config } : current)} />
    : activeTab?.kind === 'candidate' && activeTab.lemma ? <CandidateView lemma={activeTab.lemma} onRefresh={refresh} onOpenWord={openWord} />
    : activeTab?.kind === 'graph' ? <div className="welcome-pane"><span className="eyebrow">A LIVING LEXICON</span><h1>从一个词，走向一片词语。</h1><p>点击左侧图谱中的词，查看它的释义、例句和关联；也可以搜索想学的词。</p><div className="welcome-stats"><strong>{boot.words.length}</strong><span>个已入库词条</span></div></div> : <div className="empty-content">正在载入词条…</div>;

  return <div className="app-shell" style={themeVariables(boot.config)}>
    <div className="app-body"><nav className="page-rail" aria-label="页面切换">
      <div className="rail-brand"><span className="brand-seal">英</span><span className="rail-brand-text"><strong>词汇星图</strong><small>WORD ATLAS</small></span></div>
      {([
        ['projects', FolderOpen, '项目页'], ['graph', Waypoints, '词汇星图'], ['dictionary', Languages, '词典页'],
        ['flashcards', BookOpen, '背单词页'], ['calendar', CalendarDays, '日历页'], ['favorites', Star, '收藏夹'],
      ] as const).map(([id, Icon, label]) => <button key={id} type="button" title={label} aria-label={label} aria-current={section === id ? 'page' : undefined}
        className={section === id ? 'active' : ''} onClick={() => {
          if (id === 'favorites') { openTab({ tabId: 'favorites', kind: 'favorites', wordId: null, lemma: null, pinned: false }); setSection('graph'); }
          else setSection(id);
        }}><Icon size={19} /><span>{label}</span></button>)}
      <button type="button" className="rail-settings" title="设置" aria-label="设置" onClick={() => { openTab({ tabId: 'settings', kind: 'settings', wordId: null, lemma: null, pinned: false }); setSection('graph'); }}><Settings size={19} /><span>设置</span></button>
    </nav><div className="app-main">{section === 'graph' ? <>
    <div className="tab-bar" role="tablist">{ui.tabs.map(tab => <div key={tab.tabId} role="tab" aria-selected={ui.activeTabId === tab.tabId} tabIndex={0}
      className={`tab ${ui.activeTabId === tab.tabId ? 'active' : ''}`} draggable={tab.tabId !== 'graph'} onDragStart={() => { draggedTab.current = tab.tabId; }} onDragOver={event => event.preventDefault()} onDrop={() => reorderTab(tab.tabId)}
      onDoubleClick={() => { if (isPreview(tab)) togglePin(tab.tabId); }} onClick={() => commitUi(current => ({ ...current, activeTabId: tab.tabId }))} onKeyDown={event => { if (event.key === 'Enter') commitUi(current => ({ ...current, activeTabId: tab.tabId })); }}>
      <span className="tab-icon">{tab.kind === 'graph' ? '◉' : tab.kind === 'favorites' ? '★' : tab.kind === 'settings' ? '⚙' : '◌'}</span>
      <span>{tab.kind === 'graph' ? '词汇星图' : tab.kind === 'favorites' ? '收藏夹' : tab.kind === 'settings' ? '设置' : tab.kind === 'word' ? boot.words.find(w => w.wordId === tab.wordId)?.lemma || '单词' : tab.lemma}</span>
      {tab.tabId === 'graph' ? <span className="pin"><Pin size={13} fill="currentColor" /></span> : <><button type="button" className={`tab-pin ${tab.pinned ? 'is-pinned' : ''}`} aria-label={tab.pinned ? '取消固定标签' : '固定标签'} title={tab.pinned ? '取消固定' : '固定标签'} aria-pressed={tab.pinned} onClick={event => { event.stopPropagation(); togglePin(tab.tabId); }}><Pin size={14} fill={tab.pinned ? 'currentColor' : 'none'} /></button>
        {!tab.pinned && <button type="button" aria-label="关闭标签" onClick={event => { event.stopPropagation(); closeTab(tab.tabId); }}>×</button>}</>}</div>)}
      <span className={`save-state ${saveStatus}`}>{saveStatus === 'saved' ? '✓ 已保存' : saveStatus === 'saving' ? '◌ 保存中' : '! 未保存'}</span></div>
    <main ref={shellRef} className={`workspace ${ui.split.enabled && activeTab?.kind !== 'graph' ? 'is-split' : 'graph-only'}`}>
      <div className="workspace-left" style={{ width: ui.split.enabled && activeTab?.kind !== 'graph' ? `${ui.split.leftWidthPercent}%` : '100%' }}>{graphView}</div>
      {ui.split.enabled && activeTab?.kind !== 'graph' && <div className="split-handle" role="separator" aria-orientation="vertical" aria-label="调整分屏宽度" onMouseDown={() => setSplitDrag(true)} />}
      {activeTab?.kind !== 'graph' && <div className={`workspace-right ${ui.split.enabled ? '' : 'independent'}`}>
        <div className="pane-actions"><span>{activeTab?.kind === 'word' ? '单词笔记' : activeTab?.kind === 'candidate' ? '候选词' : '我的空间'}</span><button type="button" onClick={() => commitUi(current => ({ ...current, split: { ...current.split, enabled: !current.split.enabled } }))}>{ui.split.enabled ? '独立显示 ↗' : '返回分屏 ↙'}</button></div>
        <div className="content-scroll" ref={contentScrollRef} onScroll={event => { if (activeTab && (activeTab.kind !== 'word' || page?.entry.wordId === activeTab.wordId)) scrollPositions.current[activeTab.tabId] = event.currentTarget.scrollTop; }}>{content}</div></div>}
    </main></> : <div className="section-host">
      {section === 'projects' ? <ProjectsPage projects={boot.projects} activeId={ui.activeProjectId} onSelect={switchProject} onCreate={addProject} onSave={saveProject} onDelete={removeProject} /> :
       section === 'dictionary' ? <DictionaryPage activeProjectId={ui.activeProjectId} onOpenWord={openWord} onOpenCandidate={openCandidate} /> :
       section === 'calendar' ? <StudyCalendarPage projects={boot.projects} words={boot.words} /> :
       section === 'favorites' ? content :
       <StudyPage projects={boot.projects} words={boot.words} levels={learning.words} activeProjectId={ui.activeProjectId} favorites={favorites} pageSize={boot.config.study.wordsPerPage}
         tabs={ui.study.openPlanIds} active={ui.study.activeTabId} onTabsChange={(tabs, active) => commitUi(current => ({ ...current, study: { openPlanIds: tabs, activeTabId: active } }))}
         onLevel={(id, level) => { void changeLevel(id, level); }} onFavorite={id => { void toggleFavorite(id); }} onOpenWord={openWord} onOpenCandidate={openCandidate} />}
    </div>}</div></div>
    {notice && <div className="toast" role="alert"><span>{notice}</span><button type="button" onClick={() => { setNotice(''); if (saveStatus === 'error') retryAction?.(); }}>{saveStatus === 'error' ? conflict ? '重新载入' : '重试保存' : '关闭'}</button></div>}
  </div>;
}
