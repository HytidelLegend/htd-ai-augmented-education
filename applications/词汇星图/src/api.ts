import type { Bootstrap, FavoritesState, GraphData, LearningState, Level, Project, SearchHit, UiState, WordPage, WordSummary } from './model';

export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined });
  let data: unknown;
  try { data = await response.json(); } catch { throw new Error(`服务响应无效（${response.status}）`); }
  if (!response.ok) throw new Error((data as { error?: string }).error || `请求失败（${response.status}）`);
  return data as T;
}
export const bootstrap = () => request<Bootstrap>('/api/bootstrap');
export const wordPage = (id: string, stage: string) => request<WordPage>(`/api/words/${encodeURIComponent(id)}?stage=${encodeURIComponent(stage)}`);
export const wordSummaries = (stage: string) => request<WordSummary[]>(`/api/words?stage=${encodeURIComponent(stage)}`);
export const graph = (params: URLSearchParams) => request<GraphData>(`/api/graph?${params}`);
export const createProject = (name: string, content: string, format: string) => request<Project>('/api/projects', 'POST', { name, content, format });
export const renameProject = (id: string, name: string, expectedRevision: number) => request<Project>(`/api/projects/${id}`, 'PUT', { name, expectedRevision });
export const projectLearning = (id: string) => request<LearningState>(`/api/projects/${id}/learning`);
export const searchDictionary = (query: string) => request<SearchHit[]>(`/api/dictionary/search?q=${encodeURIComponent(query)}`);
export const addToStage = (stage: string, wordId: string, expectedRevision: number) => request<{ revision: number }>(`/api/stages/${encodeURIComponent(stage)}/words`, 'POST', { wordId, expectedRevision });
export const candidate = (lemma: string) => request<{ lemma: string; existingRunId?: string | null; associations: { sourceLemma: string; sourceSenseId?: string; sourceSenseText?: string; type?: string; status?: string }[] }>(`/api/candidates?lemma=${encodeURIComponent(lemma)}`);
export const saveUi = (state: UiState, expectedRevision: number) => request<UiState>('/api/state/ui', 'PUT', { state, expectedRevision });
export const saveLevel = (id: string, level: Level, expectedRevision: number, projectId: string) => request<LearningState>(`/api/learning/${id}`, 'PATCH', { level, expectedRevision, projectId });
export const saveFavorite = (id: string, favorited: boolean, expectedRevision: number) => request<FavoritesState>(`/api/favorites/${id}`, 'POST', { favorited, expectedRevision });
export const openFavorite = (id: string, expectedRevision: number) => request<FavoritesState>(`/api/favorites/${id}/opened`, 'POST', { expectedRevision });
export const startWord = (lemma: string) => request<{ jobId: string | null; runId: string | null; status: string; error?: string }>('/api/runs', 'POST', { lemma });
export const jobStatus = (id: string) => request<{ jobId: string; runId: string | null; status: string; skillStatus?: string; error?: string }>(`/api/jobs/${id}`);
export const runStatus = (id: string) => request<{ status: string; current_stage?: string; resume_stage?: string; error?: { code: string; message: string }; pending_decisions?: unknown[]; run_id: string }>(`/api/runs/${id}`);
export const resumeRun = (id: string, decision?: object) => request<{ jobId: string; status: string }>(`/api/runs/${id}/resume`, 'POST', decision ? { decision } : {});
