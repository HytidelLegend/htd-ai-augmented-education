export type Mode = 'edit' | 'practice';
export type Difficulty = 'easy' | 'medium' | 'hard';
export type Phase = 'recent_projects' | 'creating_project' | 'extracting_spans' | 'reviewing_extraction' | 'editor_editing' | 'editor_practice' | 'opening_project' | 'saving' | 'conflict_pending' | 'error';

export interface Segment { start: number; end: number }
export interface MemorySpan { id: string; segments: Segment[]; color: string; role: string }
export interface Project {
  schemaVersion: 1;
  projectId: string;
  title: string;
  directoryName?: string;
  revision: number;
  lastWriterId: string;
  createdAt: string;
  updatedAt: string;
  lastOpenedAt: string;
  source: { path: 'source.txt'; sha256: string };
  extraction: { source: 'mark-memory-spans'; runId: string; status: 'verified' } | null;
  memorySpans: MemorySpan[];
}
export interface ProjectRecord { project: Project; text: string }
export interface RecentProject { projectId: string; title: string; directoryName: string; updatedAt: string; spanCount: number; error?: string }

export const transitions: Record<Phase, Phase[]> = {
  recent_projects: ['creating_project', 'opening_project', 'error'],
  creating_project: ['extracting_spans', 'editor_editing', 'editor_practice', 'recent_projects', 'error'],
  extracting_spans: ['reviewing_extraction', 'editor_editing', 'error'],
  reviewing_extraction: ['editor_editing', 'extracting_spans', 'saving', 'recent_projects', 'error'],
  editor_editing: ['editor_practice', 'saving', 'opening_project', 'creating_project', 'extracting_spans', 'conflict_pending', 'recent_projects', 'error'],
  editor_practice: ['editor_editing', 'saving', 'opening_project', 'creating_project', 'conflict_pending', 'recent_projects', 'error'],
  opening_project: ['editor_editing', 'editor_practice', 'recent_projects', 'error'],
  saving: ['editor_editing', 'editor_practice', 'reviewing_extraction', 'conflict_pending', 'error'],
  conflict_pending: ['editor_editing', 'editor_practice', 'opening_project', 'error'],
  error: ['recent_projects', 'editor_editing', 'creating_project'],
};

export function assertTransition(from: Phase, to: Phase) {
  if (from !== to && !transitions[from].includes(to)) throw new Error(`非法状态迁移：${from} → ${to}`);
}

export const normalizeText = (text: string) => text.replace(/\r\n?/g, '\n');
export const codepoints = (text: string) => Array.from(text);

export async function sourceHash(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(normalizeText(text));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function membership(spans: MemorySpan[], length: number): (string | null)[] {
  const result: (string | null)[] = Array(length).fill(null);
  for (const span of spans) for (const segment of span.segments)
    for (let index = segment.start; index < segment.end; index++) result[index] = span.id;
  return result;
}

export function compact(indices: number[]): Segment[] {
  const sorted = [...new Set(indices)].sort((a, b) => a - b);
  if (!sorted.length) return [];
  const result: Segment[] = [];
  let start = sorted[0], end = start + 1;
  for (const index of sorted.slice(1)) {
    if (index === end) end++;
    else { result.push({ start, end }); start = index; end = index + 1; }
  }
  result.push({ start, end });
  return result;
}

export function spanIndices(span: MemorySpan): number[] {
  return span.segments.flatMap(segment => Array.from({ length: segment.end - segment.start }, (_, i) => segment.start + i));
}

export function nextSpanColor(spans: MemorySpan[]): string {
  const used = new Set(spans.map(span => span.color.toLowerCase()));
  for (let index = spans.length; index < spans.length + 10001; index++) {
    const hue = ((index * 137.508) % 360) / 360;
    const channel = (offset: number) => {
      const k = (offset + hue * 12) % 12;
      const a = 0.58 * Math.min(0.57, 0.43);
      return Math.round(255 * (0.57 - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
    };
    const color = `#${[channel(0), channel(8), channel(4)].map(value => value.toString(16).padStart(2, '0')).join('')}`;
    if (!used.has(color)) return color;
  }
  throw new Error('无法分配新的记忆要点颜色');
}

export function paint(spans: MemorySpan[], indices: number[], action: 'add' | 'remove', text: string, targetId?: string): MemorySpan[] {
  const chars = codepoints(text);
  const selected = [...new Set(indices)].filter(i => i >= 0 && i < chars.length && !/\s/.test(chars[i]));
  if (!selected.length) return spans;
  const owners = membership(spans, chars.length);
  const next = structuredClone(spans);
  if (action === 'remove') {
    const removing = new Set(selected);
    return next.map(span => ({ ...span, segments: compact(spanIndices(span).filter(i => !removing.has(i))) })).filter(span => span.segments.length);
  }
  const free = selected.filter(i => owners[i] === null);
  if (!free.length) return spans;
  const target = targetId ? next.find(span => span.id === targetId) : undefined;
  if (target) target.segments = compact([...spanIndices(target), ...free]);
  else next.push({ id: crypto.randomUUID(), segments: compact(free), color: nextSpanColor(next), role: '记忆要点' });
  return next;
}

export function reassign(spans: MemorySpan[], indices: number[], targetId: string): MemorySpan[] {
  const selected = new Set(indices);
  const target = spans.find(span => span.id === targetId);
  if (!target) return spans;
  const targetIndices = new Set(spanIndices(target));
  if ([...selected].every(index => targetIndices.has(index))) return spans;
  const changed = spans.map(span => ({ ...span, segments: compact(spanIndices(span).filter(i => !selected.has(i))) })).filter(span => span.segments.length || span.id === targetId);
  const destination = changed.find(span => span.id === targetId)!;
  destination.segments = compact([...spanIndices(destination), ...indices]);
  return changed;
}

export function sampleHidden(spans: MemorySpan[], ratio: number): Set<string> {
  const ids = spans.map(span => span.id);
  for (let i = ids.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [ids[i], ids[j]] = [ids[j], ids[i]];
  }
  return new Set(ids.slice(0, Math.round(ids.length * Math.max(0, Math.min(100, ratio)) / 100)));
}
