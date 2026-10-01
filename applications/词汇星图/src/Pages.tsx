import { useEffect, useRef, useState } from 'react';
import { searchDictionary, studyPlans } from './api';
import type { Project, SearchHit } from './model';

export function ProjectsPage({ projects, activeId, onSelect, onCreate, onSave, onDelete }: {
  projects: Project[]; activeId: string; onSelect: (id: string) => void;
  onCreate: (name: string, content: string, format: string) => Promise<void>;
  onSave: (project: Project, name: string, content: string, replan: boolean) => Promise<void>;
  onDelete: (project: Project) => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [mode, setMode] = useState<'create' | 'edit' | 'replan' | 'delete' | null>(null);
  const [editing, setEditing] = useState<Project | null>(null);
  const [preview, setPreview] = useState<Project | null>(null);
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [planCount, setPlanCount] = useState<Record<string, number>>({});
  useEffect(() => { setPreview(current => current ? projects.find(project => project.projectId === current.projectId) || null : null); }, [projects]);
  useEffect(() => { studyPlans().then(plans => setPlanCount(plans.reduce<Record<string, number>>((counts, plan) => {
    counts[plan.projectId] = (counts[plan.projectId] || 0) + 1; return counts;
  }, {}))).catch(exc => setError(String(exc))); }, []);
  useEffect(() => { if (mode) dialogRef.current?.showModal(); }, [mode]);
  const close = () => { dialogRef.current?.close(); setMode(null); setError(''); };
  const openCreate = () => { setEditing(null); setName(''); setContent(''); setFile(null); setError(''); setMode('create'); };
  const openEdit = (project: Project) => { setEditing(project); setName(project.name); setContent(project.words.map(row => row.lemma).join('\n')); setError(''); setMode('edit'); };
  const save = async (replan: boolean) => {
    if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try {
      if (mode === 'create') {
        const format = file?.name.toLowerCase().endsWith('.jsonl') ? 'jsonl' : file?.name.toLowerCase().endsWith('.json') ? 'json' : 'text';
        await onCreate(name.trim(), file ? await file.text() : content, format);
      } else if (editing) {
        const normalize = (value: string) => value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase();
        const before = new Set(editing.words.map(row => normalize(row.lemma)));
        const after = new Set(content.split(/\r?\n/).map(normalize).filter(Boolean));
        if (mode === 'edit' && planCount[editing.projectId] && (before.size !== after.size || [...before].some(word => !after.has(word)))) {
          dialogRef.current?.close(); setMode('replan'); return;
        }
        await onSave(editing, name.trim(), content, replan);
      }
      close();
    } catch (exc) { setError(String(exc)); }
    finally { setBusy(false); }
  };
  const remove = async () => { if (!editing || busy) return; setBusy(true); setError('');
    try { await onDelete(editing); setPreview(null); close(); } catch (exc) { setError(String(exc)); } finally { setBusy(false); }
  };
  return <section className="section-page projects-page"><header className="projects-heading"><div><span className="eyebrow">YOUR WORD LISTS</span><h1>项目</h1></div><button type="button" className="primary-button" onClick={openCreate}>新建项目</button></header>
    <p>每个项目对应一份单词表。词条与收藏跨项目共用，熟悉程度由各项目单独记录。</p>
    <div className="projects-layout"><div className="project-grid">{projects.map(project => <article key={project.projectId}
      className={`project-card ${activeId === project.projectId ? 'selected' : ''}`}>
      <button type="button" className="project-settings" aria-label={`设置${project.name}`} onClick={() => openEdit(project)}>设置</button>
      <strong>{project.name}</strong><span>{project.words.length} 个单词</span>
      <button type="button" className="project-preview" onClick={() => setPreview(project)}>预览</button>
      <button type="button" className="project-open" onClick={() => onSelect(project.projectId)}>{activeId === project.projectId ? '打开当前项目的词汇星图 ↗' : '切换并打开词汇星图 ↗'}</button>
    </article>)}</div>
    {preview && <aside className="project-preview-pane"><header><h2>{preview.name} · 单词列表</h2><button type="button" onClick={() => setPreview(null)} aria-label="关闭预览">×</button></header><p>{preview.words.length} 个单词</p><div>{preview.words.map((row, index) => <span key={row.wordId}>{index + 1}. {row.lemma}</span>)}</div></aside>}</div>
    {mode && <dialog ref={dialogRef} className="project-modal" onCancel={event => { event.preventDefault(); close(); }}>
      <header><h2>{mode === 'create' ? '新建项目' : mode === 'edit' ? `设置 · ${editing?.name}` : mode === 'replan' ? '词表变更与背诵计划' : '确认删除项目'}</h2><button type="button" onClick={close} aria-label="关闭">×</button></header>
      {mode === 'create' && <><label>项目名<input value={name} onChange={event => setName(event.target.value)} /></label><label>导入词表（TXT、JSON、JSONL）<input type="file" accept=".txt,.json,.jsonl" onChange={event => setFile(event.target.files?.[0] || null)} /></label><label>或每行输入一个英文词<textarea value={content} onChange={event => setContent(event.target.value)} disabled={!!file} /></label><footer><button type="button" onClick={close}>取消</button><button type="button" className="primary-button" disabled={!name.trim() || busy} onClick={() => void save(false)}>创建项目</button></footer></>}
      {mode === 'edit' && <><label>项目名<input value={name} onChange={event => setName(event.target.value)} /></label><label>单词数<input value={new Set(content.split(/\r?\n/).map(word => word.normalize('NFKC').trim().toLowerCase()).filter(Boolean)).size} readOnly /></label><label>单词列表（每行一个英文词）<textarea value={content} onChange={event => setContent(event.target.value)} /></label><footer><button type="button" className="danger-button" disabled={!!planCount[editing?.projectId || ''] || projects.length <= 1} onClick={() => { dialogRef.current?.close(); setMode('delete'); }}>删除</button><button type="button" className="primary-button" disabled={!name.trim() || busy} onClick={() => void save(false)}>保存</button></footer>{!!planCount[editing?.projectId || ''] && <p>该项目有 {planCount[editing?.projectId || '']} 个背诵计划；请先处理计划后再删除。</p>}</>}
      {mode === 'replan' && <><p>词表内容已变化。此项目的 {planCount[editing?.projectId || ''] || 0} 个背诵计划是否重新生成排程？重新生成会清空这些计划的通过记录。</p><footer><button type="button" onClick={() => void save(false)} disabled={busy}>保留原排程</button><button type="button" className="primary-button" onClick={() => void save(true)} disabled={busy}>重新生成排程</button></footer></>}
      {mode === 'delete' && <><p>确定删除项目“{editing?.name}”吗？单词笔记会保留在本地。</p><footer><button type="button" onClick={close}>取消</button><button type="button" className="danger-button" onClick={() => void remove()} disabled={busy}>确认删除</button></footer></>}
      {error && <p role="alert" className="error-note">{error}</p>}
    </dialog>}
  </section>;
}

export function DictionaryPage({ activeProjectId, onOpenWord, onOpenCandidate }: {
  activeProjectId: string; onOpenWord: (id: string) => void; onOpenCandidate: (lemma: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let live = true;
    setLoading(!!query.trim());
    setHits([]);
    const timer = window.setTimeout(() => {
      if (!query.trim()) { setError(''); return; }
      searchDictionary(query, activeProjectId).then(result => { if (live) { setHits(result); setError(''); setLoading(false); } })
        .catch(exc => { if (live) { setError(String(exc)); setLoading(false); } });
    }, 180);
    return () => { live = false; window.clearTimeout(timer); };
  }, [query, activeProjectId]);
  return <section className="section-page dictionary-page"><span className="eyebrow">WORD INDEX</span><h1>字典</h1>
    <label className="dictionary-search">搜索所有项目及已建词条<input value={query} onChange={event => setQuery(event.target.value)} placeholder="输入英文单词…" autoFocus /></label>
    {error && <p role="alert" className="error-note">{error}</p>}
    {loading && <p role="status">搜索中…</p>}
    <div className="dictionary-results">{hits.map(hit => <button type="button" key={hit.wordId} onClick={() => hit.built ? onOpenWord(hit.wordId) : onOpenCandidate(hit.lemma)}>
      <strong>{hit.lemma}</strong><span>{hit.projects.length ? hit.projects.map(project => project.name).join(' · ') : '未加入项目'}</span><small>{hit.built ? '查看词条 ↗' : '待建词条 ↗'}</small>
    </button>)}{query.trim() && !loading && !hits.length && !error && <p>没有找到匹配的单词。</p>}</div>
  </section>;
}

export function CalendarPage() {
  const today = new Date();
  const months = Array.from({ length: 25 }, (_, index) => new Date(today.getFullYear(), today.getMonth() + index - 12, 1));
  return <section className="section-page calendar-page"><span className="eyebrow">CALENDAR</span><h1>日历</h1>
    <div className="calendar-scroll">{months.map(month => {
      const year = month.getFullYear(), number = month.getMonth();
      const offset = (month.getDay() + 6) % 7;
      const days = new Date(year, number + 1, 0).getDate();
      return <div className="calendar-month" key={`${year}-${number}`}><h2>{year} 年 {number + 1} 月</h2>
        <div className="calendar-grid">{['一', '二', '三', '四', '五', '六', '日'].map(day => <strong key={day}>{day}</strong>)}
          {Array.from({ length: offset }, (_, index) => <span key={`empty-${index}`} />)}
          {Array.from({ length: days }, (_, index) => <span className={year === today.getFullYear() && number === today.getMonth() && index + 1 === today.getDate() ? 'today' : ''} key={index}>{index + 1}</span>)}
        </div></div>;
    })}</div>
  </section>;
}
