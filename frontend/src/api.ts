import type {
  BacktestReport,
  LLMTestResponse,
  Paper,
  PaperNote,
  PublicationTask,
  PublishByCodesResponse,
  RunDetail,
  SettingsPayload,
  ServiceHealth,
  WorkflowGraph,
  WorkflowProgress,
  WorkflowRun,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
    },
    ...init,
  });

  if (!response.ok) {
    const rawMessage = await response.text();
    let message = rawMessage;
    try {
      const payload = JSON.parse(rawMessage) as { detail?: string; message?: string };
      message = payload.detail ?? payload.message ?? rawMessage;
    } catch {
      // Keep non-JSON error bodies unchanged.
    }
    throw new Error(message || `请求失败（${response.status}）`);
  }

  return (await response.json()) as T;
}

export function listRuns(): Promise<WorkflowRun[]> {
  return request<WorkflowRun[]>("/api/workflow/runs?limit=30");
}

export function getRunDetail(runId: number): Promise<RunDetail> {
  return request<RunDetail>(`/api/workflow/runs/${runId}`);
}

export function deleteRun(runId: number): Promise<{ ok: boolean; message: string }> {
  return request<{ ok: boolean; message: string }>(`/api/workflow/runs/${runId}`, {
    method: "DELETE",
  });
}

export function triggerWorkflow(): Promise<{ accepted: boolean; message: string }> {
  return request<{ accepted: boolean; message: string }>("/api/workflow/trigger", {
    method: "POST",
  });
}

export function listLatestPapers(): Promise<Paper[]> {
  return request<Paper[]>("/api/papers/latest");
}

export function getPaperNote(paperId: number): Promise<PaperNote> {
  return request<PaperNote>(`/api/papers/${paperId}/note`);
}

export function getLatestBacktest(): Promise<BacktestReport | null> {
  return request<BacktestReport | null>("/api/backtests/latest");
}

export function listPublications(runId?: number): Promise<PublicationTask[]> {
  const query = runId ? `?run_id=${runId}` : "";
  return request<PublicationTask[]>(`/api/publications${query}`);
}

export function pollPublicationCommands(): Promise<{
  ok: boolean;
  received_commands?: number;
  published_count?: number;
  drafted_count?: number;
  failed_count?: number;
  ignored_count?: number;
  message?: string;
}> {
  return request("/api/publications/poll-mail", { method: "POST" });
}

export function publishByCodes(codes: string[]): Promise<PublishByCodesResponse> {
  return request<PublishByCodesResponse>("/api/publications/publish", {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

export function getWorkflowGraph(): Promise<WorkflowGraph> {
  return request<WorkflowGraph>("/api/workflow/graph");
}

export function getWorkflowProgress(): Promise<WorkflowProgress> {
  return request<WorkflowProgress>("/api/workflow/progress");
}

export function getServiceHealth(): Promise<ServiceHealth> {
  return request<ServiceHealth>("/health");
}

export function resetZhihuBreaker(confirmedOfficialPageClear: boolean): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/api/zhihu/breaker/reset", {
    method: "POST",
    body: JSON.stringify({ confirmed_official_page_clear: confirmedOfficialPageClear }),
  });
}

export function reconcilePublication(
  code: string,
  published: boolean,
  publicUrl?: string,
): Promise<{ code: string; status: string; zhihu_url: string }> {
  return request(`/api/publications/${encodeURIComponent(code)}/reconcile`, {
    method: "POST",
    body: JSON.stringify({ confirmed: true, published, public_url: publicUrl }),
  });
}

export function getSettings(): Promise<SettingsPayload> {
  return request<SettingsPayload>("/api/settings");
}

export function updateSettings(values: Record<string, string>): Promise<{ ok: boolean; runtime: Record<string, string> }> {
  return request<{ ok: boolean; runtime: Record<string, string> }>("/api/settings", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
}

export function testLLM(values: Record<string, string>): Promise<LLMTestResponse> {
  return request<LLMTestResponse>("/api/llm/test", {
    method: "POST",
    body: JSON.stringify({ values }),
  });
}
