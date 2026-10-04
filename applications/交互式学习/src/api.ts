export type Node = { id: string; title: string; number: string; group: string; track: 'main' | 'branch'; status: string; skip: boolean; mastery?: number | null; teaches?: string[]; prerequisites?: string[] };
export type Edge = { predecessor_id: string; successor_id: string; unit_id?: string };
export type ProjectSummary = { id: string; title: string; status: string; unitCount: number; lessonCount: number; updatedAt: string; createdAt: string | null; lastLearningAt: string | null };
export type Counts = { total: number; completed: number; learning: number; pending: number; skipped: number; completionRate: number | null };
export type ProjectDetails = { kind: 'tutor' | 'navigation'; directory: string; createdAt: string | null; lastLearningAt: string | null; currentLesson: { id: string; number: string; title: string } | null; statistics: { units: Counts; lessons: Counts; main: Counts; branch: Counts; mastery: number | null }; materials: { id: string; title: string }[]; coverage: { point_id: string; summary: string; track: string; unit_ids: string[]; lessons: { lesson_id: string; number: string; block_ids: string[]; degree: string }[] }[] };
export type Project = { id: string; title: string; status: string; state: string; revision: string; canEdit: boolean; units: Node[]; lessons: Node[]; unitEdges: Edge[]; lessonEdges: Edge[]; pending: Record<string, boolean>; details: ProjectDetails };
export type Archive = { id: string; title: string; kind: string; archivedAt: string; revision: string };
export type ConfigValue = number | boolean | { [key: string]: ConfigValue };
export type Config = { config: Record<string, ConfigValue>; revision: string };
export type UiConfig = { poll_interval_seconds: number; graph: { min_zoom: number; max_zoom: number; prerequisite_color: string; teaching_color: string } };
const base = import.meta.env.VITE_LEARNING_API_URL || 'http://127.0.0.1:5178';
async function request<T>(path: string, method = 'GET', payload?: unknown): Promise<T> {
  let response: Response;
  try { response = await fetch(base + path, { method, headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' }, body: payload === undefined ? undefined : JSON.stringify(payload) }); }
  catch { throw new Error('无法连接本地服务，请通过 run.ps1 启动应用'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data as T;
}
export const listProjects = () => request<ProjectSummary[]>('/api/projects');
export const readProject = (id: string) => request<Project>(`/api/projects/${encodeURIComponent(id)}`);
export const version = (id: string) => request<{ revision: string }>(`/api/projects/${encodeURIComponent(id)}/version`);
export const uiConfig = () => request<UiConfig>('/api/ui-config');
export const configs = () => request<Record<string, Config>>('/api/config');
export const saveConfig = (skill: string, config: Config) => request<Config>(`/api/config/${encodeURIComponent(skill)}`, 'PUT', { config: config.config, expectedRevision: config.revision });
export const action = (project: Project, type: 'lesson' | 'unit', id: string, operation: 'skip' | 'restore') => request<Project>(`/api/projects/${encodeURIComponent(project.id)}/actions`, 'POST', { actionId: crypto.randomUUID(), expectedRevision: project.revision, targetType: type, targetId: id, operation });
export const listArchives = () => request<Archive[]>('/api/archives');
export const manageProject = (id: string, revision: string, operation: 'rename' | 'archive' | 'restore' | 'open-directory', title?: string, actionId: string = crypto.randomUUID()) => request<{ status: string }>(`/api/projects/${encodeURIComponent(id)}/manage`, 'POST', { actionId, expectedRevision: revision, operation, ...(title === undefined ? {} : { title }) });
