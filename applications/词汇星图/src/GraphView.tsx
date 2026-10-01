import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent } from 'react';
import type { GraphData, Level, RelationType, Theme, WordSummary } from './model';
import { relationLabels, relationTypes } from './model';
type GraphEdge = GraphData['edges'][number];
const EMPTY_NODES: WordSummary[] = [];
const EMPTY_FAMILIES: GraphData['families'] = [];
const EMPTY_EDGES: GraphEdge[] = [];

type Props = {
  projectName: string; onProjectRename: (name: string) => Promise<void>;
  data: GraphData | null; theme: Theme; selected: string | null;
  zoom: number; positions: Record<string, { x: number; y: number }>;
  visible: Record<RelationType, boolean>; levels: Record<string, Level>;
  search: string; showOthers: boolean;
  showOutside: boolean; stageId: string; onShowOutside: () => void;
  onSearch: (value: string) => void; onSelect: (id: string | null) => void;
  onOpenCandidate: (lemma: string) => void;
  onToggle: (kind: RelationType) => void; onShowOthers: () => void;
  onZoom: (value: number) => void; onPositions: (positions: Record<string, { x: number; y: number }>) => void;
  resetToken: number; onInitialize: () => void;
};

const clampZoom = (value: number) => Math.min(400, Math.max(25, Math.round(value / 5) * 5));
const point = (cx: number, cy: number, radius: number, angle: number) => ({ x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius });
const arc = (cx: number, cy: number, radius: number, start: number, end: number) => {
  const a = point(cx, cy, radius, start), b = point(cx, cy, radius, end);
  return `M ${a.x} ${a.y} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${b.x} ${b.y}`;
};
const cellSize = 240;
type Positions = Props['positions'];
const nodeRadius = (id: string, selected: string | null) => id === selected ? 38 : 32;
const posAbbreviations: Record<string, string> = {
  noun: 'n.', verb: 'v.', adjective: 'adj.', adverb: 'adv.', pronoun: 'pron.', preposition: 'prep.',
  conjunction: 'conj.', interjection: 'interj.', determiner: 'det.', article: 'art.',
  'auxiliary verb': 'aux.', 'modal verb': 'modal v.', 'phrasal verb': 'phr. v.',
};
function fitNodeLabel(value: string, radius: number, fontSize = 9) {
  const available = (radius * 2 - 10) / fontSize;
  const width = (text: string) => [...text].reduce((sum, char) => sum + (/^[\x00-\x7F]$/.test(char) ? 0.6 : 1), 0);
  if (width(value) <= available) return value;
  let result = '';
  for (const char of value) {
    if (width(result + char + '…') > available) break;
    result += char;
  }
  return result.trimEnd() + '…';
}

