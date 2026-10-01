import { useEffect, useMemo, useRef, useState } from 'react';
import { createStudyPlan, deleteStudyPlan, projectLearning, saveLevel, studyDate, studyPlan, studyPlanDay, studyPlans, studyToday, updateStudyPlan } from './api';
import type { FavoritesState, Level, Project, StudyDay, StudyPlan, WordSummary } from './model';

const localDate = (value: Date) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
const addDays = (day: string, count: number) => { const value = new Date(`${day}T12:00:00`); value.setDate(value.getDate() + count); return localDate(value); };
const fmt = (value: string) => value.replace('T', ' ');

function ScrollCalendar({ start, end, onSelect }: { start: string; end: string; onSelect: (day: string) => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const monthRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const anchor = new Date(`${start}T12:00:00`);
  const months = Array.from({ length: 25 }, (_, index) => new Date(anchor.getFullYear(), anchor.getMonth() + index - 12, 1));
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const key = start.slice(0, 7);
      const container = scrollRef.current, month = monthRefs.current[key];
      if (container && month) container.scrollTop += month.getBoundingClientRect().top - container.getBoundingClientRect().top;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [start]);
  return <div className="study-calendar-scroll" ref={scrollRef}>{months.map(month => {
    const year = month.getFullYear(), number = month.getMonth(), offset = (month.getDay() + 6) % 7;
    const count = new Date(year, number + 1, 0).getDate();
    return <div className="study-calendar-month" key={`${year}-${number}`} ref={element => { monthRefs.current[`${year}-${String(number + 1).padStart(2, '0')}`] = element; }}><h3>{year} 年 {number + 1} 月</h3><div className="study-calendar-grid">
      {['一', '二', '三', '四', '五', '六', '日'].map(label => <strong key={label}>{label}</strong>)}
      {Array.from({ length: offset }, (_, index) => <span key={`empty-${index}`} />)}
      {Array.from({ length: count }, (_, index) => { const day = localDate(new Date(year, number, index + 1)); return <button type="button" key={day}
        className={`${day >= start && day <= end ? 'in-range' : ''} ${day === start ? 'range-start' : ''} ${day === end ? 'range-end' : ''}`}
        aria-label={day} aria-pressed={day === start} onClick={() => onSelect(day)}>{index + 1}</button>; })}
    </div></div>;
  })}</div>;
}

