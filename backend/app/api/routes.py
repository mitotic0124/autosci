import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import SECRET_SETTING_KEYS, get_settings
from app.db import crud
from app.db.session import get_db
from app.schemas.workflow import (
    BacktestReportOut,
    PaperNoteOut,
    PaperOut,
    PublicationTaskOut,
    TriggerResponse,
    WorkflowRunOut,
)

router = APIRouter()


class SettingsUpdateRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class PublishCodesRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)


class LLMTestRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class ZhihuBreakerResetRequest(BaseModel):
    confirmed_official_page_clear: bool = False


class ReconcilePublicationRequest(BaseModel):
    confirmed: bool = False
    published: bool
    public_url: str | None = None


def _without_secret_values(values: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in values.items() if key not in SECRET_SETTING_KEYS}


def _without_blank_secret_updates(values: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in values.items()
        if key not in SECRET_SETTING_KEYS or str(value).strip()
    }


def _secret_configuration(runtime: dict[str, str], settings: Any) -> dict[str, bool]:
    return {
        key: bool(str(runtime[key] if key in runtime else getattr(settings, key, "") or "").strip())
        for key in SECRET_SETTING_KEYS
    }


@router.get("/health")
def health_check(request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    settings = workflow_service._settings
    zhihu_state = workflow_service._publication_service._zhihu_service.rate_limit_status()
    daily = workflow_service.scheduled_status()
    issues = []
    if zhihu_state.get("session_error"):
        issues.append(zhihu_state["session_error"])
    if not (settings.workflow_auto_publish_after_run and settings.zhihu_auto_publish and settings.zhihu_publish_enabled):
        issues.append("自动公开发布未完全启用；生成或保存草稿不等于文章已发表。")
    if settings.llm_provider == "disabled" or not (settings.llm_api_key or settings.openai_api_key):
        issues.append("未配置可用的模型服务，无法自动生成并核验文章。")
    if settings.zhihu_firefox_profile_path and not Path(settings.zhihu_firefox_profile_path).expanduser().exists():
        issues.append("服务器上的知乎登录配置目录不存在，需要恢复登录配置。")
    if daily["reason"] == "daily_retry_limit_reached":
        issues.append("今日自动任务连续失败，已达到三次重试上限，请查看最近任务的错误。")
    return {
        "status": "ok", "running": workflow_service.is_running,
        "scheduler": request.app.state.scheduler.status(),
        "daily_workflow": daily,
        "automation_issues": issues,
        "public_publication": settings.zhihu_auto_publish,
        "automatic_publication": settings.workflow_auto_publish_after_run and settings.zhihu_auto_publish and settings.zhihu_publish_enabled,
        "zhihu_rate_limit": zhihu_state,
    }


@router.post("/api/zhihu/breaker/reset")
def reset_zhihu_breaker(
    payload: ZhihuBreakerResetRequest,
    request: Request,
) -> dict[str, Any]:
    if not payload.confirmed_official_page_clear:
        raise HTTPException(status_code=400, detail="请先在知乎官方页面确认当前账号可正常进入创作中心且风控提示已消失。")
    workflow_service = request.app.state.workflow_service
    try:
        result = workflow_service.clear_zhihu_breaker()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "zhihu_rate_limit": result}


@router.post("/api/workflow/trigger", response_model=TriggerResponse)
def trigger_workflow(request: Request) -> TriggerResponse:
    workflow_service = request.app.state.workflow_service
    accepted, message = workflow_service.trigger_async(trigger_source="manual")
    return TriggerResponse(accepted=accepted, message=message)


@router.post("/api/workflow/scheduled", response_model=TriggerResponse)
def trigger_scheduled_workflow(request: Request) -> TriggerResponse:
    accepted, message = request.app.state.workflow_service.trigger_async(trigger_source="scheduler")
    return TriggerResponse(accepted=accepted, message=message)


@router.get("/api/workflow/runs", response_model=list[WorkflowRunOut])
def list_workflow_runs(limit: int = 20, db: Session = Depends(get_db)) -> list[WorkflowRunOut]:
    return crud.list_runs(db, limit=limit)


@router.delete("/api/workflow/runs/{run_id}")
def delete_workflow_run(run_id: int, request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    result = workflow_service.delete_run(run_id)
    if not result.get("ok"):
        message = result.get("message", "Delete failed")
        if "running" in message.lower():
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=404, detail=message)
    return result