// Pack disks with the configured boundary gap, without overlapping disks.
// Translating the finished cluster preserves every spacing constraint during family avoidance.
function packFamily(members: string[], positions: Positions, selected: string | null, familyNodeGap: number) {
  if (members.length < 2) return;
  const center = { x: members.reduce((sum, id) => sum + positions[id].x, 0) / members.length,
    y: members.reduce((sum, id) => sum + positions[id].y, 0) / members.length };
  const ordered = [...members].sort((a, b) => Number(b === selected) - Number(a === selected) || a.localeCompare(b));
  const packed: Positions = { [ordered[0]]: { x: 0, y: 0 } };
  const placed = [ordered[0]];
  const cells = spatialBuckets(placed, packed);
  for (const id of ordered.slice(1)) {
    let best: { x: number; y: number } | null = null;
    let score = Infinity;
    for (const anchor of placed) for (let angle = 0; angle < 12; angle++) {
      const candidate = point(packed[anchor].x, packed[anchor].y,
        nodeRadius(id, selected) + nodeRadius(anchor, selected) + familyNodeGap, angle * Math.PI / 6);
      const distance = candidate.x ** 2 + candidate.y ** 2;
      if (distance >= score) continue;
      if (nearby(cells, candidate.x, candidate.y, 76 + familyNodeGap).some(other =>
        Math.hypot(candidate.x - packed[other].x, candidate.y - packed[other].y) <
        nodeRadius(id, selected) + nodeRadius(other, selected) + familyNodeGap - 0.001)) continue;
      best = candidate; score = distance;
    }
    if (!best) throw new Error('无法构造满足间距的词族布局');
    packed[id] = best; placed.push(id);
    const key = `${Math.floor(best.x / cellSize)},${Math.floor(best.y / cellSize)}`;
    const cell = cells.get(key) || []; cell.push(id); cells.set(key, cell);
  }
  const mean = { x: placed.reduce((sum, id) => sum + packed[id].x, 0) / placed.length,
    y: placed.reduce((sum, id) => sum + packed[id].y, 0) / placed.length };
  for (const id of placed) positions[id] = { x: center.x + packed[id].x - mean.x, y: center.y + packed[id].y - mean.y };
}
function spatialBuckets(ids: string[], positions: Positions) {
  const cells = new Map<string, string[]>();
  for (const id of ids) {
    const position = positions[id];
    if (!position) continue;
    const key = `${Math.floor(position.x / cellSize)},${Math.floor(position.y / cellSize)}`;
    const members = cells.get(key) || [];
    members.push(id); cells.set(key, members);
  }
  return cells;
}
function nearby(cells: Map<string, string[]>, x: number, y: number, radius: number) {
  const result: string[] = [];
  for (let column = Math.floor((x - radius) / cellSize); column <= Math.floor((x + radius) / cellSize); column++) {
    for (let row = Math.floor((y - radius) / cellSize); row <= Math.floor((y + radius) / cellSize); row++) {
      result.push(...(cells.get(`${column},${row}`) || []));
    }
  }
  return result;
}

function familyContactsHold(families: GraphData['families'], positions: Positions, selected: string | null, familyNodeGap: number) {
  return families.every(family => {
    const members = family.nodeIds;
    if (members.length < 2) return true;
    return members.every(id => {
      if (!positions[id]) return false;
      let touches = false;
      for (const other of members) {
        if (other === id) continue;
        if (!positions[other]) return false;
        const gap = Math.hypot(positions[id].x - positions[other].x, positions[id].y - positions[other].y)
          - nodeRadius(id, selected) - nodeRadius(other, selected) - familyNodeGap;
        if (gap < -0.01) return false;
        if (Math.abs(gap) <= 0.01) touches = true;
      }
      return touches;
    });
  });
}

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

