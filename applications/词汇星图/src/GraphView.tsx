import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent } from 'react';
import type { GraphData, Level, RelationType, Theme, WordSummary } from './model';
import { relationLabels, relationTypes } from './model';
type GraphEdge = GraphData['edges'][number];

type Props = {
  data: GraphData | null; theme: Theme; selected: string | null;
  zoom: number; positions: Record<string, { x: number; y: number }>;
  visible: Record<RelationType, boolean>; levels: Record<string, Level>;
  search: string; showOthers: boolean;
  showOutside: boolean; stageId: string; onShowOutside: () => void;
  onSearch: (value: string) => void; onSelect: (id: string | null) => void;
  onOpenCandidate: (lemma: string) => void;
  onToggle: (kind: RelationType) => void; onShowOthers: () => void;
  onZoom: (value: number) => void; onPositions: (positions: Record<string, { x: number; y: number }>) => void;
};

const clampZoom = (value: number) => Math.min(400, Math.max(25, Math.round(value / 5) * 5));
const point = (cx: number, cy: number, radius: number, angle: number) => ({ x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius });
const arc = (cx: number, cy: number, radius: number, start: number, end: number) => {
  const a = point(cx, cy, radius, start), b = point(cx, cy, radius, end);
  return `M ${a.x} ${a.y} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${b.x} ${b.y}`;
};

function initialPositions(nodes: WordSummary[], families: GraphData['families'], selected: string | null, saved: Props['positions']) {
  const result: Record<string, { x: number; y: number }> = {};
  const familyIds = new Set(families.flatMap(family => family.nodeIds));
  if (selected) {
    result[selected] = saved[selected] || { x: 500, y: 350 };
    const ownFamily = new Set(families.find(family => family.nodeIds.includes(selected))?.nodeIds || [selected]);
    const members = nodes.filter(node => node.wordId !== selected && ownFamily.has(node.wordId));
    members.forEach((node, index) => {
      const proposed = point(500, 350, 92, (2 * Math.PI * index) / members.length - Math.PI / 2);
      const stored = saved[node.wordId];
      result[node.wordId] = stored || proposed;
    });
    const neighbors = nodes.filter(node => !ownFamily.has(node.wordId));
    neighbors.forEach((node, index) => {
      const ring = Math.floor(index / 12);
      const slot = index % 12;
      const count = Math.min(12, neighbors.length - ring * 12);
      const proposed = point(500, 350, 265 + ring * 110, (2 * Math.PI * slot) / count - Math.PI / 2);
      const stored = saved[node.wordId];
      result[node.wordId] = stored || proposed;
    });
  } else {
    const cols = Math.min(3, Math.max(1, Math.ceil(Math.sqrt(families.length))));
    families.forEach((family, index) => {
      const members = nodes.filter(node => family.nodeIds.includes(node.wordId));
      const center = { x: 170 + (index % cols) * 300, y: 140 + Math.floor(index / cols) * 280 };
      members.forEach((node, slot) => {
        const proposed = members.length === 1 ? center : point(center.x, center.y, 74, (slot * Math.PI * 2) / members.length);
        const stored = saved[node.wordId];
        result[node.wordId] = stored || proposed;
      });
    });
    const outsiders = nodes.filter(node => !familyIds.has(node.wordId));
    const outsideY = 230 + Math.ceil(families.length / cols) * 280;
    outsiders.forEach((node, index) => {
      const proposed = { x: 100 + (index % 5) * 190, y: outsideY + Math.floor(index / 5) * 105 };
      const stored = saved[node.wordId];
      result[node.wordId] = stored || proposed;
    });
  }
  return result;
}