@router.get("/api/workflow/runs/{run_id}")
def get_workflow_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = crud.get_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")

    summary = json.loads(run.summary) if run.summary else {}
    quality_by_arxiv = {
        str(item.get("arxiv_id")): item
        for item in summary.get("article_quality", [])
        if isinstance(item, dict) and item.get("arxiv_id")
    }
    papers = [
        {
            "id": p.id,
            "title": p.title,
            "arxiv_id": p.arxiv_id,
            "score": p.score,
            "url": p.url,
            "pdf_url": p.pdf_url,
            "note_markdown": p.note.markdown_content if p.note else "",
            "image_paths": json.loads(p.note.image_paths) if p.note else [],
            "quality": quality_by_arxiv.get(p.arxiv_id),
        }
        for p in run.papers
    ]
    backtests = [
        {
            "id": r.id,
            "title": r.title,
            "markdown_content": r.markdown_content,
            "metrics": json.loads(r.metrics_json),
            "artifact_paths": json.loads(r.artifact_paths),
        }
        for r in run.backtest_reports
    ]
    publications = [
        {
            "id": t.id,
            "paper_code": t.paper_code,
            "title": t.title,
            "status": t.status,
            "zhihu_url": t.zhihu_url,
            "error_message": t.error_message,
            "published_at": t.published_at.isoformat() if t.published_at else None,
        }
        for t in run.publication_tasks
    ]

    return {
        "run": WorkflowRunOut.model_validate(run).model_dump(),
        "summary": summary,
        "papers": papers,
        "backtests": backtests,
        "publications": publications,
    }


@router.get("/api/papers/latest", response_model=list[PaperOut])
def list_latest_papers(db: Session = Depends(get_db)) -> list[PaperOut]:
    latest_run = crud.get_latest_successful_run(db)
    if not latest_run:
        return []
    return crud.list_papers_for_run(db, latest_run.id)


@router.get("/api/papers/{paper_id}/note", response_model=PaperNoteOut)
def get_paper_note(paper_id: int, db: Session = Depends(get_db)) -> PaperNoteOut:
    note = crud.get_paper_note(db, paper_id)
    if not note:
        raise HTTPException(status_code=404, detail="Paper note not found.")
    return PaperNoteOut.model_validate(note)


@router.get("/api/backtests/latest", response_model=BacktestReportOut | None)
def get_latest_backtest_report(db: Session = Depends(get_db)) -> BacktestReportOut | None:
    report = crud.get_latest_backtest_report(db)
    if not report:
        return None
    return BacktestReportOut.model_validate(report)


@router.get("/api/publications", response_model=list[PublicationTaskOut])
def list_publication_tasks(
    request: Request,
    run_id: int | None = None,
    status: str | None = None,
) -> list[PublicationTaskOut]:
    workflow_service = request.app.state.workflow_service
    tasks = workflow_service.list_publication_tasks(run_id=run_id, status=status)
    return [PublicationTaskOut.model_validate(task) for task in tasks]


