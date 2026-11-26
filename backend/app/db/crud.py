import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings
from app.db.models import AppSetting, ArticleRevisionTask, BacktestReport, EmailCommandLog, Paper, PaperNote, PublicationTask, WorkflowRun


def create_run(db: Session, trigger_source: str) -> WorkflowRun:
    run = WorkflowRun(trigger_source=trigger_source, status="RUNNING", started_at=datetime.utcnow())
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_run(
    db: Session,
    run_id: int,
    status: str,
    summary: dict | None = None,
    error_message: str | None = None,
) -> WorkflowRun | None:
    run = db.get(WorkflowRun, run_id)
    if not run:
        return None
    run.status = status
    run.ended_at = datetime.utcnow()
    run.error_message = error_message
    run.summary = json.dumps(summary or {}, ensure_ascii=False)
    db.commit()
    db.refresh(run)
    return run


def list_runs(db: Session, limit: int = 20) -> list[WorkflowRun]:
    stmt = select(WorkflowRun).order_by(desc(WorkflowRun.id)).limit(limit)
    return list(db.scalars(stmt).all())


def scheduled_run_state(db: Session, settings: Settings, now: datetime | None = None) -> dict:
    """Use durable run history, not the scheduler's in-memory fire time."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings.timezone))
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    scheduled_at = midnight.replace(hour=settings.schedule_hour, minute=settings.schedule_minute)
    start = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    end = (midnight + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    runs = list(db.scalars(select(WorkflowRun).where(
        WorkflowRun.trigger_source == "scheduler",
        WorkflowRun.started_at >= start,
        WorkflowRun.started_at < end,
    ).order_by(desc(WorkflowRun.id))).all())
    state = {
        "date": local_now.date().isoformat(), "scheduled_at": scheduled_at.isoformat(),
        "attempts": len(runs), "last_run_id": runs[0].id if runs else None,
        "last_status": runs[0].status if runs else None, "due": False,
    }
    if local_now < scheduled_at:
        state["reason"] = "before_schedule"
    elif any(run.status in {"SUCCESS", "PARTIAL"} for run in runs):
        state["reason"] = "completed"
    elif any(run.status == "RUNNING" for run in runs):
        state["reason"] = "running"
    elif len(runs) >= 3:
        state["reason"] = "daily_retry_limit_reached"
    elif runs and local_now.astimezone(timezone.utc).replace(tzinfo=None) < (
        (runs[0].ended_at or runs[0].started_at) + timedelta(minutes=30)
    ):
        state["reason"] = "retry_backoff"
    else:
        state.update(due=True, reason="retry_due" if runs else "not_started")
    return state


def recover_interrupted_work(db: Session) -> dict[str, int]:
    """Called before workers start in the single-process deployment."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    runs = list(db.scalars(select(WorkflowRun).where(WorkflowRun.status == "RUNNING")).all())
    for run in runs:
        run.status = "INTERRUPTED"
        run.ended_at = now
        run.error_message = "Backend restarted before this run finished; scheduled work will be reconsidered."
    revisions = list(db.scalars(select(ArticleRevisionTask).where(ArticleRevisionTask.status == "RUNNING")).all())
    for task in revisions:
        task.status = "PENDING"
        task.next_attempt_at = now
    publications = list(db.scalars(select(PublicationTask).where(PublicationTask.status == "PUBLISHING")).all())
    for task in publications:
        task.status = "PUBLISH_UNCERTAIN"
        task.error_message = "Backend restarted during publication; verify the public result before retrying."
    db.commit()
    return {"runs": len(runs), "revisions": len(revisions), "uncertain_publications": len(publications)}


def delete_run(db: Session, run_id: int) -> bool:
    run = db.get(WorkflowRun, run_id)
    if not run:
        return False
    db.delete(run)
    db.commit()
    return True


def get_run(db: Session, run_id: int) -> WorkflowRun | None:
    stmt = (
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .options(
            joinedload(WorkflowRun.papers).joinedload(Paper.note),
            joinedload(WorkflowRun.backtest_reports),
            joinedload(WorkflowRun.publication_tasks),
        )
    )
    return db.execute(stmt).unique().scalar_one_or_none()


