import { useEffect, useState } from 'react';
import { searchDictionary } from './api';
import type { Project, SearchHit } from './model';

export function ProjectsPage({ projects, activeId, onSelect, onCreate }: {
  projects: Project[]; activeId: string; onSelect: (id: string) => void;
  onCreate: (name: string, content: string, format: string) => Promise<void>;
}) {
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = projects.find(project => project.projectId === activeId);
  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try {
      const format = file?.name.toLowerCase().endsWith('.jsonl') ? 'jsonl' : file?.name.toLowerCase().endsWith('.json') ? 'json' : 'text';
      await onCreate(name.trim(), file ? await file.text() : '', format);
      setName(''); setFile(null);
    } catch (exc) { setError(String(exc)); }
    finally { setBusy(false); }
  };
  return <section className="section-page projects-page"><span className="eyebrow">YOUR WORD LISTS</span><h1>项目</h1>
    <p>每个项目对应一份单词表。词条与收藏跨项目共用，熟悉程度由各项目单独记录。</p>
    <div className="project-grid">{projects.map(project => <button type="button" key={project.projectId}
      className={`project-card ${activeId === project.projectId ? 'selected' : ''}`} onClick={() => onSelect(project.projectId)}>
      <strong>{project.name}</strong><span>{project.words.length} 个单词</span><small>{activeId === project.projectId ? '当前项目' : '切换并打开词汇星图 ↗'}</small>
    </button>)}</div>
    {active && <div className="project-word-list"><h2>{active.name}的单词表</h2><p>共 {active.words.length} 个单词</p>
      <div>{active.words.map((row, index) => <span key={row.wordId}>{index + 1}. {row.lemma}</span>)}</div></div>}
    <div className="project-create"><h2>新建项目</h2><label>项目名称<input value={name} onChange={event => setName(event.target.value)} placeholder="例如：四级词汇" /></label>
      <label>导入词表（可选，支持 TXT、JSON、JSONL）<input type="file" accept=".txt,.json,.jsonl" onChange={event => setFile(event.target.files?.[0] || null)} /></label>
      <button type="button" className="primary-button" disabled={!name.trim() || busy} onClick={create}>{busy ? '创建中…' : '创建项目'}</button>
      {error && <p role="alert" className="error-note">{error}</p>}</div>
    <div className="placeholder-note">背单词页将在后续版本关联项目词表。</div>
  </section>;
}

export function DictionaryPage({ onOpenWord, onOpenCandidate }: {
  onOpenWord: (id: string) => void; onOpenCandidate: (lemma: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    const timer = window.setTimeout(() => {
      if (!query.trim()) { setHits([]); return; }
      searchDictionary(query).then(result => { if (live) { setHits(result); setError(''); } })
        .catch(exc => { if (live) setError(String(exc)); });
    }, 180);
    return () => { live = false; window.clearTimeout(timer); };
  }, [query]);
  return <section className="section-page dictionary-page"><span className="eyebrow">WORD INDEX</span><h1>字典</h1>
    <label className="dictionary-search">搜索已有项目词表中的单词<input value={query} onChange={event => setQuery(event.target.value)} placeholder="输入英文单词…" autoFocus /></label>
    {error && <p role="alert" className="error-note">{error}</p>}
    <div className="dictionary-results">{hits.map(hit => <button type="button" key={hit.wordId} onClick={() => hit.built ? onOpenWord(hit.wordId) : onOpenCandidate(hit.lemma)}>
      <strong>{hit.lemma}</strong><span>{hit.projects.map(project => project.name).join(' · ')}</span><small>{hit.built ? '查看词条 ↗' : '待建词条 ↗'}</small>
    </button>)}{query.trim() && !hits.length && !error && <p>没有找到匹配的单词。</p>}</div>
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
