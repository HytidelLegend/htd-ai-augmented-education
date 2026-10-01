import { useEffect, useState } from 'react';
import { Volume2 } from 'lucide-react';
import type { ContentItem, Level, Relation, Sense, WordPage } from './model';
import { relationLabels } from './model';

function marked(text: string, spans: { start: number; end: number }[] = []) {
  const chars = Array.from(text);
  if (!spans.length) return text;
  const fragments: React.ReactNode[] = [];
  let cursor = 0;
  spans.slice().sort((a, b) => a.start - b.start).forEach((span, index) => {
    if (span.start > cursor) fragments.push(chars.slice(cursor, span.start).join(''));
    fragments.push(<strong key={index}>{chars.slice(span.start, span.end).join('')}</strong>);
    cursor = span.end;
  });
  if (cursor < chars.length) fragments.push(chars.slice(cursor).join(''));
  return fragments;
}

function SourceBadge({ item }: { item: ContentItem }) {
  const status: Record<string, string> = { pending: '待核验', automatic_passed: '自动通过',
    agent_passed: '审查通过', agent_reviewed: '审查通过', human_passed: '人工通过', rejected: '已驳回' };
  return <span className={`source-badge ${item.generationMethod === 'ai_generated' ? 'ai' : ''}`}>
    {item.generationMethod === 'rule_derived' ? '规则推导 · 待核验' :
      <>{item.generationMethod === 'ai_generated' ? `AI 生成 · 置信度 ${item.confidence?.toFixed(2) ?? '—'}` : '来源支持'}
        {' · ' + (status[item.verificationStatus] || item.verificationStatus)}</>}
  </span>;
}

