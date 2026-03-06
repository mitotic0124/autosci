import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
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


@router.get("/health")
def health_check(request: Request) -> dict[str, Any]:
    workflow_service = request.app.state.workflow_service
    return {"status": "ok", "running": workflow_service.is_running}


@router.post("/api/workflow/trigger", response_model=TriggerResponse)
def trigger_workflow(request: Request) -> TriggerResponse:
    workflow_service = request.app.state.workflow_service
    accepted, message = workflow_service.trigger_async(trigger_source="manual")
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
    summary = json.loads(run.summary) if run.summary else {}
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
            "llm_provider": settings.llm_provider,
            "llm_api_base_url": settings.llm_api_base_url or "",
            "llm_api_key": settings.llm_api_key or "",
            "llm_model": settings.llm_model,
            "llm_note_system_prompt": settings.llm_note_system_prompt,
            "llm_score_system_prompt": settings.llm_score_system_prompt,
            "mail_to": settings.mail_to or "",
            "mail_from": settings.mail_from or "",
            "smtp_host": settings.smtp_host or "",
            "smtp_port": settings.smtp_port,
            "imap_host": settings.imap_host or "",
            "imap_port": settings.imap_port,
            "imap_username": settings.imap_username or "",
            "imap_password": settings.imap_password or "",
            "imap_use_ssl": settings.imap_use_ssl,
            "imap_folder": settings.imap_folder,
            "command_poll_interval_seconds": settings.command_poll_interval_seconds,
            "command_authorized_sender": settings.command_authorized_sender or "",
            "zhihu_column_url": settings.zhihu_column_url or "",
            "zhihu_firefox_profile_path": settings.zhihu_firefox_profile_path or "",
            "zhihu_headless": settings.zhihu_headless,
            "zhihu_publish_enabled": settings.zhihu_publish_enabled,
            "zhihu_markdown_converter_cmd": settings.zhihu_markdown_converter_cmd,
            "timezone": settings.timezone,
        },
        "runtime": runtime,
    }


@router.put("/api/settings")
def update_runtime_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    updates = {k: str(v) for k, v in payload.values.items()}
    allowed_keys = {
        "schedule_hour",
        "schedule_minute",
        "paper_count",
        "arxiv_max_candidates",
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
        "zhihu_firefox_profile_path",
        "zhihu_headless",
        "zhihu_publish_enabled",
        "zhihu_markdown_converter_cmd",
        "timezone",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed_keys}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid settings fields provided.")

    crud.upsert_settings(db, filtered)
    merged = crud.list_settings(db)

    settings = get_settings()
    schedule_hour = int(merged.get("schedule_hour", settings.schedule_hour))
    schedule_minute = int(merged.get("schedule_minute", settings.schedule_minute))
    poll_seconds = int(merged.get("command_poll_interval_seconds", settings.command_poll_interval_seconds))
    request.app.state.scheduler.reschedule(schedule_hour, schedule_minute)
    request.app.state.scheduler.repoll(poll_seconds)

    return {"ok": True, "runtime": merged}