@router.post("/api/publications/poll-mail")
def poll_publication_commands(request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    return workflow_service.poll_publish_commands()


@router.post("/api/publications/publish")
def publish_by_codes(payload: PublishCodesRequest, request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    if not payload.codes:
        raise HTTPException(status_code=400, detail="codes is required.")
    return workflow_service.publish_codes(payload.codes, sender="manual_api")


@router.post("/api/publications/{paper_code}/reconcile")
def reconcile_publication(
    paper_code: str,
    payload: ReconcilePublicationRequest,
    request: Request,
) -> dict[str, Any]:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="请先核对知乎创作中心和公开文章页，并明确确认检查结果。")
    workflow_service = request.app.state.workflow_service
    try:
        return workflow_service.reconcile_publication(
            paper_code.strip().upper(),
            published=payload.published,
            public_url=payload.public_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/workflow/graph")
def get_workflow_graph(request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    return workflow_service.get_graph()


@router.get("/api/workflow/progress")
def get_workflow_progress(request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    return workflow_service.get_progress()


@router.post("/api/llm/test")
def test_llm_connection(payload: LLMTestRequest, request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    return workflow_service.test_llm_connection(payload.values)


@router.get("/api/settings")
def get_runtime_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    runtime = crud.list_settings(db)
    return {
        "defaults": {
            "schedule_hour": settings.schedule_hour,
            "schedule_minute": settings.schedule_minute,
            "paper_count": settings.paper_count,
            "arxiv_max_candidates": settings.arxiv_max_candidates,
            "paper_history_lookback_days": settings.paper_history_lookback_days,
            "arxiv_extra_keywords": settings.arxiv_extra_keywords,
            "llm_provider": settings.llm_provider,
            "llm_api_base_url": settings.llm_api_base_url or "",
            "llm_api_key": "",
            "llm_model": settings.llm_model,
            "llm_article_model": settings.llm_article_model or "",
            "llm_vision_model": settings.llm_vision_model or "",
            "llm_verifier_model": settings.llm_verifier_model or "",
            "llm_request_timeout_seconds": settings.llm_request_timeout_seconds,
            "llm_max_output_tokens": settings.llm_max_output_tokens,
            "llm_thinking_budget": settings.llm_thinking_budget,
            "llm_reasoning_effort": settings.llm_reasoning_effort,
            "llm_use_responses_api": settings.llm_use_responses_api,
            "llm_note_system_prompt": settings.llm_note_system_prompt,
            "llm_score_system_prompt": settings.llm_score_system_prompt,
            "article_fetch_arxiv_source": settings.article_fetch_arxiv_source,
            "article_max_source_chars": settings.article_max_source_chars,
            "article_max_claims": settings.article_max_claims,
            "article_max_figure_candidates": settings.article_max_figure_candidates,
            "article_min_images": settings.article_min_images,
            "article_max_images": settings.article_max_images,
            "article_min_figure_score": settings.article_min_figure_score,
            "article_quality_threshold": settings.article_quality_threshold,
            "article_max_repair_attempts": settings.article_max_repair_attempts,
            "article_max_quality_retries": settings.article_max_quality_retries,
            "article_parallel_workers": settings.article_parallel_workers,
            "article_require_verified": settings.article_require_verified,
            "article_min_paragraphs": settings.article_min_paragraphs,
            "article_min_sections": settings.article_min_sections,
            "article_min_chars": settings.article_min_chars,
            "mail_to": settings.mail_to or "",
            "mail_from": settings.mail_from or "",
            "smtp_host": settings.smtp_host or "",
            "smtp_port": settings.smtp_port,
            "imap_host": settings.imap_host or "",
            "imap_port": settings.imap_port,
            "imap_username": settings.imap_username or "",
            "imap_password": "",
            "imap_use_ssl": settings.imap_use_ssl,
            "imap_folder": settings.imap_folder,
            "command_poll_interval_seconds": settings.command_poll_interval_seconds,
            "command_authorized_sender": settings.command_authorized_sender or "",
            "zhihu_column_url": settings.zhihu_column_url or "",
            "zhihu_column_name": settings.zhihu_column_name,
            "zhihu_default_tags": settings.zhihu_default_tags,
            "zhihu_topic_api_enabled": settings.zhihu_topic_api_enabled,
            "zhihu_topic_max_count": settings.zhihu_topic_max_count,
            "zhihu_topic_min_relevance": settings.zhihu_topic_min_relevance,
            "zhihu_firefox_profile_path": settings.zhihu_firefox_profile_path or "",
            "zhihu_headless": settings.zhihu_headless,
            "zhihu_publish_enabled": settings.zhihu_publish_enabled,
            "zhihu_auto_publish": settings.zhihu_auto_publish,
            "zhihu_max_publications_per_day": settings.zhihu_max_publications_per_day,
            "zhihu_min_interaction_interval_seconds": settings.zhihu_min_interaction_interval_seconds,
            "workflow_auto_publish_after_run": settings.workflow_auto_publish_after_run,
            "zhihu_markdown_converter_cmd": settings.zhihu_markdown_converter_cmd,
            "timezone": settings.timezone,
        },
        "runtime": _without_secret_values(runtime),
        "secret_configured": _secret_configuration(runtime, settings),
    }


@router.put("/api/settings")
def update_runtime_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    updates = _without_blank_secret_updates({k: str(v) for k, v in payload.values.items()})
    allowed_keys = {
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
        "llm_article_model",
        "llm_vision_model",
        "llm_verifier_model",
        "llm_request_timeout_seconds",
        "llm_max_output_tokens",
        "llm_thinking_budget",
        "llm_reasoning_effort",
        "llm_use_responses_api",
        "llm_note_system_prompt",
        "llm_score_system_prompt",
        "article_fetch_arxiv_source",
        "article_max_source_chars",
        "article_max_claims",
        "article_max_figure_candidates",
        "article_min_images",
        "article_max_images",
        "article_min_figure_score",
        "article_quality_threshold",
        "article_max_repair_attempts",
        "article_max_quality_retries",
        "article_parallel_workers",
        "article_require_verified",
        "article_min_paragraphs",
        "article_min_sections",
        "article_min_chars",
        "mail_to",
        "mail_from",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password",
        "smtp_use_tls",
        "imap_host",
        "imap_port",
        "imap_username",
        "imap_password",
        "imap_use_ssl",
        "imap_folder",
        "command_poll_interval_seconds",
        "command_authorized_sender",
        "zhihu_column_url",
        "zhihu_column_name",
        "zhihu_default_tags",
        "zhihu_topic_api_enabled",
        "zhihu_topic_max_count",
        "zhihu_topic_min_relevance",
        "zhihu_firefox_profile_path",
        "zhihu_headless",
        "zhihu_publish_enabled",
        "zhihu_auto_publish",
        "zhihu_max_publications_per_day",
        "zhihu_min_interaction_interval_seconds",
        "workflow_auto_publish_after_run",
        "zhihu_markdown_converter_cmd",
        "timezone",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed_keys}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid settings fields provided.")

    crud.upsert_settings(db, filtered)
    merged = crud.list_settings(db)

    settings = get_settings()
    request.app.state.workflow_service._apply_runtime_settings(db)
    schedule_hour = int(merged.get("schedule_hour", settings.schedule_hour))
    schedule_minute = int(merged.get("schedule_minute", settings.schedule_minute))
    poll_seconds = int(merged.get("command_poll_interval_seconds", settings.command_poll_interval_seconds))
    request.app.state.scheduler.reschedule(schedule_hour, schedule_minute)
    request.app.state.scheduler.repoll(poll_seconds)

    return {
        "ok": True,
        "runtime": _without_secret_values(merged),
        "secret_configured": _secret_configuration(merged, settings),
    }
