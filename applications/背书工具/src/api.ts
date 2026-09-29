import type { MemorySpan, Project, ProjectRecord, RecentProject } from './model';

const base = import.meta.env.VITE_BEISHU_API_URL || 'http://127.0.0.1:5176';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(base + path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } });
  } catch {
    throw new Error('无法连接本地服务，请通过 run.ps1 启动应用');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data as T;
}

export const health = () => request<{ status: string; workspace: string }>('/health');
export const timestamp = () => request<{ timestamp: string }>('/api/timestamp');
export const listProjects = () => request<RecentProject[]>('/api/projects');
export const readProject = (id: string) => request<ProjectRecord>(`/api/projects/${encodeURIComponent(id)}`);
export const saveProject = (project: Project, text: string) => request<ProjectRecord>('/api/projects/save', { method: 'POST', body: JSON.stringify({ project, text }) });
export const saveProjectOrder = (projectIds: string[]) => request<{ projectIds: string[] }>('/api/projects/order', { method: 'POST', body: JSON.stringify({ projectIds }) });
export const deleteProject = (id: string) => request<{ deleted: string }>(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const deleteInvalidProject = (directoryName: string) => request<{ deletedDirectory: string }>(`/api/project-directories/${encodeURIComponent(directoryName)}`, { method: 'DELETE' });
export const startExtraction = (text: string) => request<{ jobId: string; status: string }>('/api/extractions', { method: 'POST', body: JSON.stringify({ text }) });
export const extractionStatus = (id: string) => request<{ jobId: string; status: 'running' | 'completed' | 'failed'; stage: string; error?: string; spans?: MemorySpan[]; extraction?: Project['extraction'] }>(`/api/extractions/${encodeURIComponent(id)}`);
