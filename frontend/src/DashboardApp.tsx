import { useEffect, useMemo, useState } from "react";
import {
  Activity, BookOpen, Check, ChevronRight, CircleAlert, Clock3, ExternalLink, Eye, EyeOff,
  FileText, Inbox, LoaderCircle, MailCheck, Play, RefreshCw, Rocket, Save, Settings2,
  ShieldCheck, Sparkles, Trash2, X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

import {
  deleteRun, getRunDetail, getSettings, getServiceHealth, getWorkflowGraph, getWorkflowProgress, listRuns,
  pollPublicationCommands, publishByCodes, reconcilePublication, resetZhihuBreaker, testLLM, triggerWorkflow, updateSettings,
} from "./api";
import { WorkflowGraphPanel } from "./components/WorkflowGraph";
import { DEFAULT_SETTINGS, SETTINGS_GROUPS, SETTINGS_META } from "./dashboardConfig";
import type {
  LLMTestResponse, PublishByCodesResponse, RunDetail, SettingsPayload, WorkflowGraph,
  WorkflowProgress, WorkflowRun, ServiceHealth,
} from "./types";

type ViewId = "overview" | "publishing" | "notes" | "settings";
type BusyAction = "trigger" | "mail" | "publish" | "delete" | "settings" | "llm" | null;

const SECRET_SETTING_KEYS = new Set(["llm_api_key", "smtp_password", "imap_password"]);

const NAV_ITEMS: Array<{ id: ViewId; label: string; icon: LucideIcon }> = [
  { id: "overview", label: "运行概览", icon: Activity },
  { id: "publishing", label: "发布任务", icon: Rocket },
  { id: "notes", label: "论文解读", icon: BookOpen },
  { id: "settings", label: "系统配置", icon: Settings2 },
];

function formatTime(value: string | null, timeZone = "Asia/Shanghai"): string {
  if (!value) return "-";
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  return new Date(timestamp).toLocaleString("zh-CN", { hour12: false, timeZone });
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

function statusTone(status: string | undefined): string {
  const normalized = (status ?? "").toUpperCase();
  if (["SUCCESS", "DONE", "PUBLISHED"].includes(normalized)) return "success";
  if (["FAILED", "INTERRUPTED", "RATE_LIMITED", "PUBLISH_UNCERTAIN", "QUALITY_BLOCKED"].includes(normalized)) return "danger";
  if (["RUNNING", "ACTIVE", "DRAFT", "PENDING", "PUBLISHING", "PARTIAL"].includes(normalized)) return "warning";
  return "neutral";
}

function summarizeManualResult(result: PublishByCodesResponse): string {
  const stats = result.stats ?? result.results.reduce(
    (acc, item) => {
      if (item.status in acc) acc[item.status as keyof typeof acc] += 1;
      return acc;
    },
    { PUBLISHED: 0, DRAFT: 0, PENDING: 0, PUBLISH_UNCERTAIN: 0, ALREADY_PUBLISHED: 0, FAILED: 0, RATE_LIMITED: 0, NOT_FOUND: 0, SUPERSEDED: 0, QUALITY_BLOCKED: 0 },
  );
  return `处理完成：草稿 ${stats.DRAFT ?? 0}，公开 ${stats.PUBLISHED}，排队 ${stats.PENDING ?? 0}，待核实 ${stats.PUBLISH_UNCERTAIN ?? 0}，已发布 ${stats.ALREADY_PUBLISHED}，失败 ${stats.FAILED}，限流 ${stats.RATE_LIMITED}，未找到 ${stats.NOT_FOUND}，已被新版本替代 ${stats.SUPERSEDED ?? 0}，质量阻断 ${stats.QUALITY_BLOCKED ?? 0}。`;
}

function ButtonSpinner() {
  return <LoaderCircle size={16} className="spin" />;
}

export default function DashboardApp() {
  const [activeView, setActiveView] = useState<ViewId>("overview");
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [progress, setProgress] = useState<WorkflowProgress | null>(null);
  const [health, setHealth] = useState<ServiceHealth | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [manualCodes, setManualCodes] = useState("");
  const [manualResult, setManualResult] = useState<PublishByCodesResponse | null>(null);
  const [llmTestResult, setLlmTestResult] = useState<LLMTestResponse | null>(null);
  const [settingsForm, setSettingsForm] = useState<Record<string, string>>(DEFAULT_SETTINGS);
  const [configuredSecrets, setConfiguredSecrets] = useState<Record<string, boolean>>({});
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, boolean>>({});

  const latestRun = runs[0] ?? null;
  const latestSummary = useMemo(() => parseSummary(latestRun?.summary ?? null), [latestRun?.summary]);
  const autoPublish = health?.public_publication ?? settingsForm.zhihu_auto_publish === "true";
  const nextRun = health?.scheduler.jobs.find((job) => job.id === "daily_quant_pipeline")?.next_run_at;
  const displayTimeZone = health?.scheduler.timezone ?? "Asia/Shanghai";
  const dailyStatusLabels: Record<string, string> = { SUCCESS: "流程完成", PARTIAL: "部分完成，仍有待处理项", RUNNING: "运行中", FAILED: "失败", INTERRUPTED: "中断待补跑" };
  const dailyStatusLabel = health?.daily_workflow?.last_status
    ? dailyStatusLabels[health.daily_workflow.last_status] || health.daily_workflow.last_status
    : health?.daily_workflow?.due ? "待补跑" : "未开始";
  const runError = runDetail?.run.error_message;
  const runErrorText = runError && /402|insufficient.*balance|余额不足/i.test(runError)
    ? "模型服务账户余额不足，请充值或更新可用的 API 配置后重新运行。当前任务未完成最终内容核验。"
    : runError;
  const currentView = NAV_ITEMS.find((item) => item.id === activeView) ?? NAV_ITEMS[0];

  async function refreshAll(options?: { quiet?: boolean }) {
    setLoading(true);
    if (!options?.quiet) setErrorMessage("");
    try {
      const [runItems, graphData, settingsData, progressData, healthData] = await Promise.all([
        listRuns(), getWorkflowGraph(), getSettings(), getWorkflowProgress(), getServiceHealth(),
      ]);
      setRuns(runItems);
      setGraph(graphData);
      setProgress(progressData);
      setHealth(healthData);
      setConfiguredSecrets(settingsData.secret_configured ?? {});
      setSettingsForm((previous) => {
        const next = { ...previous };
        for (const key of Object.keys(DEFAULT_SETTINGS)) {
          if (SECRET_SETTING_KEYS.has(key)) {
            next[key] = "";
            continue;
          }
          const value = pickSetting(settingsData, key);
          if (value !== "") next[key] = value;
        }
        return next;
      });
      setSelectedRunId((previous) => {
        if (runItems.length === 0) return null;
        if (previous && runItems.some((item) => item.id === previous)) return previous;
        return runItems[0].id;
      });
    } catch (error) {
      if (!options?.quiet) setErrorMessage((error as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshAll();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const [currentProgress, currentHealth] = await Promise.all([getWorkflowProgress(), getServiceHealth()]);
        setProgress(currentProgress);
        setHealth(currentHealth);
      } catch {
        // Keep the last known progress during transient polling failures.
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
        setRunDetail(await getRunDetail(selectedRunId));
      } catch (error) {
        setErrorMessage((error as Error).message);
      }
    }
    void loadRunDetail();
  }, [selectedRunId]);

  function beginAction(action: BusyAction, message: string) {
    setBusyAction(action);
    setStatusMessage(message);
    setErrorMessage("");
  }

  async function handleTriggerRun() {
    beginAction("trigger", "正在启动论文工作流...");
    try {
      const result = await triggerWorkflow();
      setStatusMessage(result.message);
      window.setTimeout(() => void refreshAll({ quiet: true }), 1200);
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDeleteSelectedRun() {
    if (!selectedRunId) {
      setErrorMessage("请先选择运行记录。");
      return;
    }
    if (!window.confirm(`确认删除 Run #${selectedRunId}？此操作会同时删除关联论文和发布任务。`)) return;
    beginAction("delete", `正在删除 Run #${selectedRunId}...`);
    try {
      const result = await deleteRun(selectedRunId);
      setStatusMessage(result.message);
      setSelectedRunId(null);
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handlePollInbox() {
    beginAction("mail", "正在检查回复邮件...");
    try {
      const result = await pollPublicationCommands();
      if (!result.ok) throw new Error(result.message ?? "邮箱轮询失败");
      setStatusMessage(
        `邮件检查完成：命令 ${result.received_commands ?? 0}，草稿 ${result.drafted_count ?? 0}，公开 ${
          result.published_count ?? 0
        }，失败 ${result.failed_count ?? 0}。`,
      );
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleManualPublish() {
    const codes = manualCodes.split(/[，,\s]+/).map((code) => code.trim()).filter(Boolean);
    if (codes.length === 0) {
      setErrorMessage("请输入至少一个论文编号，例如 R010-01。");
      return;
    }
    if (autoPublish && !window.confirm("当前已开启公开发布。确认将所选论文公开发布到知乎？")) return;
    beginAction("publish", autoPublish ? "正在发布到知乎..." : "正在写入知乎草稿...");
    setManualResult(null);
    try {
      const result = await publishByCodes(codes);
      setManualResult(result);
      setStatusMessage(summarizeManualResult(result));
      if (result.ok === false) setErrorMessage(result.message ?? "发布任务处理失败");
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSaveSettings() {
    beginAction("settings", "正在保存配置...");
    try {
      await updateSettings(settingsForSubmission());
      setRevealedSecrets({});
      setStatusMessage("配置已保存并应用。");
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleResetZhihuBreaker() {
    const confirmed = window.confirm("请确认你已在知乎官方页面检查：创作中心可正常访问，40362/频率限制提示已消失。解除后工作流可能按每日上限公开发布排队文章。是否继续？");
    if (!confirmed) return;
    beginAction("settings", "正在解除知乎发布熔断...");
    try {
      await resetZhihuBreaker(true);
      setStatusMessage("知乎发布熔断已解除；后续任务仍受每日公开发布上限约束。");
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleReconcilePublication(code: string, published: boolean) {
    let publicUrl: string | undefined;
    if (published) {
      const entered = window.prompt("粘贴知乎公开文章链接（https://zhuanlan.zhihu.com/p/文章ID）");
      if (!entered) return;
      publicUrl = entered.trim();
    } else if (!window.confirm(`请确认已检查知乎创作中心和公开页面，${code} 当前确实未公开。确认后任务会回到自动队列。`)) {
      return;
    }
    beginAction("publish", "正在登记知乎发布核实结果...");
    try {
      const result = await reconcilePublication(code, published, publicUrl);
      setStatusMessage(published ? `${code} 已登记为公开发布。` : `${code} 已确认未公开并放回队列。`);
      setManualResult({ requested: [code], results: [{ code, status: result.status, zhihu_url: result.zhihu_url }] });
      await refreshAll({ quiet: true });
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleTestLLM() {
    beginAction("llm", "正在测试模型接口...");
    setLlmTestResult(null);
    try {
      const result = await testLLM(settingsForSubmission());
      setLlmTestResult(result);
      if (!result.ok) throw new Error(result.message);
      setStatusMessage(`模型接口可用，响应耗时 ${result.latency_ms ?? "-"} ms。`);
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setBusyAction(null);
    }
  }

  function updateSetting(key: string, value: string) {
    setSettingsForm((previous) => ({ ...previous, [key]: value }));
  }

  function settingsForSubmission(): Record<string, string> {
    return Object.fromEntries(
      Object.entries(settingsForm).filter(([key, value]) => !SECRET_SETTING_KEYS.has(key) || value.trim() !== ""),
    );
  }

  function renderSettingField(key: string) {
    const meta = SETTINGS_META[key];
    const value = settingsForm[key] ?? "";
    if (!meta) return null;
    if (meta.kind === "boolean") {
      const checked = value === "true";
      return (
        <div className="setting-field setting-toggle" key={key}>
          <div><div className="field-label">{meta.label}</div><div className="field-help">{meta.help}</div></div>
          <button type="button" className={`toggle ${checked ? "on" : ""}`} role="switch" aria-checked={checked}
            aria-label={meta.label} onClick={() => updateSetting(key, checked ? "false" : "true")}><span /></button>
        </div>
      );
    }

    const id = `setting-${key}`;
    const onChange = (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
      updateSetting(key, event.target.value);
    return (
      <label className={`setting-field ${meta.kind === "textarea" ? "wide" : ""}`} key={key} htmlFor={id}>
        <span className="field-label">{meta.label}</span><span className="field-help">{meta.help}</span>
        {meta.kind === "textarea" ? (
          <textarea id={id} value={value} onChange={onChange} rows={key.includes("prompt") ? 6 : 3} />
        ) : meta.kind === "select" ? (
          <select id={id} value={value} onChange={onChange}>
            {meta.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        ) : meta.kind === "secret" ? (
          <span className="secret-input">
            <input id={id} value={value} onChange={onChange} type={revealedSecrets[key] ? "text" : "password"}
              placeholder={configuredSecrets[key] ? "已配置，留空则保留" : "尚未配置"} autoComplete="new-password" />
            <button type="button" className="icon-button" title={revealedSecrets[key] ? "隐藏" : "显示"}
              aria-label={revealedSecrets[key] ? `隐藏${meta.label}` : `显示${meta.label}`}
              onClick={() => setRevealedSecrets((previous) => ({ ...previous, [key]: !previous[key] }))}>
              {revealedSecrets[key] ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </span>
        ) : (
          <input id={id} value={value} onChange={onChange} type={meta.kind === "number" ? "number" : "text"}
            min={key === "zhihu_max_publications_per_day" ? 0 : undefined} step={key === "zhihu_max_publications_per_day" ? 1 : undefined} />
        )}
      </label>
    );
  }

  return (
    <div className="app-frame">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><Sparkles size={19} /></div><div><strong>AutoSci</strong><span>Research workflow</span></div></div>
        <nav className="primary-nav" aria-label="主导航">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return <button key={item.id} className={activeView === item.id ? "active" : ""} onClick={() => setActiveView(item.id)}><Icon size={18} /><span>{item.label}</span></button>;
          })}
        </nav>
        <div className="sidebar-status"><span className={`live-dot ${progress?.running ? "running" : ""}`} /><div><strong>{progress?.running ? "工作流运行中" : "系统待命"}</strong><span>{progress?.current_step ?? "idle"}</span></div></div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <div><div className="eyebrow">{progress?.run_id ? `RUN #${progress.run_id}` : "AUTOSCI CONSOLE"}</div><h1>{currentView.label}</h1></div>
          <div className="topbar-actions"><span className={`safety-state ${autoPublish ? "danger" : "safe"}`}><ShieldCheck size={15} />{autoPublish ? "允许公开发布" : "仅保存草稿"}</span>
            <button className="icon-button" onClick={() => void refreshAll()} disabled={loading} title="刷新状态" aria-label="刷新状态"><RefreshCw size={18} className={loading ? "spin" : ""} /></button></div>
        </header>

        {(statusMessage || errorMessage) && <div className="notification-stack" aria-live="polite">
          {statusMessage && <div className="notification success"><Check size={17} /><span>{statusMessage}</span><button onClick={() => setStatusMessage("")} aria-label="关闭"><X size={16} /></button></div>}
          {errorMessage && <div className="notification error"><CircleAlert size={17} /><span>{errorMessage}</span><button onClick={() => setErrorMessage("")} aria-label="关闭"><X size={16} /></button></div>}
        </div>}

        {activeView === "overview" && <div className="view-stack">
          {runErrorText && <div className="notification error automation-notice" role="status"><CircleAlert size={17} /><span>运行 #{runDetail?.run.id}：{runErrorText}</span></div>}
          {health?.zhihu_rate_limit?.active && <div className="notification error automation-notice" role="status"><CircleAlert size={17} /><span>知乎发布熔断：{health.zhihu_rate_limit.reason || "检测到平台访问限制"} 冷却参考至 {health.zhihu_rate_limit.until ? formatTime(health.zhihu_rate_limit.until, displayTimeZone) : "待确认"}。自动化不会自行重试。</span><button className="button secondary" onClick={() => void handleResetZhihuBreaker()} disabled={busyAction !== null}>我已确认知乎恢复</button></div>}
          {health?.automation_issues?.map((issue) => <div key={issue} className="notification error automation-notice" role="status"><CircleAlert size={17} /><span>{issue}</span></div>)}
          {health?.zhihu_rate_limit?.public_readback_mode === "published_api" && !health.zhihu_rate_limit.active && <div className="notification info automation-notice" role="status"><CircleAlert size={17} /><span>公开展示页的自动读取受限，现通过官方已发表记录与编辑器回读核验，不会因此重复发文或停止自动任务。</span></div>}
          <section className="command-bar"><div><strong>{health?.scheduler.running ? "服务与定时调度运行中" : "工作流控制"}</strong><span>下次定时执行：{nextRun ? formatTime(nextRun, displayTimeZone) : "等待服务状态"} · {displayTimeZone}{health?.automatic_publication ? " · 自动发布已启用" : " · 自动公开发布未启用"}{health?.zhihu_rate_limit?.active ? " · 知乎发布熔断中" : ""}{health?.daily_workflow ? ` · 今日任务：${dailyStatusLabel}（${health.daily_workflow.attempts}/3 次）` : ""}</span></div><div className="button-row">
            <button className="button secondary" onClick={handlePollInbox} disabled={busyAction !== null}>{busyAction === "mail" ? <ButtonSpinner /> : <MailCheck size={17} />}检查邮件</button>
            <button className="button primary" onClick={handleTriggerRun} disabled={busyAction !== null || progress?.running}>{busyAction === "trigger" ? <ButtonSpinner /> : <Play size={17} fill="currentColor" />}立即运行</button>
          </div></section>

          <section className="metric-grid" aria-label="运行指标">
            <article className="metric"><div className="metric-icon ink"><Activity size={18} /></div><div><span>最近运行</span><strong>{latestRun ? `#${latestRun.id}` : "-"}</strong><small className={`tone-${statusTone(latestRun?.status)}`}>{latestRun?.status ?? "暂无记录"}</small></div></article>
            <article className="metric"><div className="metric-icon teal"><FileText size={18} /></div><div><span>已选论文</span><strong>{String(latestSummary.selected_papers ?? 0)}</strong><small>目标 {settingsForm.paper_count} 篇</small></div></article>
            <article className="metric"><div className="metric-icon amber"><Clock3 size={18} /></div><div><span>当前步骤</span><strong className="compact-value">{progress?.current_step ?? "idle"}</strong><small>{progress?.message ?? "等待下一次任务"}</small></div></article>
            <article className="metric"><div className={`metric-icon ${autoPublish ? "red" : "green"}`}><ShieldCheck size={18} /></div><div><span>知乎策略</span><strong className="compact-value">{autoPublish ? "公开发布" : "保存草稿"}</strong><small>{settingsForm.zhihu_publish_enabled === "true" ? "流程已启用" : "流程已停用"}</small></div></article>
          </section>

          <section className="workspace-grid">
            <article className="tool-panel workflow-panel"><div className="panel-heading"><div><h2>工作流</h2><p>{progress?.message ?? "等待任务"}</p></div><div className="legend"><span><i className="pending" />待处理</span><span><i className="active" />进行中</span><span><i className="done" />完成</span></div></div><WorkflowGraphPanel graph={graph} progress={progress} /></article>
            <article className="tool-panel recent-panel"><div className="panel-heading"><div><h2>运行记录</h2><p>最近 {runs.length} 次</p></div><button className="icon-button danger-hover" onClick={handleDeleteSelectedRun} disabled={!selectedRunId || busyAction !== null} title="删除选中运行" aria-label="删除选中运行">{busyAction === "delete" ? <ButtonSpinner /> : <Trash2 size={17} />}</button></div>
              <div className="run-table">{runs.slice(0, 8).map((run) => <button key={run.id} className={run.id === selectedRunId ? "selected" : ""} onClick={() => setSelectedRunId(run.id)}><span className="run-id">#{run.id}</span><span className={`status-pill ${statusTone(run.status)}`}>{run.status}</span><time>{formatTime(run.started_at, displayTimeZone)}</time><ChevronRight size={16} /></button>)}{runs.length === 0 && <div className="empty-state"><Inbox size={22} /><span>暂无运行记录</span></div>}</div>
            </article>
          </section>
        </div>}

        {activeView === "publishing" && <div className="view-stack">
          <section className={`publish-mode ${autoPublish ? "public" : "draft"}`}><ShieldCheck size={20} /><div><strong>{autoPublish ? "公开发布模式" : "草稿模式"}</strong><span>{autoPublish ? "执行编号后会点击知乎发布按钮。" : "执行编号后只保存并验证知乎草稿。"}</span></div></section>
          <section className="tool-panel publish-console"><div className="panel-heading"><div><h2>编号处理</h2><p>多个编号可用逗号或空格分隔</p></div><select className="run-select" value={selectedRunId ?? ""} onChange={(event) => setSelectedRunId(Number(event.target.value))} aria-label="选择运行">{runs.map((run) => <option key={run.id} value={run.id}>Run #{run.id} · {run.status}</option>)}</select></div>
            <div className="publish-input-row"><input value={manualCodes} onChange={(event) => setManualCodes(event.target.value)} placeholder="R010-01, R010-03" onKeyDown={(event) => { if (event.key === "Enter") void handleManualPublish(); }} /><button className={`button ${autoPublish ? "danger" : "primary"}`} onClick={handleManualPublish} disabled={busyAction !== null}>{busyAction === "publish" ? <ButtonSpinner /> : autoPublish ? <Rocket size={17} /> : <Save size={17} />}{autoPublish ? "公开发布" : "保存并验收草稿"}</button></div>
          </section>
          <section className="tool-panel"><div className="panel-heading"><div><h2>发布任务</h2><p>Run #{selectedRunId ?? "-"}</p></div><span className="record-count">{runDetail?.publications?.length ?? 0} 项</span></div>
            <div className="table-scroll"><div className="publication-table"><div className="table-head"><span>编号</span><span>状态</span><span>标题 / 结果</span><span>更新时间</span></div>
              {runDetail?.publications?.map((task) => <div className="table-row" key={task.id}><strong>{task.paper_code}</strong><span><i className={`status-pill ${statusTone(task.status)}`}>{task.status === "SUPERSEDED" ? "已被替代" : task.status === "PUBLISHING" ? "发布中断待核实" : task.status === "PUBLISH_UNCERTAIN" ? "发布结果待核实" : task.status}</i></span><span className="result-cell">{task.zhihu_url && <a href={task.zhihu_url} target="_blank" rel="noreferrer">{task.status === "PUBLISHED" ? "查看已发布文章" : "打开知乎草稿"} <ExternalLink size={14} /></a>}{task.error_message ? <span className="publication-error error-text" title={task.error_message}>{task.error_message}</span> : !task.zhihu_url && task.title}{["PUBLISHING", "PUBLISH_UNCERTAIN"].includes(task.status) && <span className="reconcile-actions"><button className="button secondary" disabled={busyAction !== null} onClick={() => void handleReconcilePublication(task.paper_code, true)}>已公开</button><button className="button secondary" disabled={busyAction !== null} onClick={() => void handleReconcilePublication(task.paper_code, false)}>确认未公开</button></span>}</span><time>{formatTime(task.published_at, displayTimeZone)}</time></div>)}
              {!runDetail?.publications?.length && <div className="empty-state"><Rocket size={22} /><span>当前运行暂无发布任务</span></div>}
            </div>
            </div>
          </section>
          {manualResult?.results?.length ? <section className="tool-panel"><div className="panel-heading"><div><h2>本次结果</h2><p>{manualResult.results.length} 个编号</p></div></div><div className="result-list">{manualResult.results.map((item) => <div key={item.code}><strong>{item.code}</strong><span className={`status-pill ${statusTone(item.status)}`}>{item.status}</span><span className="result-cell">{item.zhihu_url && <a href={item.zhihu_url} target="_blank" rel="noreferrer">{["PUBLISHED", "ALREADY_PUBLISHED"].includes(item.status) ? "查看已发布文章" : "打开草稿"} <ExternalLink size={13} /></a>}{item.error && <span className="publication-error error-text">{item.error}</span>}{!item.zhihu_url && !item.error && "-"}</span></div>)}</div></section> : null}
        </div>}

        {activeView === "notes" && <div className="view-stack">
          <section className="notes-toolbar"><div><strong>论文解读</strong><span>{runDetail?.papers?.length ?? 0} 篇</span></div><select className="run-select" value={selectedRunId ?? ""} onChange={(event) => setSelectedRunId(Number(event.target.value))} aria-label="选择运行">{runs.map((run) => <option key={run.id} value={run.id}>Run #{run.id} · {formatTime(run.started_at, displayTimeZone)}</option>)}</select></section>
          <section className="notes-list">{runDetail?.papers?.map((paper, index) => <details className="paper-item" key={paper.id} open={index === 0}><summary><div className="paper-index">{String(index + 1).padStart(2, "0")}</div><div><h2>{paper.title}</h2><p>{paper.arxiv_id} · score {paper.score.toFixed(2)}{paper.quality && <span className={`quality-badge ${paper.quality.passed ? "passed" : "failed"}`}>{paper.quality.mode === "verified" ? "证据验证通过" : paper.quality.mode === "extractive_fallback" ? "安全摘录模式" : "质量未通过"} · {(paper.quality.score * 100).toFixed(0)}</span>}</p></div><ChevronRight size={18} className="summary-chevron" /></summary><div className="paper-actions"><a href={paper.url} target="_blank" rel="noreferrer">arXiv <ExternalLink size={13} /></a><a href={paper.pdf_url} target="_blank" rel="noreferrer">PDF <ExternalLink size={13} /></a></div><div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>{paper.note_markdown}</ReactMarkdown></div></details>)}
            {!runDetail?.papers?.length && <div className="empty-state large"><BookOpen size={28} /><span>当前运行暂无论文解读</span></div>}
          </section>
        </div>}

        {activeView === "settings" && <div className="settings-layout"><div className="settings-main">{SETTINGS_GROUPS.map((group) => <section className="settings-section" key={group.title}><div className="settings-section-heading"><h2>{group.title}</h2><p>{group.description}</p></div><div className="settings-grid">{group.keys.map(renderSettingField)}</div></section>)}</div>
          <aside className="settings-side"><div className="settings-save-box"><Settings2 size={19} /><h2>配置操作</h2><p>保存后调度时间与邮件轮询间隔会立即生效。</p><button className="button primary full" onClick={handleSaveSettings} disabled={busyAction !== null}>{busyAction === "settings" ? <ButtonSpinner /> : <Save size={17} />}保存配置</button><button className="button secondary full" onClick={handleTestLLM} disabled={busyAction !== null}>{busyAction === "llm" ? <ButtonSpinner /> : <Activity size={17} />}测试模型接口</button></div>
            <div className={`safety-box ${autoPublish ? "danger" : "safe"}`}><ShieldCheck size={18} /><div><strong>{autoPublish ? "公开发布已开启" : "公开发布已关闭"}</strong><span>{autoPublish ? "编号处理会产生公开文章。" : "编号处理只会生成知乎草稿。"}</span></div></div>
            {llmTestResult && <div className={`test-result ${llmTestResult.ok ? "success" : "error"}`}><strong>{llmTestResult.message}</strong><span>{llmTestResult.model ?? "-"} · {llmTestResult.latency_ms ?? "-"} ms</span>{llmTestResult.response_preview && <p>{llmTestResult.response_preview}</p>}</div>}
          </aside></div>}
      </main>
    </div>
  );
}