def get_latest_successful_run(db: Session) -> WorkflowRun | None:
    stmt = select(WorkflowRun).where(WorkflowRun.status == "SUCCESS").order_by(desc(WorkflowRun.id)).limit(1)
    return db.scalar(stmt)


def list_papers_for_run(db: Session, run_id: int) -> list[Paper]:
    stmt = (
        select(Paper)
        .where(Paper.run_id == run_id)
        .order_by(desc(Paper.score), Paper.id)
        .options(joinedload(Paper.note))
    )
    return list(db.scalars(stmt).all())


def list_recent_paper_id_title_pairs(db: Session, lookback_days: int) -> list[tuple[str, str]]:
    if lookback_days <= 0:
        return []
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    stmt = select(Paper.arxiv_id, Paper.title).where(Paper.created_at >= cutoff)
    rows = db.execute(stmt).all()
    return [(str(arxiv_id), str(title)) for arxiv_id, title in rows]


def get_paper_note(db: Session, paper_id: int) -> PaperNote | None:
    stmt = select(PaperNote).where(PaperNote.paper_id == paper_id)
    return db.scalar(stmt)


def get_latest_backtest_report(db: Session) -> BacktestReport | None:
    stmt = select(BacktestReport).order_by(desc(BacktestReport.id)).limit(1)
    return db.scalar(stmt)


def upsert_settings(db: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        item = db.scalar(select(AppSetting).where(AppSetting.key == key))
        if not item:
            item = AppSetting(key=key, value=value)
            db.add(item)
        else:
            item.value = value
    db.commit()


def list_settings(db: Session) -> dict[str, str]:
    stmt = select(AppSetting).order_by(AppSetting.key.asc())
    items = db.scalars(stmt).all()
    return {item.key: item.value for item in items}


def create_publication_task(
    db: Session,
    run_id: int,
    paper_id: int,
    paper_code: str,
    title: str,
    note_markdown: str,
) -> PublicationTask:
    task = PublicationTask(
        run_id=run_id,
        paper_id=paper_id,
        paper_code=paper_code,
        title=title,
        note_markdown=note_markdown,
        status="PENDING",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def list_publication_tasks(db: Session, run_id: int | None = None, status: str | None = None) -> list[PublicationTask]:
    stmt = select(PublicationTask).order_by(desc(PublicationTask.id))
    if run_id is not None:
        stmt = stmt.where(PublicationTask.run_id == run_id)
    if status:
        stmt = stmt.where(PublicationTask.status == status)
    return list(db.scalars(stmt).all())


def get_publication_task_by_code(db: Session, paper_code: str) -> PublicationTask | None:
    stmt = select(PublicationTask).where(PublicationTask.paper_code == paper_code.strip().upper())
    return db.scalar(stmt)


def update_publication_task(
    db: Session,
    task_id: int,
    status: str,
    zhihu_url: str | None = None,
    error_message: str | None = None,
    command_text: str | None = None,
    requested_by: str | None = None,
) -> PublicationTask | None:
    task = db.get(PublicationTask, task_id)
    if not task:
        return None
    task.status = status
    task.zhihu_url = zhihu_url
    task.error_message = error_message
    if command_text is not None:
        task.command_text = command_text
    if requested_by is not None:
        task.requested_by = requested_by
    if status == "PUBLISHED":
        task.published_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task


def get_processed_email_uids(db: Session) -> set[str]:
    stmt = select(EmailCommandLog.message_uid)
    return set(db.scalars(stmt).all())


def save_email_command_log(
    db: Session,
    message_uid: str,
    sender: str,
    subject: str,
    body: str,
    command_text: str,
    status: str,
    result: str | None,
) -> EmailCommandLog:
    record = EmailCommandLog(
        message_uid=message_uid,
        sender=sender,
        subject=subject,
        body=body,
        command_text=command_text,
        status=status,
        result=result,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