function Sources({ item }: { item: ContentItem }) {
  return item.sourceRefs?.length ? <details className="sources"><summary>查看来源 · {item.sourceRefs.length}</summary><div>
    {item.sourceRefs.map((source, index) => <a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer">{({ cambridge: '剑桥词典', oxford: '牛津学习词典', longman: '朗文词典', thesaurus: '同义词词典' } as Record<string, string>)[source.site] || source.site}<small>{source.summary}</small></a>)}
  </div></details> : null;
}

function Usage({ item, kind }: { item: ContentItem; kind: 'example' | 'collocation' | 'phrase' }) {
  return <div className={`usage usage-${kind}`}><div className="usage-main">
    <span className="usage-en">{kind === 'example' ? marked(item.text, item.emphasis?.en) : item.text}</span>
    <span className="usage-zh">{item.translationZh ? kind === 'example' ? marked(item.translationZh.text, item.emphasis?.zh) : item.translationZh.text : '译文待完善'}</span>
  </div><div className="usage-meta"><SourceBadge item={item} /><Sources item={item} /></div></div>;
}

function SenseCard({ sense, index, core }: { sense: Sense; index: number; core: boolean }) {
  const posLabels: Record<string, string> = { adjective: '形容词', adverb: '副词', noun: '名词', verb: '动词', pronoun: '代词', preposition: '介词', conjunction: '连词', interjection: '感叹词', determiner: '限定词' };
  const sameDefinitionStatus = sense.definitionZh.generationMethod === sense.definitionEn.generationMethod &&
    sense.definitionZh.verificationStatus === sense.definitionEn.verificationStatus &&
    sense.definitionZh.confidence === sense.definitionEn.confidence;
  const sameDefinitionSources = JSON.stringify(sense.definitionZh.sourceRefs || []) ===
    JSON.stringify(sense.definitionEn.sourceRefs || []);
  return <article className="sense-card" id={sense.senseId}>
    <div className="sense-head"><span className="sense-number">{String(index + 1).padStart(2, '0')}</span><span className="pos">{posLabels[sense.partOfSpeech] ? `${posLabels[sense.partOfSpeech]} ${sense.partOfSpeech}` : sense.partOfSpeech}</span>
      {core && <span className="core-label">核心释义</span>}{core && sense.definitionZh.verificationStatus === 'pending' && <span className="pending-label">待核验</span>}</div>
    <h3>{sense.definitionZh.text}</h3><p className="definition-en">{sense.definitionEn.text}</p>
    <div className="sense-badges">{[...sense.grammarTags, ...sense.registerTags].map(tag => <span key={tag.itemId} className="tag-group"><span className="tag">{tag.text}</span><SourceBadge item={tag} /><Sources item={tag} /></span>)}
      {sameDefinitionStatus && sameDefinitionSources ? <span className="definition-source">中英释义 <SourceBadge item={sense.definitionZh} /><Sources item={sense.definitionZh} /></span> : <>
        <span className="definition-source">中译 <SourceBadge item={sense.definitionZh} /><Sources item={sense.definitionZh} /></span>
        <span className="definition-source">英释 <SourceBadge item={sense.definitionEn} /><Sources item={sense.definitionEn} /></span></>}</div>
    {sense.examples.length > 0 && <div className="sense-section"><h4>例句 <small>EXAMPLES</small></h4>{sense.examples.map(item => <Usage key={item.itemId} item={item} kind="example" />)}</div>}
    {sense.collocations.length > 0 && <div className="sense-section"><h4>搭配 <small>COLLOCATIONS</small></h4>{sense.collocations.map(item => <Usage key={item.itemId} item={item} kind="collocation" />)}</div>}
    {sense.phrases.length > 0 && <div className="sense-section"><h4>短语与习语 <small>PHRASES</small></h4>{sense.phrases.map(item => <Usage key={item.itemId} item={item} kind="phrase" />)}</div>}
  </article>;
}

type Props = { page: WordPage; level: Level; favorited: boolean;
  onLevel: (level: Level) => void; onFavorite: () => void;
  onOpenWord: (id: string) => void; onOpenCandidate: (lemma: string) => void;
  onAddToStage: () => void };

export default function WordView({ page, level, favorited, onLevel, onFavorite, onOpenWord, onOpenCandidate, onAddToStage }: Props) {
  const { entry, coreSenses, moreSenses } = page;
  const familyReview = (field: 'inflections' | 'derivatives') => page.familyReviews?.find(review => review.field === field);
  const reviewNote = (field: 'inflections' | 'derivatives') => {
    const review = familyReview(field);
    if (!review) return null;
    const hasPendingRules = field === 'inflections' && entry.inflections.some(
      form => form.form.generationMethod === 'rule_derived');
    return <p className="progress-note" title={`Agent 审核 ${review.reviewRef.reviewedAt} · 运行 ${review.reviewRef.runId}`}>
      {review.outcome === 'no_supported_candidate'
        ? hasPendingRules ? '已复核直接来源 · 规则推导词形待核验' : '已复核现有证据 · 暂无可发布内容'
        : '自动通过 · Agent 已审核 · 已发布来源支持的候选'}
    </p>;
  };
  const [audioAvailable, setAudioAvailable] = useState<Record<string, boolean>>({});
  useEffect(() => {
    let active = true;
    setAudioAvailable({});
    for (const variety of ['uk', 'us']) {
      if (!entry.pronunciations.some(item => item.variety === variety)) continue;
      fetch(`/api/audio/${entry.wordId}/${variety}?check=1`).then(response => {
        if (active) setAudioAvailable(current => ({ ...current, [variety]: response.ok }));
      }).catch(() => { if (active) setAudioAvailable(current => ({ ...current, [variety]: false })); });
    }
    return () => { active = false; };
  }, [entry.wordId, entry.pronunciations]);
  const play = (variety: string) => {
    const audio = new Audio(`/api/audio/${entry.wordId}/${variety}`);
    audio.onerror = () => setAudioAvailable(current => ({ ...current, [variety]: false }));
    audio.play().catch(() => setAudioAvailable(current => ({ ...current, [variety]: false })));
  };
  const relations = (items: Relation[]) => items.map((relation, index) => <button key={relation.relationshipId || relation.candidateId || index}
    type="button" className="relation-pill" onClick={() => relation.targetWordId ? onOpenWord(relation.targetWordId) : relation.targetLemma && onOpenCandidate(relation.targetLemma)}>
    <span className={`relation-mark relation-${relation.proposedType === 'synonym_or_near_synonym' ? 'near_synonym' : relation.type || relation.proposedType}`} />
    <span><strong>{relation.targetLemma || relation.targetWordId || '待采集'}</strong><small>{relation.proposedType === 'synonym_or_near_synonym' ? '近义词 · 按规则分类' : relation.type && relation.type in relationLabels ? relationLabels[relation.type] : relation.proposedType || '候选关系'}{relation.candidateId ? ' · 待核验' : ''}{relation.type === 'spelling_similar' ? ' · 词条级关系' : ''}{!relation.targetWordId ? ' · 待采集' : ''}</small></span><span aria-hidden="true">↗</span>
  </button>);
  return <div className="word-page">
    <div className="word-head"><h1>{entry.lemma}</h1><div className="word-head-actions"><button type="button" className={`favorite-button ${favorited ? 'is-favorite' : ''}`} aria-label={favorited ? '取消收藏' : '加入收藏夹'} aria-pressed={favorited} onClick={onFavorite}>★</button>
      <div className="learning-bar"><span>我的熟悉度</span><div className="level-switch">{(['unfamiliar', 'seen', 'familiar'] as const).map((value, index) => <button key={value} type="button" aria-pressed={level === value} className={level === value ? 'selected' : ''} onClick={() => onLevel(value)}>{['陌生', '见过', '熟悉'][index]}</button>)}</div></div></div></div>
    <div className="pronunciations">{entry.pronunciations.map(item => <div key={item.pronunciationId} className="pronunciation"><span className="variety">{item.variety === 'uk' ? '英' : item.variety === 'us' ? '美' : '其他'}</span>
      <span className="ipa">/{item.ipa.text}/</span>{audioAvailable[item.variety] && <button type="button" className="audio-button" onClick={() => play(item.variety)} aria-label={`播放${item.variety === 'uk' ? '英式' : '美式'}发音`} title="播放发音"><Volume2 size={15} /></button>}{item.partOfSpeech && <span className="muted">{item.partOfSpeech}</span>}<SourceBadge item={item.ipa} /><Sources item={item.ipa} /></div>)}</div>
    {page.outsideStage && <div className="notice-line">此词尚未加入当前学龄段。<button type="button" onClick={onAddToStage}>加入当前学龄段</button></div>}
    <section className="page-section"><div className="section-heading"><h2>核心释义</h2><small>FIRST TO KNOW</small></div>
      {coreSenses.length ? coreSenses.map((sense, index) => <SenseCard key={sense.senseId} sense={sense} index={index} core />) : <div className="empty-content">暂无已核实的核心释义。</div>}</section>
    {moreSenses.length > 0 && <section className="page-section"><div className="section-heading"><h2>更多释义</h2><small>MORE MEANINGS</small></div>
      {moreSenses.map((sense, index) => <SenseCard key={sense.senseId} sense={sense} index={coreSenses.length + index} core={false} />)}</section>}
    {(entry.inflections.length > 0 || familyReview('inflections')) && <section className="page-section"><div className="section-heading"><h2>词形变化</h2><small>FORMS</small></div>{reviewNote('inflections')}<div className="form-grid">
      {entry.inflections.map(form => <button type="button" className="relation-pill" key={form.formId} onClick={() => form.targetWordId ? onOpenWord(form.targetWordId) : onOpenCandidate(form.form.text)}><span className="relation-mark relation-family"/><span><strong>{form.form.text}</strong><small>{form.kind} · {form.form.generationMethod === 'rule_derived' ? '规则推导 · 待核验 · ' : form.form.verificationStatus === 'automatic_passed' ? '自动通过 · ' : ''}{form.targetWordId ? '打开词条' : '待建词条'}</small></span><span aria-hidden="true">↗</span></button>)}</div></section>}
    {(entry.derivatives.length > 0 || familyReview('derivatives')) && <section className="page-section"><div className="section-heading"><h2>派生词</h2><small>WORD FAMILY</small></div>{reviewNote('derivatives')}<div className="relation-grid derivative-grid">
      {entry.derivatives.map(item => <button key={item.derivativeId} type="button" className="relation-pill" onClick={() => item.targetWordId ? onOpenWord(item.targetWordId) : onOpenCandidate(item.word)}><span className="relation-mark relation-family"/><span><strong>{item.word}</strong><small>{familyReview('derivatives')?.outcome === 'published' && item.verificationStatus === 'agent_reviewed' ? '自动通过 · Agent 已审核 · ' : ''}{item.status === 'linked' ? '打开词条' : '待采集'}</small></span></button>)}</div></section>}
    {(page.relationships.length > 0 || page.pendingRelations.length > 0) && <section className="page-section"><div className="section-heading"><h2>关联词</h2><small>CONNECTIONS</small></div>
      {(['synonym', 'near_synonym', 'antonym', 'spelling_similar'] as const).map(kind => { const group = [...page.relationships, ...page.pendingRelations].filter(item => (item.proposedType === 'synonym_or_near_synonym' ? 'near_synonym' : item.type || item.proposedType) === kind).sort((a, b) => (a.targetLemma || '').localeCompare(b.targetLemma || '', 'en')); return group.length ? <div className="relation-group" key={kind}><h3>{relationLabels[kind]}</h3><div className="relation-grid">{relations(group)}</div></div> : null; })}</section>}
  </div>;
}