function settlePositions(nodes: WordSummary[], edges: GraphEdge[], families: GraphData['families'],
  initial: Props['positions'], physics: Props['theme']['graph']['physics'], steps = 24, selected: string | null = null) {
  const next = Object.fromEntries(Object.entries(initial).map(([id, position]) => [id, { ...position }]));
  const nodeIds = nodes.map(node => node.wordId);
  const nodeOrder = new Map(nodeIds.map((id, index) => [id, index]));
  const groups = families.map(family => family.nodeIds.filter(id => next[id])).filter(members => members.length);
  const geometry = (members: string[]) => {
    const center = { x: members.reduce((sum, id) => sum + next[id].x, 0) / members.length,
      y: members.reduce((sum, id) => sum + next[id].y, 0) / members.length };
    const radius = Math.max(64, ...members.map(id => Math.hypot(next[id].x - center.x, next[id].y - center.y) + 58));
    return { center, radius };
  };
  for (let step = 0; step < steps; step++) {
    const forces: Record<string, { x: number; y: number }> = Object.fromEntries(nodeIds.map(id => [id, { x: 0, y: 0 }]));
    for (const edge of edges) {
      const a = next[edge.source], b = next[edge.target];
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y, distance = Math.max(1, Math.hypot(dx, dy));
      const force = Math.max(-8, Math.min(8, (distance - physics.restLength) * physics.springStrength));
      forces[edge.source].x += force * dx / distance; forces[edge.source].y += force * dy / distance;
      forces[edge.target].x -= force * dx / distance; forces[edge.target].y -= force * dy / distance;
    }
    for (const members of groups) {
      const { center } = geometry(members);
      const compactRadius = Math.max(32, Math.sqrt(members.length) * 24);
      for (const id of members) {
        const dx = center.x - next[id].x, dy = center.y - next[id].y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = Math.max(0, distance - compactRadius) * physics.springStrength * 3;
        forces[id].x += force * dx / distance; forces[id].y += force * dy / distance;
      }
    }
    const repulsionCells = spatialBuckets(nodeIds, next);
    for (let i = 0; i < nodeIds.length; i++) for (const otherId of nearby(repulsionCells, next[nodeIds[i]].x, next[nodeIds[i]].y, 300)) {
      const j = nodeOrder.get(otherId) ?? -1;
      if (j <= i) continue;
      const a = next[nodeIds[i]], b = next[otherId];
      const dx = a.x - b.x, dy = a.y - b.y, distance2 = Math.max(100, dx * dx + dy * dy);
      if (distance2 > 90000) continue;
      const force = physics.repulsionStrength / (distance2 * Math.sqrt(distance2)) + Math.max(0, 90 - Math.sqrt(distance2)) * 0.2 / Math.sqrt(distance2);
      forces[nodeIds[i]].x += force * dx; forces[nodeIds[i]].y += force * dy;
      forces[otherId].x -= force * dx; forces[otherId].y -= force * dy;
    }
    for (const id of nodeIds) {
      next[id].x += Math.max(-12, Math.min(12, forces[id].x * physics.damping));
      next[id].y += Math.max(-12, Math.min(12, forces[id].y * physics.damping));
    }
    // Keep family circles separate and exclude nodes from other families.
    for (let i = 0; i < groups.length; i++) for (let j = i + 1; j < groups.length; j++) {
      const a = geometry(groups[i]), b = geometry(groups[j]);
      const dx = b.center.x - a.center.x, dy = b.center.y - a.center.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const overlap = a.radius + b.radius + 18 - distance;
      if (overlap <= 0) continue;
      const direction = distance === 1 ? { x: 1, y: 0 } : { x: dx / distance, y: dy / distance };
      const shift = Math.min(18, overlap * physics.familyNonMemberRepulsionStrength / 5000);
      for (const id of groups[i]) { next[id].x -= direction.x * shift; next[id].y -= direction.y * shift; }
      for (const id of groups[j]) { next[id].x += direction.x * shift; next[id].y += direction.y * shift; }
    }
    const exclusionCells = spatialBuckets(nodeIds, next);
    for (const members of groups) {
      const { center, radius } = geometry(members), own = new Set(members);
      for (const id of nearby(exclusionCells, center.x, center.y, radius + 36)) {
        if (own.has(id)) continue;
        const dx = next[id].x - center.x, dy = next[id].y - center.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        if (distance >= radius + 36) continue;
        const angle = distance === 1 ? ((nodeOrder.get(id) ?? 0) * 2.39996) : Math.atan2(dy, dx);
        const shift = Math.min(24, (radius + 36 - distance) * physics.familyNonMemberRepulsionStrength / 5000);
        next[id].x += Math.cos(angle) * shift; next[id].y += Math.sin(angle) * shift;
      }
    }
  }
  for (const members of groups) packFamily(members, next, selected, physics.familyNodeGap);
  const grouped = new Set(groups.flat());
  const rigidGroups = [...groups, ...nodeIds.filter(id => !grouped.has(id)).map(id => [id])];
  const shapes = rigidGroups.map(geometry);
  const shapeIds = shapes.map((_, index) => String(index));
  const centers = Object.fromEntries(shapes.map((shape, index) => [String(index), shape.center]));
  const shapeCells = spatialBuckets(shapeIds, centers);
  const maxRadius = Math.max(0, ...shapes.map(shape => shape.radius));
  const moveGroup = (index: number, dx: number, dy: number) => {
    const id = String(index), center = shapes[index].center;
    const oldKey = `${Math.floor(center.x / cellSize)},${Math.floor(center.y / cellSize)}`;
    center.x += dx; center.y += dy;
    for (const nodeId of rigidGroups[index]) { next[nodeId].x += dx; next[nodeId].y += dy; }
    const newKey = `${Math.floor(center.x / cellSize)},${Math.floor(center.y / cellSize)}`;
    if (oldKey === newKey) return;
    shapeCells.set(oldKey, (shapeCells.get(oldKey) || []).filter(member => member !== id));
    const cell = shapeCells.get(newKey) || []; cell.push(id); shapeCells.set(newKey, cell);
  };
  // Avoid other families as rigid clusters so exclusion cannot break the spacing constraint.
  for (let pass = 0; pass < 80; pass++) {
    let moved = false;
    for (let i = 0; i < rigidGroups.length; i++) for (const otherId of
      nearby(shapeCells, shapes[i].center.x, shapes[i].center.y, shapes[i].radius + maxRadius + 18)) {
      const j = Number(otherId);
      if (j <= i) continue;
      const a = shapes[i], b = shapes[j];
      const dx = b.center.x - a.center.x, dy = b.center.y - a.center.y;
      const distance = Math.hypot(dx, dy), overlap = a.radius + b.radius + 18 - distance;
      if (overlap <= 0.001) continue;
      const angle = distance < 0.01 ? (i + j) * 2.39996 : Math.atan2(dy, dx);
      const sx = Math.cos(angle) * (overlap / 2 + 0.001), sy = Math.sin(angle) * (overlap / 2 + 0.001);
      moveGroup(i, -sx, -sy); moveGroup(j, sx, sy);
      moved = true;
    }
    if (!moved) break;
  }
  return next;
}