function CreateDialog({ projects, today, plan, onClose, onSaved }: { projects: Project[]; today: string; plan?: StudyPlan; onClose: () => void; onSaved: (plan: StudyPlan) => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = dialogRef.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  const [projectId, setProjectId] = useState(plan?.projectId || projects[0]?.projectId || '');
  const [method, setMethod] = useState(plan?.method || 'ebbinghaus');
  const [name, setName] = useState(plan?.name || '');
  const [startDate, setStartDate] = useState(plan?.rounds[0].startDate || today);
  const [dailyItems, setDailyItems] = useState(plan ? Math.ceil(plan.wordIds.length / plan.schedule.batches.length) : 50);
  const [days, setDays] = useState(plan?.schedule.batches.length || 1);
  const [lastEdited, setLastEdited] = useState<'dailyItems' | 'firstPassDays'>(plan ? 'firstPassDays' : 'dailyItems');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const count = projects.find(item => item.projectId === projectId)?.words.length || 0;
  const effectiveDays = lastEdited === 'firstPassDays' ? days : Math.ceil(count / Math.max(1, dailyItems));
  useEffect(() => { if (lastEdited === 'dailyItems') setDays(Math.max(1, effectiveDays)); }, [count, dailyItems, effectiveDays, lastEdited]);
  const changeDays = (value: number) => { const next = Math.min(Math.max(1, count), Math.max(1, Math.round(value || 1))); setDays(next); setDailyItems(Math.max(1, Math.ceil(count / next))); setLastEdited('firstPassDays'); };
  const changeDaily = (value: number) => { setDailyItems(Math.max(1, Math.round(value || 1))); setLastEdited('dailyItems'); };
  const endDate = addDays(startDate, effectiveDays - 1);
  const save = async () => {
    setBusy(true); setError('');
    try {
      const scheduleInput = lastEdited === 'dailyItems' ? { dailyItems } : { firstPassDays: days };
      const finalName = name.trim() || `${projects.find(item => item.projectId === projectId)?.name || '背诵计划'} · ${method === 'hulu' ? '葫芦背书法' : '艾宾浩斯'} · ${startDate}`;
      if (plan) {
        const sameSchedule = method === plan.method && projectId === plan.projectId && startDate === plan.rounds[0].startDate &&
          lastEdited === 'firstPassDays' && days === plan.schedule.batches.length;
        const updated = sameSchedule ? await updateStudyPlan(plan, 'rename', { name: finalName }) :
          await updateStudyPlan(plan, 'reconfigure', { projectId, name: finalName, method, startDate, ...scheduleInput });
        onSaved(updated);
      } else onSaved(await createStudyPlan({ projectId, name, method, startDate, ...scheduleInput }));
    }
    catch (exc) { setError(String(exc)); }
    finally { setBusy(false); }
  };
  return <dialog ref={dialogRef} className="study-modal" aria-label={plan ? '设置背诵计划' : '新建背诵计划'} onCancel={event => { event.preventDefault(); onClose(); }}>
      <header><h2>{plan ? '设置背诵计划' : '新建背诵计划'}</h2><button type="button" aria-label="关闭" onClick={onClose}>×</button></header>
      <label>项目<select value={projectId} onChange={event => { setProjectId(event.target.value); setLastEdited('dailyItems'); }}>{projects.map(project => <option key={project.projectId} value={project.projectId}>{project.name} · {project.words.length} 词</option>)}</select></label>
      <label>计划名（可选）<input value={name} onChange={event => setName(event.target.value)} placeholder="默认按项目和日期命名" /></label>
      <label>记忆方法<select value={method} onChange={event => setMethod(event.target.value as StudyPlan['method'])}><option value="ebbinghaus">艾宾浩斯遗忘曲线</option><option value="hulu">葫芦背书法</option></select></label>
      <div className="study-number-grid"><label>每天要背的单词数<input type="number" min="1" step="1" value={dailyItems} onChange={event => changeDaily(Number(event.target.value))} /></label>
        <label>要背完一遍的天数<input type="number" min="1" step="1" value={days} onChange={event => changeDays(Number(event.target.value))} /></label></div>
      <p className="study-date-note">开始日期：{startDate}　首遍结束日期：{endDate}（含首尾两天）</p>
      <ScrollCalendar start={startDate} end={endDate} onSelect={setStartDate} />
      {error && <p role="alert" className="error-note">{error}</p>}
      {plan && <p className="study-date-note">修改项目、日期或背诵数量会重新排程，并清空所有轮次的通过记录。</p>}
      <footer><button type="button" onClick={onClose}>取消</button><button type="button" className="primary-button" disabled={busy || !count} onClick={() => void save()}>{busy ? '保存中…' : plan ? '保存设置' : '创建计划'}</button></footer>
  </dialog>;
}

function DeleteDialog({ plan, onClose, onDelete }: { plan: StudyPlan; onClose: () => void; onDelete: () => Promise<void> }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { const dialog = dialogRef.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  const remove = async () => { setBusy(true); setError(''); try { await onDelete(); } catch (exc) { setError(String(exc)); setBusy(false); } };
  return <dialog ref={dialogRef} className="study-modal study-delete-modal" aria-label="确认删除背诵计划" onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><h2>删除背诵计划？</h2><button type="button" aria-label="关闭" onClick={onClose}>×</button></header>
    <p>确认删除“{plan.name}”？该计划的排程和所有通过记录会被删除，对应标签页也会关闭。</p>
    {error && <p role="alert" className="error-note">{error}</p>}
    <footer><button type="button" onClick={onClose}>取消</button><button type="button" className="primary-button" disabled={busy} onClick={() => void remove()}>{busy ? '删除中…' : '确认删除'}</button></footer>
  </dialog>;
}

function PlanView({ plan, today, projects, words, levels, favorites, pageSize, onPatch, onLevel, onFavorite, onOpenWord, onOpenCandidate }: {
  plan: StudyPlan; projects: Project[]; words: WordSummary[]; levels: Record<string, Level>; favorites: FavoritesState;
  today: string;
  pageSize: number; onPatch: (plan: StudyPlan, action: string, value?: object) => Promise<StudyPlan>;
  onLevel: (id: string, level: Level) => void; onFavorite: (id: string) => void; onOpenWord: (id: string) => void; onOpenCandidate: (lemma: string) => void;
}) {
  const round = plan.rounds[plan.rounds.length - 1];
  const [day, setDay] = useState(today < round.startDate ? round.startDate : today > round.reviewEndDate ? round.reviewEndDate : today);
  useEffect(() => { setDay(today < round.startDate ? round.startDate : today > round.reviewEndDate ? round.reviewEndDate : today); }, [today, round.startDate, round.reviewEndDate]);
  const [practice, setPractice] = useState(false);
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(false);
  const [retryCycle, setRetryCycle] = useState(1);
  const [error, setError] = useState('');
  const [pendingPass, setPendingPass] = useState<Record<string, boolean>>({});
  const [pendingShuffle, setPendingShuffle] = useState<Partial<typeof round.shuffle>>({});
  const shuffle = { ...round.shuffle, ...pendingShuffle };
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(plan.name);
  useEffect(() => { setPage(0); setRetry(false); setRetryCycle(1); setShown({}); setPractice(false); }, [day, plan.planId, round.number]);
  const offset = Math.round((new Date(`${day}T12:00:00`).getTime() - new Date(`${round.startDate}T12:00:00`).getTime()) / 86400000);
  const scheduleDay = plan.schedule.days[offset];
  const original = useMemo(() => scheduleDay?.item_ids.map(number => plan.wordIds[number - 1]) || [], [scheduleDay, plan.wordIds]);
  const [dayOrder, setDayOrder] = useState<{ day: string; revision: number; wordIds: string[] } | null>(null);
  useEffect(() => { let live = true; setDayOrder(null);
    studyPlanDay(plan.planId, day, pageSize).then(result => { if (live) setDayOrder({ day, revision: plan.revision, wordIds: result.wordIds }); })
      .catch(exc => { if (live) setError(String(exc)); });
    return () => { live = false; };
  }, [plan.planId, plan.revision, day, pageSize]);
  const ordered = dayOrder?.day === day && dayOrder.revision === plan.revision ? dayOrder.wordIds : original;
  const passed = round.passed[day] || [];
  const retries = ordered.filter(id => !passed.includes(id));
  const list = plan.method === 'hulu' ? ordered : retry ? retries : ordered;
  const totalPages = Math.max(1, Math.ceil(list.length / pageSize));
  useEffect(() => { if (page >= totalPages) setPage(totalPages - 1); }, [page, totalPages]);
  useEffect(() => { setShown({}); }, [page]);
  const visible = list.slice(page * pageSize, (page + 1) * pageSize);
  const todayOffset = Math.round((new Date(`${today}T12:00:00`).getTime() - new Date(`${round.startDate}T12:00:00`).getTime()) / 86400000);
  const todayWords = today >= round.startDate && today <= round.reviewEndDate ?
    [...new Set((plan.schedule.days[todayOffset]?.item_ids || []).map(number => plan.wordIds[number - 1]))] : [];
  const progressText = (ids: string[], passedIds: string[]) => {
    const count = ids.filter(id => passedIds.includes(id)).length;
    return `${ids.length ? Math.round(count * 100 / ids.length) : 0}%（${count}/${ids.length}）`;
  };
  const wordById = Object.fromEntries(words.map(word => [word.wordId, word]));
  const lemmaById = Object.fromEntries((projects.find(project => project.projectId === plan.projectId)?.words || []).map(word => [word.wordId, word.lemma]));
  const update = async (action: string, value: object = {}) => { try { setError(''); await onPatch(plan, action, value); return true; } catch (exc) { setError(String(exc)); return false; } };
  const changeShuffle = (key: keyof typeof round.shuffle, value: boolean) => {
    setPendingShuffle(current => ({ ...current, [key]: value }));
    void update('shuffle', { [key]: value }).finally(() => setPendingShuffle(current => { const next = { ...current }; delete next[key]; return next; }));
  };
  const next = () => { if (plan.method === 'hulu') {
      if (Object.keys(pendingPass).length) { setError('请等待通过记录保存完成。'); return; }
      if (!practice) { setError('请先切换到练习模式。'); return; }
      const passedCount = visible.filter(id => passed.includes(id)).length;
      if (5 * passedCount < 4 * visible.length) {
        setPractice(false); setShown({}); setError('本页未达到 80%，请回到学习模式快速记忆后再练习。'); return;
      }
      setError(''); setPractice(false); setShown({});
      if (page + 1 < totalPages) setPage(page + 1);
      return;
    }
    if (page + 1 < totalPages) setPage(page + 1);
    else if (retry && retries.length) { setRetryCycle(current => current + 1); setShown({}); setPage(0); }
    else if (!retry && retries.length) { setRetry(true); setRetryCycle(1); setShown({}); setPage(0); } };
  const previous = () => { if (page > 0) setPage(page - 1); else if (retry) { setRetry(false); setPage(Math.max(0, Math.ceil(ordered.length / pageSize) - 1)); } };
  useEffect(() => { const handler = (event: KeyboardEvent) => { const tag = (event.target as HTMLElement)?.tagName;
    const editing = tag === 'TEXTAREA' || tag === 'SELECT' ||
      (tag === 'INPUT' && !['checkbox', 'radio'].includes((event.target as HTMLInputElement).type)) ||
      (event.target as HTMLElement)?.isContentEditable;
    if (editing) return;
    if (event.key === ' ' && practice) {
      event.preventDefault();
      if (event.repeat) return;
      const target = visible.find(id => !shown[id]);
      if (target) setShown(current => ({ ...current, [target]: true }));
    }
    if (event.key === 'ArrowRight') next(); if (event.key === 'ArrowLeft') previous(); };
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  });
  return <div className="study-plan-view"><header><div><small>{projects.find(project => project.projectId === plan.projectId)?.name} · 第 {round.number} 轮</small>
    {editingName ? <div className="study-rename"><input aria-label="计划名" value={nameDraft} onChange={event => setNameDraft(event.target.value)} /><button type="button" onClick={() => { void update('rename', { name: nameDraft }).then(success => { if (success) setEditingName(false); }); }}>保存</button></div>
      : <h2>{plan.name} <button type="button" className="study-rename-button" onClick={() => { setNameDraft(plan.name); setEditingName(true); }}>重命名</button></h2>}</div>
    <div className="study-mode-actions">{practice && <><button type="button" onClick={() => setShown({})}>隐藏所有答案</button><button type="button" onClick={() => setShown(Object.fromEntries(ordered.map(id => [id, true])))}>显示所有答案</button></>}
      <label className="study-switch">学习模式 <input type="checkbox" checked={practice} onChange={event => { setPractice(event.target.checked); setShown({}); setError(''); }} /> 练习模式</label></div></header>
    <div className="study-plan-controls"><label>背诵日期 <input type="date" min={round.startDate} max={round.reviewEndDate} value={day} onChange={event => setDay(event.target.value)} /></label>
      <label className="study-switch">乱序 <input type="checkbox" checked={shuffle.enabled} onChange={event => changeShuffle('enabled', event.target.checked)} /></label>
      {shuffle.enabled && <><button type="button" aria-pressed={shuffle.withinGroup} onClick={() => changeShuffle('withinGroup', !shuffle.withinGroup)}>组内乱序</button>
        <button type="button" aria-pressed={shuffle.betweenGroups} onClick={() => changeShuffle('betweenGroups', !shuffle.betweenGroups)}>组间乱序</button></>}
      <div className="study-progress-summary"><span>本页已通过 {progressText(visible, passed)}</span><span>{todayWords.length ? `今天已通过 ${progressText(todayWords, round.passed[today] || [])}` : '今日无任务'}</span></div>
      <span>{plan.method === 'hulu' ? `今日 ${original.length} 词 · 本页须通过 80%` : original.length > 0 && retries.length === 0 ? '今日已完成' : retry ? `第 ${retryCycle} 次重试 · ${retries.length} 词` : `今日 ${original.length} 词`}</span></div>
    {practice && <p className="study-date-note">按空格可按顺序逐词揭示词性和中文释义。</p>}
    {(today < round.startDate || today > round.reviewEndDate) && <p className="study-date-note">今天不在当前轮日期范围内，已显示最接近的一天。</p>}
    <div className="study-table"><div className="study-table-head"><span>单词</span><span>词性与中文释义</span><span>操作</span></div>
      {visible.map(id => { const word = wordById[id]; const answer = word?.core.map(sense => `${sense.partOfSpeech} ${sense.text}`).join('；') || '词条待完善';
        return <div className="study-row" key={id}><strong>{word?.lemma || lemmaById[id] || id}</strong>
          <button type="button" className={`study-answer ${practice && !shown[id] ? 'is-hidden' : ''}`} onClick={() => practice && setShown(current => ({ ...current, [id]: !current[id] }))}>
            {practice && !shown[id] ? '点击显示答案' : answer}</button>
          <div className="study-row-actions"><div className="level-switch">{(['unfamiliar', 'seen', 'familiar'] as const).map((value, index) => <button type="button" key={value} className={levels[id] === value ? 'selected' : ''} aria-pressed={levels[id] === value} onClick={() => onLevel(id, value)}>{['陌生', '见过', '熟悉'][index]}</button>)}</div>
          <button type="button" className="study-star" aria-pressed={!!favorites.words[id]} disabled={!word || (word.kind != null && word.kind !== 'entry')} onClick={() => onFavorite(id)}>★</button><button type="button" onClick={() => word && (word.kind == null || word.kind === 'entry') ? onOpenWord(id) : onOpenCandidate(lemmaById[id] || word?.lemma || id)}>详情</button>
            {practice && <label className="study-switch">{plan.method === 'hulu' ? '未通过' : '稍后重试'} <input type="checkbox" checked={pendingPass[id] ?? passed.includes(id)} onChange={event => { const checked = event.target.checked; setPendingPass(current => ({ ...current, [id]: checked }));
              void update(checked ? 'pass' : 'retry', { day, wordId: id }).finally(() => setPendingPass(current => { const next = { ...current }; delete next[id]; return next; })); }} /> 通过</label>}</div></div>; })}
      {!visible.length && <p className="study-empty">{retry ? '今日所有单词均已通过。' : '这一天没有安排单词。'}</p>}</div>
    <footer className="study-pagination"><button type="button" aria-label="上一组" disabled={page === 0 && !retry} onClick={previous}>◀</button><span>{page + 1} / {totalPages}{retry && plan.method !== 'hulu' ? ' · 重试' : ''}</span><button type="button" aria-label="下一组" disabled={plan.method !== 'hulu' && !retries.length && page + 1 >= totalPages} onClick={next}>▶</button></footer>
    {plan.roundComplete && <button type="button" className="primary-button" onClick={() => void update('nextRound')}>开始第 {round.number + 1} 轮</button>}
    {error && <p role="alert" className="error-note">{error}</p>}
  </div>;
}

export function StudyPage({ projects, words, levels, activeProjectId, favorites, pageSize, tabs, active, onTabsChange, onLevel, onFavorite, onOpenWord, onOpenCandidate }: {
  projects: Project[]; words: WordSummary[]; levels: Record<string, Level>; activeProjectId: string; favorites: FavoritesState; pageSize: number;
  tabs: string[]; active: string; onTabsChange: (tabs: string[], active: string) => void;
  onLevel: (id: string, level: Level) => void; onFavorite: (id: string) => void; onOpenWord: (id: string, projectId: string) => void; onOpenCandidate: (lemma: string, projectId: string) => void;
}) {
  const [plans, setPlans] = useState<StudyPlan[]>([]);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<StudyPlan | null>(null);
  const [deleting, setDeleting] = useState<StudyPlan | null>(null);
  const [today, setToday] = useState('');
  const [error, setError] = useState('');
  const [planLevels, setPlanLevels] = useState<Record<string, Level>>({});
  const mutationQueues = useRef<Record<string, Promise<StudyPlan>>>({});
  useEffect(() => { studyPlans().then(setPlans).catch(exc => setError(String(exc))); }, []);
  useEffect(() => {
    const refreshToday = () => { void studyToday().then(value => setToday(value.day)).catch(exc => setError(String(exc))); };
    refreshToday();
    window.addEventListener('focus', refreshToday);
    const timer = window.setInterval(refreshToday, 60000);
    return () => { window.removeEventListener('focus', refreshToday); window.clearInterval(timer); };
  }, []);
  const replace = (updated: StudyPlan) => setPlans(current => current.map(plan => plan.planId === updated.planId ? updated : plan));
  const patch = async (plan: StudyPlan, action: string, value: object = {}) => {
    const prior = mutationQueues.current[plan.planId];
    const next = (prior ? prior.catch(() => studyPlan(plan.planId)) : Promise.resolve(plan))
      .then(current => updateStudyPlan(current, action, value));
    mutationQueues.current[plan.planId] = next;
    try { const updated = await next; replace(updated); return updated; }
    catch (exc) { studyPlans().then(setPlans).catch(() => {}); throw exc; }
    finally { if (mutationQueues.current[plan.planId] === next) delete mutationQueues.current[plan.planId]; }
  };
  const open = (id: string) => onTabsChange(tabs.includes(id) ? tabs : [...tabs, id], id);
  const selected = plans.find(plan => plan.planId === active);
  useEffect(() => { if (!selected || selected.projectId === activeProjectId) return;
    projectLearning(selected.projectId).then(state => setPlanLevels(state.words)).catch(exc => setError(String(exc)));
  }, [selected?.projectId, activeProjectId]);
  const changePlanLevel = async (id: string, level: Level) => {
    if (!selected || selected.projectId === activeProjectId) { onLevel(id, level); return; }
    try { const state = await projectLearning(selected.projectId); const saved = await saveLevel(id, level, state.revision, selected.projectId); setPlanLevels(saved.words); }
    catch (exc) { setError(String(exc)); }
  };
  return <div className="study-page"><div className="study-tabs"><button type="button" className={active === 'home' ? 'active' : ''} onClick={() => onTabsChange(tabs, 'home')}>首页</button>
    {tabs.map(id => <div className={`study-tab ${active === id ? 'active' : ''}`} key={id}><button type="button" onClick={() => onTabsChange(tabs, id)}>{plans.find(plan => plan.planId === id)?.name || '背诵计划'}</button>
      <button type="button" aria-label="关闭计划标签" onClick={() => { onTabsChange(tabs.filter(item => item !== id), active === id ? 'home' : active); }}>×</button></div>)}</div>
    <div className="study-split"><section className="study-home"><header><div><span className="eyebrow">RECALL PLANS</span><h1>背单词</h1></div><button type="button" className="primary-button" disabled={!today} onClick={() => setCreating(true)}>新建背诵计划</button></header>
      {plans.map(plan => { const round = plan.rounds[plan.rounds.length - 1]; const progress = plan.progress ?? new Set(Object.values(round.passed).flat()).size;
        return <article className="study-plan-card" key={plan.planId}><button type="button" className="study-plan-open" onClick={() => open(plan.planId)}><strong>{plan.name}</strong><span>第 {round.number} 轮 · {projects.find(project => project.projectId === plan.projectId)?.name}</span>
          <small>创建时间：{fmt(plan.createdAt)}</small><span>当前进度：{progress} / {plan.wordIds.length}{plan.roundComplete ? ' · 本轮已完成' : ''}</span><progress max={plan.wordIds.length} value={progress} /></button>
          <div className="study-plan-card-actions"><button type="button" onClick={() => setEditing(plan)}>设置</button><button type="button" onClick={() => setDeleting(plan)}>删除</button></div></article>; })}
      {!plans.length && <p>暂无背诵计划。点击右上角创建。</p>}{error && <p role="alert" className="error-note">{error}</p>}</section>
      {selected && today && <aside className="study-detail"><PlanView key={selected.planId} plan={selected} today={today} projects={projects} words={words} levels={selected.projectId === activeProjectId ? levels : planLevels} favorites={favorites} pageSize={pageSize}
        onPatch={patch} onLevel={(id, level) => { void changePlanLevel(id, level); }} onFavorite={onFavorite} onOpenWord={id => onOpenWord(id, selected.projectId)} onOpenCandidate={lemma => onOpenCandidate(lemma, selected.projectId)} /></aside>}</div>
    {creating && today && <CreateDialog projects={projects} today={today} onClose={() => setCreating(false)} onSaved={plan => { setPlans(current => [...current, plan]); setCreating(false); open(plan.planId); }} />}
    {editing && today && <CreateDialog key={editing.planId} projects={projects} today={today} plan={editing} onClose={() => setEditing(null)} onSaved={plan => { replace(plan); setEditing(null); }} />}
    {deleting && <DeleteDialog plan={deleting} onClose={() => setDeleting(null)} onDelete={async () => {
      await mutationQueues.current[deleting.planId]?.catch(() => undefined);
      await deleteStudyPlan(await studyPlan(deleting.planId));
      setPlans(current => current.filter(plan => plan.planId !== deleting.planId));
      onTabsChange(tabs.filter(id => id !== deleting.planId), active === deleting.planId ? 'home' : active);
      setDeleting(null);
    }} />}
  </div>;
}

export function StudyCalendarPage({ projects, words }: { projects: Project[]; words: WordSummary[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const todayRef = useRef<HTMLButtonElement>(null);
  const positioned = useRef(false);
  const [today, setToday] = useState('');
  const [plansLoaded, setPlansLoaded] = useState(false);
  const [plans, setPlans] = useState<StudyPlan[]>([]);
  const [selected, setSelected] = useState('');
  const [details, setDetails] = useState<StudyDay[]>([]);
  const [error, setError] = useState('');
  useEffect(() => { studyToday().then(value => { setToday(value.day); setSelected(current => current || value.day); }).catch(exc => setError(String(exc))); }, []);
  useEffect(() => { studyPlans().then(setPlans).catch(exc => setError(String(exc))).finally(() => setPlansLoaded(true)); }, []);
  useEffect(() => { if (selected) studyDate(selected).then(setDetails).catch(exc => setError(String(exc))); }, [selected]);
  useEffect(() => {
    if (!today || !plansLoaded || positioned.current) return;
    const frame = window.requestAnimationFrame(() => {
      const container = scrollRef.current, day = todayRef.current;
      if (!container || !day) return;
      container.scrollTop += day.getBoundingClientRect().top - container.getBoundingClientRect().top - container.clientHeight / 2 + day.offsetHeight / 2;
      positioned.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [today, plansLoaded]);
  if (!today) return <div className="study-calendar-page">{error ? <p role="alert" className="error-note">{error}</p> : <p>正在载入日历…</p>}</div>;
  const planMonths = plans.flatMap(plan => plan.rounds.flatMap(round => [round.startDate, round.reviewEndDate].map(value => {
    const parsed = new Date(`${value}T12:00:00`); return parsed.getFullYear() * 12 + parsed.getMonth();
  })));
  const currentDate = new Date(`${today}T12:00:00`);
  const currentMonth = currentDate.getFullYear() * 12 + currentDate.getMonth();
  const firstMonth = Math.min(currentMonth - 12, ...planMonths);
  const lastMonth = Math.max(currentMonth + 12, ...planMonths);
  const months = Array.from({ length: lastMonth - firstMonth + 1 }, (_, index) => {
    const number = firstMonth + index; return new Date(Math.floor(number / 12), number % 12, 1);
  });
  const covered = new Set(plans.flatMap(plan => plan.rounds.flatMap(round => plan.schedule.days.map(day => addDays(round.startDate, day.day - 1)).filter(day => day <= round.reviewEndDate))));
  const wordById = Object.fromEntries([...projects.flatMap(project => project.words), ...words.map(word => ({ wordId: word.wordId, lemma: word.lemma }))].map(word => [word.wordId, word.lemma]));
  return <div className="study-calendar-page"><div className="calendar-scroll" ref={scrollRef}>{months.map(month => { const year = month.getFullYear(), number = month.getMonth();
    const offset = (month.getDay() + 6) % 7, count = new Date(year, number + 1, 0).getDate();
    return <div className="calendar-month" key={`${year}-${number}`}><h2>{year} 年 {number + 1} 月</h2><div className="calendar-grid">
      {['一', '二', '三', '四', '五', '六', '日'].map(label => <strong key={label}>{label}</strong>)}
      {Array.from({ length: offset }, (_, index) => <span key={`empty-${index}`} />)}
      {Array.from({ length: count }, (_, index) => { const day = localDate(new Date(year, number, index + 1)); return <button type="button" key={day} aria-label={day} ref={day === today ? todayRef : undefined}
        className={`${covered.has(day) ? 'has-plan' : ''} ${selected === day ? 'selected' : ''}`} onClick={() => setSelected(day)}>{index + 1}</button>; })}</div></div>; })}</div>
    <aside className="study-calendar-details"><h2>{selected}</h2>{details.map(item => <section key={item.planId}><h3>{item.name} · 第 {item.round} 轮</h3>
      <p>项目：{projects.find(project => project.projectId === item.projectId)?.name}</p>
      <p>首次学习：{item.newWordIds.map(id => wordById[id] || id).join('、') || '无'}</p>{item.method !== 'hulu' && <p>复习：{item.reviewWordIds.map(id => wordById[id] || id).join('、') || '无'}</p>}
      <p>当日单词：{item.wordIds.map(id => wordById[id] || id).join('、') || '无'}</p></section>)}
      {!details.length && <p>当日没有背诵计划。</p>}{error && <p role="alert" className="error-note">{error}</p>}</aside></div>;
}
