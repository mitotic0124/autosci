export type WorkflowRun = {
  id: number;
  trigger_source: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  summary: string | null;
  error_message: string | null;
};

export type Paper = {
  id: number;
  run_id: number;
  arxiv_id: string;
  title: string;
  authors: string;
  abstract: string;
  categories: string;
  published_at: string | null;
  url: string;
  pdf_url: string;
  score: number;
};

export type PaperNote = {
  paper_id: number;
  markdown_content: string;
  image_paths: string;
};

export type BacktestReport = {
  id: number;
  run_id: number;
  paper_id: number | null;
  title: string;
  markdown_content: string;
  metrics_json: string;
  artifact_paths: string;
};

export type PublicationTask = {
  id: number;
  run_id: number;
  paper_id: number;
  paper_code: string;
  title: string;
  status: string;
  requested_by: string | null;
  zhihu_url: string | null;
  error_message: string | null;
  published_at: string | null;
};

export type WorkflowGraph = {
  nodes: Array<{ id: string; label: string }>;
  edges: Array<{ source: string; target: string }>;
};

export type WorkflowNodeStatus = "pending" | "active" | "done" | "failed";

export type WorkflowProgress = {
  running: boolean;
  mode: string;
  run_id: number | null;
  current_step: string;
  message: string;
  updated_at: string;
  node_status: Record<string, WorkflowNodeStatus>;
};

export type RunDetail = {
  run: WorkflowRun;
  summary: Record<string, unknown>;
  papers: Array<{
    id: number;
    title: string;
    arxiv_id: string;
    score: number;
    url: string;
    pdf_url: string;
    note_markdown: string;
    image_paths: string[];
  }>;
  backtests: Array<{
    id: number;
    title: string;
    markdown_content: string;
    metrics: Record<string, number>;
    artifact_paths: string[];
  }>;
  publications?: Array<{
    id: number;
    paper_code: string;
    title: string;
    status: string;
    zhihu_url: string | null;
    error_message: string | null;
    published_at: string | null;
  }>;
};

export type SettingsPayload = {
  defaults: Record<string, string | number>;
  runtime: Record<string, string>;
};

export type PublishByCodesResponse = {
  requested: string[];
  results: Array<{
    code: string;
    status: "PUBLISHED" | "FAILED" | "NOT_FOUND" | "ALREADY_PUBLISHED" | string;
    zhihu_url?: string;
    error?: string;
  }>;
  stats?: {
    PUBLISHED: number;
    ALREADY_PUBLISHED: number;
    FAILED: number;
    RATE_LIMITED?: number;
    NOT_FOUND: number;
  };
  ok?: boolean;
  message?: string;
};

export type LLMTestResponse = {
  ok: boolean;
  provider?: string;
  model?: string;
  base_url?: string | null;
  latency_ms?: number;
  response_preview?: string;
  message: string;
};