export default function GraphView(props: Props) {
  const { data, theme, selected, zoom, positions, visible, levels, search, showOthers, showOutside, stageId } = props;
  const [projectDraft, setProjectDraft] = useState(props.projectName);
  const [renameError, setRenameError] = useState('');
  const [renaming, setRenaming] = useState(false);
  const renameBusy = useRef(false);
  const cancelRename = useRef(false);
  useEffect(() => { setProjectDraft(props.projectName); }, [props.projectName]);
  const saveProjectName = async () => {
    if (cancelRename.current) { cancelRename.current = false; return; }
    if (renameBusy.current || projectDraft === props.projectName) return;
    if (!projectDraft.trim()) { setRenameError('项目名不能为空'); return; }
    renameBusy.current = true; setRenaming(true); setRenameError('');
    try { await props.onProjectRename(projectDraft.trim()); }
    catch (exc) { setRenameError(String(exc)); }
    finally { renameBusy.current = false; setRenaming(false); }
  };
  const [live, setLive] = useState<Record<string, { x: number; y: number }>>({});
  const liveRef = useRef<Record<string, { x: number; y: number }>>({});
  const velocity = useRef<Record<string, { x: number; y: number }>>({});
  const pendingFrame = useRef<number | null>(null);
  const layoutGeneration = useRef(0);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [edgeDetails, setEdgeDetails] = useState<GraphEdge | null>(null);
  const drag = useRef<{ kind: 'node' | 'canvas'; id?: string; x: number; y: number; origin: { x: number; y: number }; latest?: { x: number; y: number }; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const lastReset = useRef(0);
  const baseline = useRef<{ positions: Positions; pan: { x: number; y: number }; zoom: number } | null>(null);
  const previousSelected = useRef<string | null>(selected);
  const nodes = data?.nodes || EMPTY_NODES;
  const families = data?.families || EMPTY_FAMILIES;
  const edges = data?.edges || EMPTY_EDGES;
  const layoutKey = useMemo(() => JSON.stringify([
    selected,
    nodes.map(node => node.wordId),
    families.map(family => [family.familyId, family.nodeIds]),
    edges.map(edge => [edge.relationshipId, edge.source, edge.target, edge.type]),
  ]), [nodes, families, edges, selected]);
  const previousLayoutSource = useRef({ layoutKey, physics: theme.graph.physics });
  const nodeIds = useMemo(() => nodes.map(node => node.wordId), [nodes]);
  const connected = useMemo(() => {
    const result = new Map<string, GraphEdge[]>();
    for (const edge of edges) {
      for (const id of [edge.source, edge.target]) {
        const items = result.get(id) || [];
        items.push(edge); result.set(id, items);
      }
    }
    return result;
  }, [edges]);
  const overviewEdgeOffsets = useMemo(() => {
    if (selected) return new Map<GraphEdge, number>();
    const pairs = new Map<string, GraphEdge[]>();
    for (const edge of edges) {
      const key = JSON.stringify([edge.source, edge.target].sort());
      const items = pairs.get(key) || []; items.push(edge); pairs.set(key, items);
    }
    const offsets = new Map<GraphEdge, number>();
    for (const items of pairs.values()) items.forEach((edge, index) => {
      offsets.set(edge, (index - (items.length - 1) / 2) * 6 * (edge.source < edge.target ? 1 : -1));
    });
    return offsets;
  }, [edges, selected]);
  useEffect(() => () => { if (pendingFrame.current !== null) window.cancelAnimationFrame(pendingFrame.current); }, []);
  const layout = useMemo(() => initialPositions(nodes, families, selected, positions), [nodes, families, selected, positions]);
  useEffect(() => {
    const generation = ++layoutGeneration.current;
    const prior = previousLayoutSource.current;
    const sameSource = prior.layoutKey === layoutKey && prior.physics === theme.graph.physics;
    previousLayoutSource.current = { layoutKey, physics: theme.graph.physics };
    if (props.resetToken > lastReset.current) return;
    if (sameSource && nodes.length && nodes.every(node => {
      const current = liveRef.current[node.wordId], saved = positions[node.wordId];
      return current && saved && current.x === saved.x && current.y === saved.y;
    })) return;
    const timer = window.setTimeout(() => {
      if (generation !== layoutGeneration.current) return;
      const restored = !selected && baseline.current ? { ...layout, ...baseline.current.positions } : null;
      const physicsChanged = prior.physics !== theme.graph.physics;
      const settled = restored && !physicsChanged && familyContactsHold(families, restored, selected, theme.graph.physics.familyNodeGap)
        ? restored
        : settlePositions(nodes, edges, families, restored || layout, theme.graph.physics, restored && !physicsChanged ? 0 : 8, selected);
      if (generation !== layoutGeneration.current) return;
      if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
      liveRef.current = settled; velocity.current = {}; setLive(settled);
      if (!selected && !baseline.current && nodes.length) baseline.current = { positions: settled, pan: { x: 0, y: 0 }, zoom };
    }, 0);
    return () => { window.clearTimeout(timer); layoutGeneration.current++; };
  }, [layout, edges, families, theme.graph.physics, selected, props.resetToken, layoutKey]);
  useEffect(() => {
    if (!nodes.length || props.resetToken <= lastReset.current) return;
    const timer = window.setTimeout(() => {
      if (props.resetToken <= lastReset.current) return;
      const scattered = Object.fromEntries(nodes.map(node => [node.wordId, {
        x: 500 + (Math.random() - 0.5) * 220, y: 350 + (Math.random() - 0.5) * 160,
      }]));
      const settled = settlePositions(nodes, edges, families, scattered, theme.graph.physics, 16, selected);
      if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
      lastReset.current = props.resetToken;
      liveRef.current = settled; velocity.current = {}; setLive(settled);
      setPan({ x: 0, y: 0 });
      baseline.current = { positions: settled, pan: { x: 0, y: 0 }, zoom };
      props.onPositions(settled);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [props.resetToken, nodes, edges, families, theme.graph.physics]);
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
    if (!selected) {
      if (previousSelected.current && baseline.current) {
        liveRef.current = baseline.current.positions;
        setLive(baseline.current.positions);
        setPan(baseline.current.pan);
        if (zoom !== baseline.current.zoom) props.onZoom(baseline.current.zoom);
      }
      previousSelected.current = null;
      return;
    }
    previousSelected.current = selected;
    const center = liveRef.current[selected] || layout[selected] || { x: 500, y: 350 };
    setPan({ x: (500 - center.x) * zoom / 100, y: (350 - center.y) * zoom / 100 });
  }, [selected, data?.selected]);

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
      const draggedOrigin = drag.current.origin;
      for (let step = 0; step < 5; step++) {
        const cells = spatialBuckets(nodeIds, next);
        for (const node of nodes) {
          const key = node.wordId;
          if (key === id) continue;
          const here = next[key];
          if (!here) continue;
          const distanceFromDrag = Math.max(1, Math.hypot(here.x - position.x, here.y - position.y));
          const influence = Math.min(1, (physics.restLength * physics.restLength) /
            (distanceFromDrag * distanceFromDrag));
          let fx = 0, fy = 0;
          for (const edge of connected.get(key) || []) {
            const other = next[edge.source === key ? edge.target : edge.source];
            if (!other) continue;
            const dx = other.x - here.x, dy = other.y - here.y;
            const distance = Math.max(1, Math.hypot(dx, dy));
            const force = (distance - physics.restLength) * physics.springStrength;
            fx += force * dx / distance; fy += force * dy / distance;
          }
          for (const otherId of nearby(cells, here.x, here.y, 300)) {
            if (otherId === key) continue;
            const other = next[otherId];
            if (!other) continue;
            const dx = here.x - other.x, dy = here.y - other.y;
            const distance2 = Math.max(100, dx * dx + dy * dy);
            if (distance2 > 90000) continue;
            fx += physics.repulsionStrength * dx / (distance2 * Math.sqrt(distance2));
            fy += physics.repulsionStrength * dy / (distance2 * Math.sqrt(distance2));
          }
          const ownFamily = families.find(family => family.nodeIds.includes(key));
          if (ownFamily) {
            const members = ownFamily.nodeIds.filter(member => next[member]);
            const cx = members.reduce((sum, member) => sum + next[member].x, 0) / members.length;
            const cy = members.reduce((sum, member) => sum + next[member].y, 0) / members.length;
            const dx = cx - here.x, dy = cy - here.y, distance = Math.max(1, Math.hypot(dx, dy));
            const force = Math.max(0, distance - Math.max(32, Math.sqrt(members.length) * 24)) * physics.springStrength * 3;
            fx += force * dx / distance; fy += force * dy / distance;
          }
          const prior = velocity.current[key] || { x: 0, y: 0 };
          const speed = { x: (prior.x + fx) * physics.damping * influence,
            y: (prior.y + fy) * physics.damping * influence };
          velocity.current[key] = speed;
          next[key] = { x: here.x + speed.x + (position.x - draggedOrigin.x) * influence * 0.002,
            y: here.y + speed.y + (position.y - draggedOrigin.y) * influence * 0.002 };
        }
      }
      liveRef.current = next;
      if (pendingFrame.current === null) pendingFrame.current = window.requestAnimationFrame(() => {
        pendingFrame.current = null; setLive({ ...liveRef.current });
      });
    }
  };
  const pointerUp = () => {
    if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
    if (drag.current?.kind === 'node' && drag.current.id && drag.current.moved) {
      const settled = settlePositions(nodes, edges, families, liveRef.current, theme.graph.physics, 10, selected);
      liveRef.current = settled; setLive(settled); props.onPositions(settled);
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
      <div className="graph-title"><strong>词汇星图</strong><input className="graph-project-name" aria-label="当前项目名" value={projectDraft} disabled={renaming || !props.projectName} onChange={event => setProjectDraft(event.target.value)} onBlur={() => void saveProjectName()} onKeyDown={event => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') { event.stopPropagation(); cancelRename.current = true; setProjectDraft(props.projectName); setRenameError(''); event.currentTarget.blur(); }
      }} />{renameError && <span className="error-note">{renameError}<button type="button" onClick={() => void saveProjectName()}>重试</button></span>}<small>{data?.entryCount || 0} 个词条节点 · {data?.placeholderCount || 0} 个待建节点{data?.hiddenCount ? ` · 另有 ${data.hiddenCount} 个未展示` : ''} · Alt + 滚轮缩放</small></div>
      <div className="graph-filters" aria-label="图谱关系筛选">
        <button type="button" className="filter-chip" onClick={() => props.onInitialize()}>初始化</button>
        {relationTypes.map(kind => <button key={kind} type="button" aria-pressed={visible[kind]} onClick={() => props.onToggle(kind)}
          className={`filter-chip ${visible[kind] ? 'active' : ''}`} style={{ '--chip': relationColor(kind) } as CSSProperties}>
          <span className="filter-dot" />{relationLabels[kind]}</button>)}
        <button type="button" className={`filter-chip ${showOthers ? 'active' : ''}`} disabled={!selected} aria-pressed={showOthers} onClick={props.onShowOthers}>显示其他节点</button>
      </div>
      {stageId !== 'all' && <button type="button" className="outside-toggle" aria-pressed={showOutside} onClick={props.onShowOutside}>显示阶段外关联词：{showOutside ? '开' : '关'}</button>}
    </div>
    <div className="graph-stage">
      {!selected && <small className="graph-density-note">展示可见节点间全部已开启的关系，选中单词查看直接关联</small>}
      <div className="graph-search"><span aria-hidden="true">⌕</span><input aria-label="搜索图谱单词" placeholder="搜索已入库单词…" value={search} onChange={event => props.onSearch(event.target.value)} />{search && <button type="button" onClick={() => props.onSearch('')} aria-label="清除搜索">×</button>}</div>
      <svg className="graph-canvas" viewBox="0 0 1000 700" role="img" aria-label="可拖拽的词汇关系图，按 Alt 加滚轮缩放"
        onPointerDown={startCanvas} onPointerMove={pointerMove} onPointerUp={pointerUp}
        onWheel={event => { if (!event.altKey || event.deltaY === 0) return; event.preventDefault(); props.onZoom(clampZoom(zoom + (event.deltaY < 0 ? 10 : -10))); }}>
        <rect x="0" y="0" width="1000" height="700" fill="transparent" onClick={() => { if (!suppressClick.current) props.onSelect(null); }} />
        <g transform={`translate(${pan.x} ${pan.y}) translate(500 350) scale(${zoom / 100}) translate(-500 -350)`}>
          {families.map(({ familyId: id, nodeIds: members }) => {
            const coordinates = members.map(place);
            const cx = coordinates.reduce((sum, item) => sum + item.x, 0) / coordinates.length;
            const cy = coordinates.reduce((sum, item) => sum + item.y, 0) / coordinates.length;
            const radius = Math.max(64, ...coordinates.map(item => Math.hypot(item.x - cx, item.y - cy) + 58));
            return <g key={id} className="family-bubble" data-member-ids={members.join(',')} onClick={event => { event.stopPropagation(); const next = clampZoom(Math.max(125, zoom)); setPan({ x: (500 - cx) * next / 100, y: (350 - cy) * next / 100 }); props.onZoom(next); }}>
              <circle cx={cx} cy={cy} r={radius} fill={theme.graph.family} fillOpacity="0.045" stroke={theme.graph.family} strokeOpacity="0.42" strokeWidth="1.5" strokeDasharray="7 7" />
              <text x={cx} y={cy - radius + 18} textAnchor="middle" fill={theme.graph.family} fontSize="11">词族 · {members.length}</text>
            </g>;
          })}
          {edges.map((edge, index) => {
            const source = place(edge.source), target = place(edge.target);
            let a = source, b = target;
            const offset = overviewEdgeOffsets.get(edge) || 0;
            if (offset) {
              const dx = target.x - source.x, dy = target.y - source.y;
              const distance = Math.max(1, Math.hypot(dx, dy));
              const ox = -dy / distance * offset, oy = dx / distance * offset;
              a = { x: source.x + ox, y: source.y + oy };
              b = { x: target.x + ox, y: target.y + oy };
            }
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
            const core = node.core[0];
            const partOfSpeech = core?.partOfSpeech || '';
            const pending = core?.verificationStatus === 'pending' || node.status === 'rule_derived_pending';
            const definition = node.kind !== 'entry'
              ? node.status === 'rule_derived_pending' ? '规则推导' : '待建词条'
              : `${partOfSpeech ? (posAbbreviations[partOfSpeech] || partOfSpeech) + ' ' : ''}${core?.text || '待完善'}`;
            const fullDefinition = `${pending ? '待核验 · ' : ''}${definition}`;
            const activate = () => active ? props.onSelect(null) : node.kind === 'entry' ? props.onSelect(node.wordId) : props.onOpenCandidate(node.lemma);
            return <g key={node.wordId} data-node-id={node.wordId} data-node-kind={node.kind} className={`graph-node ${node.kind !== 'entry' ? 'is-placeholder' : ''}`} transform={`translate(${pos.x} ${pos.y})`}
              onPointerDown={event => startNode(event, node.wordId)} onClick={event => { event.stopPropagation(); if (!suppressClick.current) activate(); }}
              tabIndex={0} role="button" aria-label={`${node.lemma}，${node.kind === 'entry' ? node.core.map(s => `${s.partOfSpeech} ${s.text}`.trim()).join('；') || '释义待核验' : node.status === 'rule_derived_pending' ? '规则推导，待核验' : '待建词条'}`}
              onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') activate(); }}>
              <circle r={nodeRadius(node.wordId, selected)} fill={node.kind === 'entry' ? colorOf(node) : theme.learning.outsideStage} fillOpacity={node.kind === 'entry' ? 1 : 0.48} stroke={active ? theme.ui.text : theme.ui.border} strokeWidth={active ? 2.5 : 1.5} />
              {active && node.senseIds.map((senseId, i) => <path key={senseId} d={arc(0, 0, 44,
                i * 2 * Math.PI / node.senseIds.length - Math.PI / 2 + 0.04,
                (i + 1) * 2 * Math.PI / node.senseIds.length - Math.PI / 2 - 0.04)}
                fill="none" stroke={theme.ui.text} strokeWidth="5"><title>{`${i + 1}. ${[...node.core, ...node.more][i]?.text || ''}`}</title></path>)}
              <text y={zoom >= 100 ? -1 : 4} textAnchor="middle" fontSize={active ? 14 : 12} fontWeight="700" fill={theme.ui.text}>{node.lemma}</text>
              {zoom >= 100 && <text y="16" textAnchor="middle" fontSize="9" fill={theme.ui.mutedText}>
                {fitNodeLabel(definition, nodeRadius(node.wordId, selected))}<title>{fullDefinition}</title>
              </text>}
              {zoom >= 100 && pending && <text y="27" textAnchor="middle" fontSize="8" fill={theme.ui.mutedText}>待核验</text>}
              {active && zoom >= 125 && node.senseIds.map((senseId, i) => {
                const angle = (i + 0.5) * 2 * Math.PI / node.senseIds.length - Math.PI / 2;
                const label = point(0, 0, 66, angle);
                return <text key={senseId} x={label.x + (label.x >= 0 ? 5 : -5)} y={label.y} textAnchor={label.x >= 0 ? 'start' : 'end'} fontSize="9" fill={theme.ui.text}>{node.senseIds.length <= 6 ? `${i + 1}. ${[...node.core, ...node.more][i]?.text || ''}` : `${i + 1}`}</text>;
              })}
              <title>{`${node.lemma} · ${node.kind === 'entry' ? node.core.map(s => `${s.partOfSpeech} ${s.text}${s.verificationStatus === 'pending' ? '（待核验）' : ''}`).join('；') || '释义待完善' : node.status === 'rule_derived_pending' ? '规则推导 · 待核验' : '待建词条'}`}</title>
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
