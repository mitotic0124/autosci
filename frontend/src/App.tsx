import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";

import {
  deleteRun,
  getRunDetail,
  getSettings,
  getWorkflowGraph,
  getWorkflowProgress,
  listRuns,
  pollPublicationCommands,
  publishByCodes,
  testLLM,
  triggerWorkflow,
  updateSettings,
} from "./api";
import { WorkflowGraphPanel } from "./components/WorkflowGraph";
import type {
  LLMTestResponse,
  PublishByCodesResponse,
  RunDetail,
  SettingsPayload,
  WorkflowGraph,
  WorkflowProgress,
  WorkflowRun,
} from "./types";

function formatTime(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function parseSummary(summary: string | null): Record<string, string | number | boolean> {
  if (!summary) return {};
  try {
    return JSON.parse(summary) as Record<string, string | number | boolean>;
  } catch {
    return {};
  }
}

function pickSetting(payload: SettingsPayload | null, key: string): string {
  if (!payload) return "";
  const runtime = payload.runtime[key];
  if (runtime !== undefined) return runtime;
  const defaultValue = payload.defaults[key];
  return defaultValue !== undefined ? String(defaultValue) : "";
}

const SETTINGS_DESCRIPTION: Record<string, string> = {
  schedule_hour: "每天定时任务触发小时（0-23）。",
  schedule_minute: "每天定时任务触发分钟（0-59）。",
  paper_count: "每天筛选论文数量，当前建议 3 篇。",
  arxiv_max_candidates: "每次从 arXiv 拉取的候选论文上限。",
  paper_history_lookback_days: "排除近 N 天内已入库论文标题/ID，减少连续多天重复选中同一批论文。",
  arxiv_extra_keywords: "额外主题关键词（英文逗号分隔），会加入 arXiv 检索与启发式打分。",
  llm_provider: "LLM 提供方类型，openai_compatible 代表 OpenAI 协议兼容接口。",
  llm_api_base_url: "LLM 接口基础地址，可填第三方兼容网关。",
  llm_api_key: "LLM 接口密钥。",
  llm_model: "模型名称，例如 gpt-4.1-mini。",
  llm_note_system_prompt: "生成论文解读的系统提示词。",
  llm_score_system_prompt: "论文筛选评分的系统提示词。",
  mail_to: "接收论文编号邮件的邮箱。",
  mail_from: "发件人邮箱地址。",
  smtp_host: "SMTP 服务器地址。",
  smtp_port: "SMTP 端口，常见 587(TLS) 或 465(SSL)。",
  smtp_username: "SMTP 登录用户名。",
  smtp_password: "SMTP 授权码或密码。",
  imap_host: "IMAP 服务器地址，用于读取回复邮件。",
  imap_port: "IMAP 端口，常见 993。",
  imap_username: "IMAP 登录用户名。",
  imap_password: "IMAP 授权码或密码。",
  imap_use_ssl: "是否启用 IMAP SSL，true/false。",
  imap_folder: "监听邮件文件夹，默认 INBOX。",
  command_poll_interval_seconds: "轮询回复邮箱的间隔（秒）。",
  command_authorized_sender: "允许触发发布命令的发件人邮箱白名单。",
  zhihu_column_url: "知乎专栏写作页地址。",
  zhihu_firefox_profile_path: "Firefox Profile 路径（用于复用登录态）。",
  zhihu_headless: "知乎自动发布是否无头模式，true/false。",
  zhihu_publish_enabled: "是否启用知乎自动发布，true/false。",
  zhihu_auto_publish: "是否在草稿写入成功后自动点击发布，true/false。建议先设为 false 手工检查。",
  workflow_auto_publish_after_run: "工作流完成后是否自动执行发布流程，true/false。建议与 zhihu_auto_publish 搭配使用。",
  zhihu_markdown_converter_cmd: "Markdown 转知乎格式命令（如 md2zhihu）。为空则不转换。",
};

const SETTING_ORDER = [
  "schedule_hour",
  "schedule_minute",
  "paper_count",
  "arxiv_max_candidates",
  "paper_history_lookback_days",
  "arxiv_extra_keywords",
  "llm_provider",
  "llm_api_base_url",
  "llm_api_key",
  "llm_model",
  "llm_note_system_prompt",
  "llm_score_system_prompt",
  "mail_to",
  "mail_from",
  "smtp_host",
  "smtp_port",
  "smtp_username",
  "smtp_password",
  "imap_host",
  "imap_port",
  "imap_username",
  "imap_password",
  "imap_use_ssl",
  "imap_folder",
  "command_poll_interval_seconds",
  "command_authorized_sender",
  "zhihu_column_url",
  "zhihu_firefox_profile_path",
  "zhihu_headless",
  "zhihu_publish_enabled",
  "zhihu_auto_publish",
  "workflow_auto_publish_after_run",
  "zhihu_markdown_converter_cmd",
];

function summarizeManualResult(result: PublishByCodesResponse): string {
  const stats =
    result.stats ??
    result.results.reduce(
      (acc, item) => {
        const key = item.status;
        if (
          key === "PUBLISHED" ||
          key === "DRAFT" ||
          key === "ALREADY_PUBLISHED" ||
          key === "FAILED" ||
          key === "RATE_LIMITED" ||
          key === "NOT_FOUND"
        ) {
          acc[key] += 1;
        }
        return acc;
      },
        {
          PUBLISHED: 0,
          DRAFT: 0,
          ALREADY_PUBLISHED: 0,
          FAILED: 0,
          RATE_LIMITED: 0,
          NOT_FOUND: 0,
        },
    );

  return `手动发布执行完成：请求 ${result.requested.length} 个编号，公开发布 ${stats.PUBLISHED}，草稿保存 ${stats.DRAFT ?? 0}，已发布 ${stats.ALREADY_PUBLISHED}，限流 ${stats.RATE_LIMITED}，失败 ${stats.FAILED}，未找到 ${stats.NOT_FOUND}。`;
}

export default function App() {
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [progress, setProgress] = useState<WorkflowProgress | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [testingLLM, setTestingLLM] = useState(false);
  const [manualCodes, setManualCodes] = useState("");
  const [manualResult, setManualResult] = useState<PublishByCodesResponse | null>(null);
  const [llmTestResult, setLlmTestResult] = useState<LLMTestResponse | null>(null);

  const [settingsForm, setSettingsForm] = useState<Record<string, string>>({
    schedule_hour: "6",
    schedule_minute: "0",
    paper_count: "3",
    arxiv_max_candidates: "40",
    paper_history_lookback_days: "7",
    arxiv_extra_keywords:
      "volatility,liquidity,market microstructure,limit order book,transaction cost,return predictability,cross-sectional,alpha",
    llm_provider: "openai_compatible",
    llm_api_base_url: "",
    llm_api_key: "",
    llm_model: "gpt-4.1-mini",
    llm_note_system_prompt: "",
    llm_score_system_prompt: "",
    mail_to: "",
    mail_from: "",
    smtp_host: "smtp.163.com",
    smtp_port: "587",
    smtp_username: "",
    smtp_password: "",
    imap_host: "imap.163.com",
    imap_port: "993",
    imap_username: "",
    imap_password: "",
    imap_use_ssl: "true",
    imap_folder: "INBOX",
    command_poll_interval_seconds: "120",
    command_authorized_sender: "",
    zhihu_column_url: "https://zhuanlan.zhihu.com/write",
    zhihu_firefox_profile_path: "",
    zhihu_headless: "false",
    zhihu_publish_enabled: "true",
    zhihu_auto_publish: "false",
    workflow_auto_publish_after_run: "false",
    zhihu_markdown_converter_cmd: "md2zhihu",
  });

  const latestRun = runs[0] ?? null;
  const latestSummary = useMemo(() => parseSummary(latestRun?.summary ?? null), [latestRun?.summary]);

  async function refreshAll() {
    setLoading(true);
    setErrorMessage("");
    try {
      const [runItems, graphData, settingsData, progressData] = await Promise.all([
        listRuns(),
        getWorkflowGraph(),
        getSettings(),
        getWorkflowProgress(),
      ]);
      setRuns(runItems);
      setGraph(graphData);
      setProgress(progressData);

      setSettingsForm((prev) => {
        const next = { ...prev };
        for (const key of SETTING_ORDER) {
          const value = pickSetting(settingsData, key);
          if (value !== "") next[key] = value;
        }
        return next;
      });

      setSelectedRunId((prev) => {
        if (runItems.length === 0) return null;
        if (prev && runItems.some((item) => item.id === prev)) return prev;
        return runItems[0].id;
      });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refreshAll();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const progressData = await getWorkflowProgress();
        setProgress(progressData);
      } catch {
        // ignore polling errors
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    async function loadRunDetail() {
      if (!selectedRunId) {
        setRunDetail(null);
        return;
      }
      try {
        const detail = await getRunDetail(selectedRunId);
        setRunDetail(detail);
      } catch (error) {
        setErrorMessage((error as Error).message);
      }
    }
    loadRunDetail();
  }, [selectedRunId]);

  async function handleTriggerRun() {
    setStatusMessage("正在触发论文工作流...");
    setErrorMessage("");
    try {
      const result = await triggerWorkflow();
      setStatusMessage(result.message);
      window.setTimeout(refreshAll, 1200);
    } catch (error) {
      setErrorMessage((error as Error).message);
    }
  }

  async function handleDeleteSelectedRun() {
    if (!selectedRunId) {
      setErrorMessage("请先选中要删除的运行记录。");
      return;
    }
    if (!window.confirm(`确认删除 Run #${selectedRunId} 吗？`)) {
      return;
    }
    setStatusMessage(`正在删除 Run #${selectedRunId} ...`);
    setErrorMessage("");
    try {
      const result = await deleteRun(selectedRunId);
      setStatusMessage(result.message);
      setSelectedRunId(null);
      await refreshAll();
    } catch (error) {
      setErrorMessage((error as Error).message);
    }
  }

  async function handlePollInbox() {
    setStatusMessage("正在轮询邮箱回复...");
    setErrorMessage("");
    try {
      const result = await pollPublicationCommands();
        if (result.ok) {
          setStatusMessage(
            `轮询完成：收到命令 ${result.received_commands ?? 0}，公开发布 ${result.published_count ?? 0}，草稿保存 ${
              result.drafted_count ?? 0
            }，失败 ${result.failed_count ?? 0}，忽略 ${result.ignored_count ?? 0}。`,
          );
        } else {
        setStatusMessage(result.message ?? "轮询完成");
      }
      await refreshAll();
    } catch (error) {
      setErrorMessage((error as Error).message);
    }
  }

  async function handleManualPublish() {
    const codes = manualCodes
      .split(/[，,\s]+/)
      .map((code) => code.trim())
      .filter(Boolean);
    if (codes.length === 0) {
      setErrorMessage("请先输入论文编号，例如：R010-01,R010-03");
      return;
    }
    setStatusMessage("正在执行手动发布...");
    setErrorMessage("");
    setManualResult(null);
    try {
      const result = await publishByCodes(codes);
      setManualResult(result);
      if (result.ok === false) {
        setErrorMessage(result.message ?? "手动发布失败");
      }
      setStatusMessage(summarizeManualResult(result));
      await refreshAll();
    } catch (error) {
      setErrorMessage((error as Error).message);
    }
  }

  async function handleSaveSettings() {
    setSavingSettings(true);
    setStatusMessage("正在保存配置...");
    setErrorMessage("");
    try {
      await updateSettings(settingsForm);
      setStatusMessage("配置已保存。");
      await refreshAll();
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setSavingSettings(false);
    }
  }

  async function handleTestLLM() {
    setTestingLLM(true);
    setStatusMessage("正在测试 LLM 连通性...");
    setErrorMessage("");
    setLlmTestResult(null);
    try {
      const result = await testLLM(settingsForm);
      setLlmTestResult(result);
      if (result.ok) {
        setStatusMessage(`LLM 测试成功，耗时 ${result.latency_ms ?? "-"}ms。`);
      } else {
        setStatusMessage("LLM 测试失败。");
        setErrorMessage(result.message);
      }
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setTestingLLM(false);
    }
  }

  return (
    <div className="app-shell">
      <div className="ambient-bg" />
      <header className="hero">
        <div>
          <h1>AutoSci 论文编号发布工作流</h1>
          <p>每天 06:00 自动筛选 3 篇论文，生成深度解读并发送编号；回复“发布Rxxx-xx”后自动发布到知乎专栏。</p>
        </div>
        <div className="hero-actions">
          <button className="btn-primary" onClick={handleTriggerRun}>
            手动执行一次
          </button>
          <button className="btn-secondary" onClick={handlePollInbox}>
            轮询邮箱回复
          </button>
          <button className="btn-secondary" onClick={refreshAll} disabled={loading}>
            刷新状态
          </button>
        </div>
      </header>

      {statusMessage && <div className="status-banner status-ok">{statusMessage}</div>}
      {errorMessage && <div className="status-banner status-error">{errorMessage}</div>}

      <section className="cards">
        <article className="card">
          <h3>最近一次运行</h3>
          <p className="card-main">{latestRun ? `#${latestRun.id} / ${latestRun.status}` : "暂无"}</p>
          <p>{latestRun ? formatTime(latestRun.started_at) : "-"}</p>
        </article>
        <article className="card">
          <h3>已选论文数</h3>
          <p className="card-main">{String(latestSummary.selected_papers ?? 0)}</p>
          <p>目标: 3 篇</p>
        </article>
        <article className="card">
          <h3>实时步骤</h3>
          <p className="card-main">{progress?.current_step ?? "idle"}</p>
          <p>{progress?.message ?? "-"}</p>
        </article>
      </section>

      <section className="panel">
        <div className="panel-title">Workflow 可视化（实时）</div>
        <div className="run-meta" style={{ marginBottom: 10 }}>
          <span>模式: {progress?.mode ?? "idle"}</span>
          <span>运行中: {progress?.running ? "是" : "否"}</span>
          <span>Run: {progress?.run_id ?? "-"}</span>
          <span>更新时间: {progress?.updated_at ? formatTime(progress.updated_at) : "-"}</span>
        </div>
        <WorkflowGraphPanel graph={graph} progress={progress} />
      </section>

      <section className="split-layout">
        <article className="panel">
          <div className="panel-title">运行记录</div>
          <div className="settings-actions" style={{ marginTop: 0, marginBottom: 10 }}>
            <button className="btn-secondary" onClick={handleDeleteSelectedRun}>
              删除选中运行
            </button>
          </div>
          <div className="run-list">
            {runs.map((run) => (
              <button
                key={run.id}
                className={`run-row ${run.id === selectedRunId ? "active" : ""}`}
                onClick={() => setSelectedRunId(run.id)}
              >
                <span>#{run.id}</span>
                <span>{run.status}</span>
                <span>{formatTime(run.started_at)}</span>
              </button>
            ))}
            {runs.length === 0 && <div className="panel-empty">暂无运行记录</div>}
          </div>
        </article>

        <article className="panel">
          <div className="panel-title">发布任务（回复命令使用编号）</div>
          {!runDetail?.publications?.length && <div className="panel-empty">当前运行暂无发布任务</div>}
          <div className="run-list">
            {runDetail?.publications?.map((task) => (
              <div key={task.id} className="run-row">
                <span>{task.paper_code}</span>
                <span>{task.status}</span>
                <span>{task.zhihu_url ?? task.error_message ?? task.title}</span>
              </div>
            ))}
          </div>
          <div style={{ marginTop: 10 }}>
            <input
              style={{ width: "100%", padding: "9px 10px", borderRadius: 10, border: "1px solid #ced7e5" }}
              placeholder="手动发布编号，例如：R010-01,R010-03"
              value={manualCodes}
              onChange={(e) => setManualCodes(e.target.value)}
            />
            <div className="settings-actions">
              <button className="btn-primary" onClick={handleManualPublish}>
                手动发布编号
              </button>
            </div>
            {manualResult?.results?.length ? (
              <div className="run-list" style={{ marginTop: 8 }}>
                {manualResult.results.map((item) => (
                  <div className="run-row" key={`manual-${item.code}`}>
                    <span>{item.code}</span>
                    <span>{item.status}</span>
                    <span>{item.zhihu_url ?? item.error ?? "-"}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </article>
      </section>

      <section className="panel">
        <div className="panel-title">论文详细解读预览</div>
        {!runDetail?.papers?.length && <div className="panel-empty">当前运行暂无论文解读</div>}
        {runDetail?.papers?.map((paper) => (
          <details key={paper.id} style={{ marginBottom: 10 }}>
            <summary>
              {paper.title} ({paper.arxiv_id})
            </summary>
            <div className="paper-links">
              <a href={paper.url} target="_blank" rel="noreferrer">
                arXiv
              </a>
              <a href={paper.pdf_url} target="_blank" rel="noreferrer">
                PDF
              </a>
            </div>
            <div className="markdown-wrapper">
              <ReactMarkdown>{paper.note_markdown}</ReactMarkdown>
            </div>
          </details>
        ))}
      </section>

      <section className="panel">
        <div className="panel-title">系统配置（支持自定义 AI 调用）</div>
        <div className="settings-grid">
          {SETTING_ORDER.map((key) => {
            const value = settingsForm[key] ?? "";
            const isPrompt = key.endsWith("_prompt");
            const isSecret = key.includes("password") || key.includes("api_key");
            return (
              <label key={key}>
                <span>{key}</span>
                {isPrompt ? (
                  <textarea
                    rows={4}
                    value={value}
                    onChange={(event) =>
                      setSettingsForm((prev) => ({
                        ...prev,
                        [key]: event.target.value,
                      }))
                    }
                  />
                ) : key === "llm_provider" ? (
                  <select
                    value={value}
                    onChange={(event) =>
                      setSettingsForm((prev) => ({
                        ...prev,
                        [key]: event.target.value,
                      }))
                    }
                  >
                    <option value="openai_compatible">openai_compatible</option>
                    <option value="disabled">disabled</option>
                  </select>
                ) : (
                  <input
                    type={isSecret ? "password" : "text"}
                    value={value}
                    onChange={(event) =>
                      setSettingsForm((prev) => ({
                        ...prev,
                        [key]: event.target.value,
                      }))
                    }
                  />
                )}
              </label>
            );
          })}
        </div>
        <div className="settings-actions" style={{ display: "flex", gap: 10 }}>
          <button className="btn-primary" onClick={handleSaveSettings} disabled={savingSettings}>
            保存配置
          </button>
          <button className="btn-secondary" onClick={handleTestLLM} disabled={testingLLM}>
            测试 LLM 连通性
          </button>
        </div>
        {llmTestResult ? (
          <div className={`status-banner ${llmTestResult.ok ? "status-ok" : "status-error"}`} style={{ marginTop: 10 }}>
            <div>{llmTestResult.message}</div>
            <div>
              provider={llmTestResult.provider ?? "-"} / model={llmTestResult.model ?? "-"} / base=
              {llmTestResult.base_url ?? "-"}
            </div>
            <div>latency={llmTestResult.latency_ms ?? "-"} ms</div>
            {llmTestResult.response_preview ? <div>preview={llmTestResult.response_preview}</div> : null}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-title">前端可配置参数含义</div>
        <div className="help-grid">
          {SETTING_ORDER.map((key) => (
            <div className="help-item" key={`help-${key}`}>
              <strong>{key}</strong>
              <p>{SETTINGS_DESCRIPTION[key] ?? "暂无说明"}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
