import { useEffect, useRef, useState } from 'react';
import { loadConfig, saveConfig } from './api';
import type { Theme } from './model';

const groups: { key: keyof Theme; label: string }[] = [
  { key: 'ui', label: '界面颜色' }, { key: 'learning', label: '学习状态颜色' },
  { key: 'graph', label: '词汇图谱' }, { key: 'controls', label: '控件颜色' },
  { key: 'favorites', label: '收藏' }, { key: 'study', label: '背单词' },
];
const labels: Record<string, string> = {
  initializeOnEnter: '进入图谱时初始化', wordsPerPage: '每页单词数', springStrength: '弹簧强度', repulsionStrength: '斥力强度',
  familyNonMemberRepulsionStrength: '非同族斥力强度', damping: '阻尼', restLength: '目标边长', familyNodeGap: '同族节点间距（画布像素）',
  family: '同族词', synonym: '同义词', near_synonym: '近义词', antonym: '反义词',
  spelling_similar: '拼写相似词', unfamiliar: '陌生', seen: '见过', familiar: '熟悉',
  outsideStage: '学龄段外', star: '收藏星', background: '背景', surface: '表面',
  surfaceRaised: '凸起表面', text: '正文', mutedText: '次要文字', border: '边框',
  hover: '悬停', selected: '选中', disabled: '禁用', error: '错误', success: '成功',
  activeText: '激活文字', inactiveText: '未激活文字', focus: '焦点', sliderTrack: '滑轨',
  relations: '关系颜色', physics: '力学参数',
};

function fields(value: Record<string, unknown>, prefix: string[] = []): { path: string[]; value: string | number | boolean }[] {
  return Object.entries(value).flatMap(([key, item]) => {
    if (key === 'schemaVersion') return [];
    const path = [...prefix, key];
    return item && typeof item === 'object' ? fields(item as Record<string, unknown>, path) :
      typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean' ? [{ path, value: item }] : [];
  });
}

export default function SettingsPage({ stages, activeStageId, onStageChange, onApplied }: {
  stages: { stageId: string; label: string }[]; activeStageId: string;
  onStageChange: (id: string) => void; onApplied: (config: Theme) => void;
}) {
  const [draft, setDraft] = useState<Theme | null>(null);
  const [revision, setRevision] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const edited = useRef(false);
  const reload = async () => {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await loadConfig();
      edited.current = false;
      setDraft(result.config); setRevision(result.revision);
      setMessage('已重新加载文件中的配置');
    } catch (exc) { setError(String(exc)); }
    finally { setBusy(false); }
  };
  useEffect(() => { let active = true;
    loadConfig().then(result => { if (active && !edited.current) { setDraft(result.config); setRevision(result.revision); } })
      .catch(exc => { if (active) setError(String(exc)); });
    return () => { active = false; };
  }, []);
  const change = (path: string[], value: string | number | boolean) => { edited.current = true; setDraft(current => {
    if (!current) return current;
    const next = structuredClone(current);
    let node: Record<string, unknown> = next as unknown as Record<string, unknown>;
    for (const key of path.slice(0, -1)) node = node[key] as Record<string, unknown>;
    node[path[path.length - 1]] = value;
    setMessage('');
    return next;
  }); };
  const save = async () => {
    if (!draft) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await saveConfig(draft, revision);
      edited.current = false;
      setDraft(result.config); setRevision(result.revision); onApplied(result.config);
      setMessage('配置已保存并应用');
    } catch (exc) { setError(String(exc)); }
    finally { setBusy(false); }
  };
  return <div className="utility-page settings-page"><span className="eyebrow">PREFERENCES</span><h1>设置</h1>
    <div className="settings-card"><label htmlFor="stage-select">当前学龄段</label><select id="stage-select" value={activeStageId} onChange={event => onStageChange(event.target.value)}>
      {stages.map(stage => <option key={stage.stageId} value={stage.stageId}>{stage.label}</option>)}</select>
      <p>正式词表由你提供后才会出现在这里；测试选项动态包含已入库词条。</p></div>
    {draft && groups.map(group => <section className="settings-card" key={group.key}><h2>{group.label}</h2>
      <div className="settings-grid">{fields(draft[group.key] as Record<string, unknown>, [group.key]).map(field => {
        const key = field.path.join('.'); const name = field.path.slice(1).map(part => labels[part] || part).join(' / ');
        return <label key={key}>{name}{typeof field.value === 'boolean' ?
          <input type="checkbox" checked={field.value} onChange={event => change(field.path, event.target.checked)} /> : typeof field.value === 'number' ?
          <input type="number" step="any" value={field.value} onChange={event => change(field.path, Number(event.target.value))} /> :
          <span className="settings-color"><input type="color" value={field.value} onChange={event => change(field.path, event.target.value.toUpperCase())} />
            <input type="text" value={field.value} pattern="#[0-9A-Fa-f]{6}" onChange={event => change(field.path, event.target.value)} /></span>}</label>;
      })}</div></section>)}
    <button type="button" className="primary-button" disabled={!draft || busy} onClick={() => void save()}>{busy ? '保存中…' : '保存配置'}</button>
    {error && <button type="button" className="settings-reload" disabled={busy} onClick={() => void reload()}>重新加载配置</button>}
    {message && <p role="status">{message}</p>}{error && <p className="error-note" role="alert">{error}</p>}
  </div>;
}