export default function GraphView(props: Props) {
  const { data, theme, selected, zoom, positions, visible, levels, search, showOthers, showOutside, stageId } = props;
  const [live, setLive] = useState<Record<string, { x: number; y: number }>>({});
  const liveRef = useRef<Record<string, { x: number; y: number }>>({});
  const velocity = useRef<Record<string, { x: number; y: number }>>({});
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [edgeDetails, setEdgeDetails] = useState<GraphEdge | null>(null);
  const drag = useRef<{ kind: 'node' | 'canvas'; id?: string; x: number; y: number; origin: { x: number; y: number }; latest?: { x: number; y: number }; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const nodes = data?.nodes || [];
  const layout = useMemo(() => initialPositions(nodes, data?.families || [], selected, positions), [nodes, data?.families, selected, positions]);
  const place = (id: string) => live[id] || layout[id] || { x: 500, y: 350 };
  const nodeById = Object.fromEntries(nodes.map(node => [node.wordId, node]));
  const senseLabel = (wordId: string, senseId?: string) => {
    const node = nodeById[wordId];
    if (!node || !senseId) return '词条级';
    return [...node.core, ...node.more].find(sense => sense.senseId === senseId)?.text || '义项待核验';
  };
  const colorOf = (node: WordSummary) => node.outsideStage ? theme.learning.outsideStage : theme.learning[levels[node.wordId] || 'unfamiliar'];
  const relationColor = (kind: RelationType) => kind === 'family' ? theme.graph.family : theme.graph.relations[kind];
  const selectedNode = selected ? nodeById[selected] : null;

  useEffect(() => {
    if (!selected) { setPan({ x: 0, y: 0 }); return; }
    const saved = positions[selected] || { x: 500, y: 350 };
    setPan({ x: (500 - saved.x) * zoom / 100, y: (350 - saved.y) * zoom / 100 });
  }, [selected]);

  const pointerMove = (event: PointerEvent<SVGSVGElement>) => {
    if (!drag.current) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const dx = (event.clientX - drag.current.x) * 1000 / bounds.width;
    const dy = (event.clientY - drag.current.y) * 700 / bounds.height;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true;
    if (drag.current.kind === 'canvas') setPan({ x: drag.current.origin.x + dx, y: drag.current.origin.y + dy });
    else if (drag.current.id) {
      const position = { x: drag.current.origin.x + dx / (zoom / 100), y: drag.current.origin.y + dy / (zoom / 100) };
      drag.current.latest = position;
      const id = drag.current.id;
      const physics = theme.graph.physics;
      const next = { ...layout, ...liveRef.current, [id]: position };
      for (let step = 0; step < 5; step++) {
        for (const node of nodes) {
          const key = node.wordId;
          if (key === id) continue;
          const here = next[key];
          if (!here) continue;
          let fx = 0, fy = 0;
          for (const edge of data?.edges || []) {
            if (edge.source !== key && edge.target !== key) continue;
            const other = next[edge.source === key ? edge.target : edge.source];
            if (!other) continue;
            const dx = other.x - here.x, dy = other.y - here.y;
            const distance = Math.max(1, Math.hypot(dx, dy));
            const force = (distance - physics.restLength) * physics.springStrength;
            fx += force * dx / distance; fy += force * dy / distance;
          }
          for (const otherNode of nodes) {
            if (otherNode.wordId === key) continue;
            const other = next[otherNode.wordId];
            if (!other) continue;
            const dx = here.x - other.x, dy = here.y - other.y;
            const distance2 = Math.max(100, dx * dx + dy * dy);
            fx += physics.repulsionStrength * dx / (distance2 * Math.sqrt(distance2));
            fy += physics.repulsionStrength * dy / (distance2 * Math.sqrt(distance2));
          }
          const prior = velocity.current[key] || { x: 0, y: 0 };
          const speed = { x: (prior.x + fx) * physics.damping, y: (prior.y + fy) * physics.damping };
          velocity.current[key] = speed;
          next[key] = { x: here.x + speed.x, y: here.y + speed.y };
        }
      }
      liveRef.current = next;
      setLive(next);
    }
  };
  const pointerUp = () => {
    if (drag.current?.kind === 'node' && drag.current.id && drag.current.moved) {
      props.onPositions(liveRef.current);
    }
    suppressClick.current = !!drag.current?.moved;
    drag.current = null;
    window.setTimeout(() => { suppressClick.current = false; }, 0);
  };
  const startNode = (event: PointerEvent<SVGGElement>, id: string) => {
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { kind: 'node', id, x: event.clientX, y: event.clientY, origin: place(id), moved: false };
  };
  const startCanvas = (event: PointerEvent<SVGSVGElement>) => {
    if (event.target !== event.currentTarget && (event.target as Element).tagName.toLowerCase() !== 'rect') return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { kind: 'canvas', x: event.clientX, y: event.clientY, origin: pan, moved: false };
  };

  return <section className="graph-panel" aria-label="词汇星图">
    <div className="graph-toolbar">
      <div className="graph-title"><strong>词汇星图</strong><small>{data?.entryCount || 0} 个词条节点 · {data?.placeholderCount || 0} 个待建节点{data?.hiddenCount ? ` · 另有 ${data.hiddenCount} 个未展示` : ''}</small></div>
      <div className="graph-filters" aria-label="图谱关系筛选">
        {relationTypes.map(kind => <button key={kind} type="button" aria-pressed={visible[kind]} onClick={() => props.onToggle(kind)}
          className={`filter-chip ${visible[kind] ? 'active' : ''}`} style={{ '--chip': relationColor(kind) } as CSSProperties}>
          <span className="filter-dot" />{relationLabels[kind]}</button>)}
        <button type="button" className={`filter-chip ${showOthers ? 'active' : ''}`} disabled={!selected} aria-pressed={showOthers} onClick={props.onShowOthers}>显示其他节点</button>
      </div>
      {stageId !== 'all' && <button type="button" className="outside-toggle" aria-pressed={showOutside} onClick={props.onShowOutside}>显示阶段外关联词：{showOutside ? '开' : '关'}</button>}
    </div>
    <div className="graph-stage">
      <div className="graph-search"><span aria-hidden="true">⌕</span><input aria-label="搜索图谱单词" placeholder="搜索已入库单词…" value={search} onChange={event => props.onSearch(event.target.value)} />{search && <button type="button" onClick={() => props.onSearch('')} aria-label="清除搜索">×</button>}</div>
      <svg className="graph-canvas" viewBox="0 0 1000 700" role="img" aria-label="可拖拽和缩放的词汇关系图"
        onPointerDown={startCanvas} onPointerMove={pointerMove} onPointerUp={pointerUp}
        onWheel={event => { event.preventDefault(); props.onZoom(clampZoom(zoom + (event.deltaY < 0 ? 10 : -10))); }}>
        <rect x="0" y="0" width="1000" height="700" fill="transparent" onClick={() => { if (!suppressClick.current) props.onSelect(null); }} />
        <g transform={`translate(${pan.x} ${pan.y}) translate(500 350) scale(${zoom / 100}) translate(-500 -350)`}>
          {(data?.families || []).map(({ familyId: id, nodeIds: members }) => {
            const coordinates = members.map(place);
            const cx = coordinates.reduce((sum, item) => sum + item.x, 0) / coordinates.length;
            const cy = coordinates.reduce((sum, item) => sum + item.y, 0) / coordinates.length;
            const radius = Math.max(64, ...coordinates.map(item => Math.hypot(item.x - cx, item.y - cy) + 58));
            return <g key={id} className="family-bubble" onClick={event => { event.stopPropagation(); const next = clampZoom(Math.max(125, zoom)); setPan({ x: (500 - cx) * next / 100, y: (350 - cy) * next / 100 }); props.onZoom(next); }}>
              <circle cx={cx} cy={cy} r={radius} fill={theme.graph.family} fillOpacity="0.045" stroke={theme.graph.family} strokeOpacity="0.42" strokeWidth="1.5" strokeDasharray="7 7" />
              <text x={cx} y={cy - radius + 18} textAnchor="middle" fill={theme.graph.family} fontSize="11">词族 · {members.length}</text>
            </g>;
          })}
          {(data?.edges || []).map((edge, index) => {
            const source = place(edge.source), target = place(edge.target);
            let a = source, b = target;
            if (selectedNode && edge.source === selected && edge.sourceSenseId) {
              const i = selectedNode.senseIds.indexOf(edge.sourceSenseId);
              if (i >= 0) a = point(source.x, source.y, 43, (i + 0.5) * 2 * Math.PI / selectedNode.senseIds.length - Math.PI / 2);
            }
            if (selectedNode && edge.target === selected && edge.targetSenseId) {
              const i = selectedNode.senseIds.indexOf(edge.targetSenseId);
              if (i >= 0) b = point(target.x, target.y, 43, (i + 0.5) * 2 * Math.PI / selectedNode.senseIds.length - Math.PI / 2);
            }
            return <line key={`${edge.relationshipId}-${index}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={relationColor(edge.type)} strokeWidth="2.4" strokeOpacity={edge.status === 'pending' ? 0.48 : 0.72} strokeDasharray={edge.status === 'pending' ? '5 5' : undefined} className="graph-edge" onClick={event => { event.stopPropagation(); setEdgeDetails(edge); }}>
              <title>{`${nodeById[edge.source]?.lemma} — ${relationLabels[edge.type]}${edge.ruleClassified ? '（按规则分类）' : ''}${edge.status === 'pending' ? '（待核验）' : ''} — ${nodeById[edge.target]?.lemma}`}</title>
            </line>;
          })}
          {nodes.map(node => {
            const pos = place(node.wordId), active = selected === node.wordId;
            const activate = () => node.kind === 'entry' ? props.onSelect(active ? null : node.wordId) : props.onOpenCandidate(node.lemma);
            return <g key={node.wordId} data-node-id={node.wordId} data-node-kind={node.kind} className={`graph-node ${node.kind !== 'entry' ? 'is-placeholder' : ''}`} transform={`translate(${pos.x} ${pos.y})`}
              onPointerDown={event => startNode(event, node.wordId)} onClick={event => { event.stopPropagation(); if (!suppressClick.current) activate(); }}
              tabIndex={0} role="button" aria-label={`${node.lemma}，${node.kind === 'entry' ? node.core.map(s => s.text).join('；') || '释义待核验' : '待建词条'}`}
              onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') activate(); }}>
              <circle r={active ? 38 : 32} fill={node.kind === 'entry' ? colorOf(node) : theme.learning.outsideStage} fillOpacity={node.kind === 'entry' ? 1 : 0.48} stroke={active ? theme.ui.text : theme.ui.border} strokeWidth={active ? 2.5 : 1.5} />
              {active && node.senseIds.map((senseId, i) => <path key={senseId} d={arc(0, 0, 44,
                i * 2 * Math.PI / node.senseIds.length - Math.PI / 2 + 0.04,
                (i + 1) * 2 * Math.PI / node.senseIds.length - Math.PI / 2 - 0.04)}
                fill="none" stroke={theme.ui.text} strokeWidth="5"><title>{`${i + 1}. ${[...node.core, ...node.more][i]?.text || ''}`}</title></path>)}
              <text y={zoom >= 125 ? -1 : 4} textAnchor="middle" fontSize={active ? 14 : 12} fontWeight="700" fill={theme.ui.text}>{node.lemma.length > 13 ? node.lemma.slice(0, 12) + '…' : node.lemma}</text>
              {zoom >= 125 && <text y="16" textAnchor="middle" fontSize="9" fill={theme.ui.mutedText}>{node.kind !== 'entry' ? '待建词条' : `${node.core[0]?.verificationStatus === 'pending' ? '待核验 · ' : ''}${node.core[0]?.text || '待完善'}`}</text>}
              {active && zoom >= 125 && node.senseIds.map((senseId, i) => {
                const angle = (i + 0.5) * 2 * Math.PI / node.senseIds.length - Math.PI / 2;
                const label = point(0, 0, 94, angle);
                return <text key={senseId} x={label.x + (label.x >= 0 ? 5 : -5)} y={label.y} textAnchor={label.x >= 0 ? 'start' : 'end'} fontSize="9" fill={theme.ui.text}>{node.senseIds.length <= 6 ? `${i + 1}. ${[...node.core, ...node.more][i]?.text || ''}` : `${i + 1}`}</text>;
              })}
              <title>{`${node.lemma} · ${node.kind === 'entry' ? node.core.map(s => `${s.text}${s.verificationStatus === 'pending' ? '（待核验）' : ''}`).join('；') || '释义待完善' : '待建词条'}`}</title>
            </g>;
          })}
        </g>
      </svg>
      {nodes.length === 0 && <div className="graph-empty"><strong>图谱正在等待词语</strong><span>生成并导入词条后，关系会在这里出现。</span></div>}
      {edgeDetails && <div className="edge-details"><button type="button" onClick={() => setEdgeDetails(null)} aria-label="关闭关系详情">×</button><strong>{nodeById[edgeDetails.source]?.lemma} ↔ {nodeById[edgeDetails.target]?.lemma}</strong><span>{relationLabels[edgeDetails.type]}{edgeDetails.ruleClassified ? ' · 按规则分类' : ''}{edgeDetails.status === 'pending' ? ' · 待核验' : ''}</span>
        {edgeDetails.relations.map((relation, index) => {
          const targetWordId = relation.sourceWordId === edgeDetails.source ? edgeDetails.target : edgeDetails.source;
          return <small key={`${relation.relationshipId}-${index}`}>{relation.sourceSenseId ?
            `${nodeById[relation.sourceWordId]?.lemma}：${senseLabel(relation.sourceWordId, relation.sourceSenseId)} → ${nodeById[targetWordId]?.lemma}：${senseLabel(targetWordId, relation.targetSenseId)}` : '词条级关系'}</small>;
        })}
      </div>}
      <div className="graph-legend"><span>熟悉度</span><i style={{ background: theme.learning.unfamiliar }} />陌生<i style={{ background: theme.learning.seen }} />见过<i style={{ background: theme.learning.familiar }} />熟悉<i style={{ background: theme.learning.outsideStage, opacity: 0.48 }} />待建{stageId !== 'all' && <><i style={{ background: theme.learning.outsideStage }} />阶段外</>}</div>
      <div className="zoom-controls">
        <input type="range" aria-label="图谱缩放比例" min="25" max="400" step="5" value={zoom} onChange={event => props.onZoom(Number(event.target.value))} />
        <select aria-label="常用缩放比例" value={[25, 50, 75, 100, 125, 150, 200].includes(zoom) ? zoom : ''} onChange={event => props.onZoom(Number(event.target.value))}>
          <option value="" disabled>{zoom}%</option>{[25, 50, 75, 100, 125, 150, 200].map(value => <option key={value} value={value}>{value}%</option>)}
        </select>
        <button type="button" disabled={zoom <= 25} onClick={() => props.onZoom(clampZoom(zoom - 10))} aria-label="缩小">−</button>
        <button type="button" disabled={zoom >= 400} onClick={() => props.onZoom(clampZoom(zoom + 10))} aria-label="放大">+</button>
      </div>
    </div>
  </section>;
}
