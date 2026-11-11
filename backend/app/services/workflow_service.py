import json
import logging
import re
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import SECRET_SETTING_KEYS, Settings
from app.db import crud
from app.db.models import ArticleRevisionTask, Paper, PaperNote, PublicationTask, WorkflowRun
from app.db.session import SessionLocal
from app.services.arxiv_service import ArxivService, CandidatePaper
from app.services.email_service import EmailService
from app.services.llm_service import LLMProviderUnavailable, LLMService
from app.services.paper_pipeline import ArticleQualityError, PaperUnderstandingPipeline
from app.services.publication_service import PublicationService

logger = logging.getLogger(__name__)


class WorkflowService:
    STEP_IDS = ["trigger", "fetch", "select", "note", "mail", "reply", "zhihu"]
    INT_FIELDS = {
        "smtp_port",
        "schedule_hour",
        "schedule_minute",
        "paper_count",
        "arxiv_max_candidates",
        "paper_history_lookback_days",
        "zhihu_max_publications_per_day",
        "zhihu_min_interaction_interval_seconds",
        "imap_port",
        "command_poll_interval_seconds",
        "article_max_source_chars",
        "article_max_claims",
        "article_max_figure_candidates",
        "article_min_images",
        "article_max_images",
        "article_max_repair_attempts",
        "article_max_quality_retries",
        "article_max_quality_candidates",
        "article_parallel_workers",
        "article_min_paragraphs",
        "article_min_sections",
        "article_min_chars",
        "llm_request_timeout_seconds",
        "llm_max_output_tokens",
        "llm_thinking_budget",
    }
    FLOAT_FIELDS = {"article_min_figure_score", "article_quality_threshold"}
    BOOL_FIELDS = {
        "smtp_use_tls",
        "imap_use_ssl",
        "zhihu_headless",
        "zhihu_publish_enabled",
        "zhihu_auto_publish",
        "workflow_auto_publish_after_run",
        "article_fetch_arxiv_source",
        "article_require_verified",
        "llm_use_responses_api",
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm_service = LLMService(settings)
        self._arxiv_service = ArxivService(settings, self._llm_service)
        self._paper_pipeline = PaperUnderstandingPipeline(self._llm_service, settings)
        self._email_service = EmailService(settings)
        self._publication_service = PublicationService(settings)

        self._lock = threading.Lock()
        self._running = False
        self._command_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress = self._new_progress_state()

    @property
    def is_running(self) -> bool:
        return self._running

    def get_progress(self) -> dict:
        with self._progress_lock:
            return {
                "running": self._progress["running"],
                "mode": self._progress["mode"],
                "run_id": self._progress["run_id"],
                "current_step": self._progress["current_step"],
                "message": self._progress["message"],
                "updated_at": self._progress["updated_at"],
                "node_status": dict(self._progress["node_status"]),
            }

    def trigger_async(self, trigger_source: str = "manual") -> tuple[bool, str]:
        if self._running:
            return False, "Workflow is already running."
        thread = threading.Thread(target=self.run_pipeline, kwargs={"trigger_source": trigger_source}, daemon=True)
        thread.start()
        return True, "Workflow accepted."

    def run_pipeline(self, trigger_source: str = "manual") -> dict | None:
        if not self._lock.acquire(blocking=False):
            logger.info("Workflow deferred because another run is executing.")
            return {"skipped": True, "reason": "workflow_busy"}

        self._running = True

        db = None
        run_id: int | None = None
        email_error: str | None = None
        try:
            db = SessionLocal()
            self._apply_runtime_settings(db)
            if trigger_source == "scheduler":
                state = crud.scheduled_run_state(db, self._settings)
                if not state["due"]:
                    return {"skipped": True, "reason": state["reason"]}
            self._start_progress(mode="workflow", message="Workflow started.")
            self._set_step("trigger", "active", "Initializing run context.")
            run = crud.create_run(db, trigger_source=trigger_source)
            run_id = run.id
            self._set_run_id(run_id)
            self._set_step("trigger", "done", f"Run #{run_id} created.")
            self._rebuild_ai_services()

            run_dir = Path(self._settings.artifacts_dir) / "runs" / f"run_{run_id}"
            run_dir.mkdir(parents=True, exist_ok=True)

            self._set_step("fetch", "active", "Fetching recent arXiv candidates.")
            fetch_warnings: list[str] = []
            fetched_window = self._arxiv_service.fetch_recent_candidates(hours_back=24 * 30)
            revision_ids = {value for (value,) in db.query(ArticleRevisionTask.arxiv_id).filter(
                ArticleRevisionTask.status.in_(["PENDING", "RUNNING"])
            ).all()}
            fetched_window = [item for item in fetched_window if item.arxiv_id not in revision_ids]
            recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            recent_candidates = [item for item in fetched_window if item.published_at >= recent_cutoff]
            if len(recent_candidates) >= self._settings.paper_count:
                candidates = recent_candidates
            else:
                candidates = fetched_window
                if len(recent_candidates) < len(fetched_window):
                    fetch_warnings.append(
                        f"Only {len(recent_candidates)} candidates were published in the last 48 hours; "
                        "using the already fetched 30-day window to avoid a second API request."
                    )

            history_days = max(0, int(self._settings.paper_history_lookback_days))
            filtered_count = 0
            if history_days > 0:
                recent_pairs = crud.list_recent_paper_id_title_pairs(db, lookback_days=history_days)
                seen_ids = {arxiv_id.strip() for arxiv_id, _ in recent_pairs if arxiv_id}
                seen_titles = {self._normalize_title(title) for _, title in recent_pairs if title}
                before_filter = len(candidates)
                filtered_candidates = self._filter_history_candidates(candidates, seen_ids=seen_ids, seen_titles=seen_titles)
                filtered_count = before_filter - len(filtered_candidates)
                if filtered_candidates:
                    candidates = filtered_candidates
                elif before_filter > 0:
                    candidates = []
                    crud.finish_run(db, run_id, status="SUCCESS", summary={
                        "selected_papers": 0, "publication_tasks": 0,
                        "reason": "All fetched papers were already processed within the history window.",
                    })
                    self._finish_progress(success=True, message="No new papers; duplicate publication skipped.")
                    return

                if filtered_count > 0 and candidates is filtered_candidates:
                    logger.info(
                        "History filter removed %s candidate(s) within %s day(s).",
                        filtered_count,
                        history_days,
                    )

            if not candidates:
                raise RuntimeError(
                    "No arXiv candidates available after fetch and fallback window expansion."
                )
            self._set_step(
                "fetch",
                "done",
                f"Fetched {len(candidates)} candidates after history filter (removed {filtered_count}).",
            )

            self._set_step("select", "active", "Ranking papers and selecting top candidates.")
            # Keep a small reserve so an article rejected by the strict quality
            # gate can be replaced without turning a daily run into an
            # unbounded paper-processing job.
            requested_papers = max(1, int(self._settings.paper_count))
            candidate_limit = self._quality_candidate_limit(
                len(candidates),
                requested_papers,
                int(getattr(self._settings, "article_max_quality_candidates", 5)),
            )
            selected = self._arxiv_service.rank_and_pick(candidates, top_k=candidate_limit)
            if not selected:
                raise RuntimeError("No arXiv papers were selected for this run.")
            self._set_step(
                "select",
                "done",
                f"Selected up to {requested_papers} verified articles with {max(0, len(selected) - requested_papers)} bounded reserve candidate(s).",
            )

            self._set_step("note", "active", "Building evidence-grounded paper interpretations.")
            papers: list[Paper] = []
            notes_map: dict[int, str] = {}
            quality_reports: list[dict] = []
            paper_errors: list[dict[str, str]] = []
            total_selected = len(selected)
            quality_attempts = 0
            quality_revisions: list[dict[str, object]] = []
            max_quality_retries = max(0, int(getattr(self._settings, "article_max_quality_retries", 2)))
            for idx, (candidate, score) in enumerate(selected, start=1):
                if len(papers) >= requested_papers:
                    break
                paper_base = run_dir / "papers" / candidate.arxiv_id.replace("/", "_")
                paper_base.mkdir(parents=True, exist_ok=True)
                pdf_path = paper_base / f"{candidate.arxiv_id.replace('/', '_')}.pdf"

                article = None
                candidate_errors: list[str] = []
                for revision in range(max_quality_retries + 1):
                    quality_attempts += 1
                    self._set_step(
                        "note",
                        "active",
                        f"Understanding paper {idx}/{total_selected}: {candidate.arxiv_id} "
                        f"(quality pass {revision + 1}/{max_quality_retries + 1})",
                    )
                    try:
                        build_article = self._paper_pipeline.run if revision == 0 else self._paper_pipeline.revise_from_checkpoint
                        article = build_article(
                            candidate,
                            pdf_path=pdf_path,
                            paper_dir=paper_base,
                            progress_callback=lambda message, arxiv_id=candidate.arxiv_id: self._set_step(
                                "note",
                                "active",
                                f"{arxiv_id}: {message}",
                            ),
                        )
                        break
                    except ArticleQualityError as exc:
                        message = str(exc)[:1800]
                        candidate_errors.append(message)
                        quality_revisions.append({
                            "arxiv_id": candidate.arxiv_id,
                            "attempt": revision + 1,
                            "status": "QUALITY_REJECTED",
                            "error": message,
                        })
                        self._queue_article_revision(db, run_id, candidate, score, paper_base, message)
                        logger.warning(
                            "Quality gate rejected %s on pass %s/%s; returning to revision before trying another candidate.",
                            candidate.arxiv_id,
                            revision + 1,
                            max_quality_retries + 1,
                        )
                        if revision < max_quality_retries:
                            self._set_step(
                                "note",
                                "active",
                                f"{candidate.arxiv_id}: quality gate rejected; regenerating the same paper before fallback selection.",
                            )
                        continue
                    except LLMProviderUnavailable:
                        # A provider quota/auth outage is not an article quality
                        # failure. Abort the run so it is recorded as FAILED and
                        # the daily worker retries it later; never queue a false
                        # rework item from an empty provider-degraded draft.
                        raise
                    except Exception as exc:
                        logger.exception("Paper understanding failed for %s: %s", candidate.arxiv_id, exc)
                        message = str(exc)[:1800]
                        candidate_errors.append(message)
                        # Older pipeline checkpoints can surface a rejected QA
                        # report as a generic RuntimeError. Treat that report
                        # as a quality rejection as well, so it is returned to
                        # the revision queue instead of disappearing with the
                        # current run.
                        rejected_report = self._read_failed_quality_report(paper_base)
                        if rejected_report is not None:
                            candidate_revision_count = sum(
                                item.get("arxiv_id") == candidate.arxiv_id
                                for item in quality_revisions
                            )
                            quality_revisions.append({
                                "arxiv_id": candidate.arxiv_id,
                                "attempt": candidate_revision_count + 1,
                                "status": "QUALITY_REJECTED",
                                "error": message,
                                "metrics": rejected_report.get("metrics", {}),
                            })
                            self._queue_article_revision(db, run_id, candidate, score, paper_base, message)
                        break

                if article is None:
                    paper_errors.append({
                        "arxiv_id": candidate.arxiv_id,
                        "error": "本轮处理未通过；质量失败稿已保留待自动返工。 " + " | ".join(candidate_errors),
                    })
                    continue

                note_markdown = article.markdown
                image_urls = article.selected_image_urls
                quality_reports.append(
                    {
                        "arxiv_id": candidate.arxiv_id,
                        "passed": article.quality_report.passed,
                        "score": article.quality_report.score,
                        "mode": article.quality_report.mode,
                        "metrics": article.quality_report.metrics,
                    }
                )

                db_paper = Paper(
                    run_id=run_id,
                    arxiv_id=candidate.arxiv_id,
                    title=candidate.title,
                    authors=", ".join(candidate.authors),
                    abstract=candidate.abstract,
                    categories=", ".join(candidate.categories),
                    published_at=candidate.published_at.replace(tzinfo=None),
                    url=candidate.url,
                    pdf_url=candidate.pdf_url,
                    score=float(score),
                )
                db.add(db_paper)
                db.commit()
                db.refresh(db_paper)

                db_note = PaperNote(
                    paper_id=db_paper.id,
                    markdown_content=note_markdown,
                    image_paths=json.dumps(image_urls, ensure_ascii=False),
                )
                db.add(db_note)
                db.commit()

                papers.append(db_paper)
                notes_map[db_paper.id] = note_markdown
                pending_revision = db.query(ArticleRevisionTask).filter_by(arxiv_id=candidate.arxiv_id).first()
                if pending_revision:
                    pending_revision.status = "PASSED"
                    pending_revision.error_message = None
                    db.commit()
            if not papers:
                pending_revisions = db.query(ArticleRevisionTask).filter(
                    ArticleRevisionTask.run_id == run_id,
                    ArticleRevisionTask.status.in_(["PENDING", "RUNNING"]),
                ).count()
                if not pending_revisions:
                    raise RuntimeError(
                        "No verified articles or resumable quality checkpoints were produced: "
                        + " | ".join(item["error"] for item in paper_errors)[:3000]
                    )
                # A run with only rejected articles is still a completed
                # workflow: its quality checkpoints are durable and the
                # revision worker owns the next attempt. Do not turn this
                # into a silent "no publish today" outcome or discard the
                # queued papers by aborting the run transaction.
                summary = {
                    "paper_candidates": len(candidates),
                    "quality_candidates_attempted": len(paper_errors),
                    "quality_attempts": quality_attempts,
                    "quality_revisions": quality_revisions,
                    "quality_target": requested_papers,
                    "selected_papers": 0,
                    "publication_tasks": 0,
                    "article_quality": quality_reports,
                    "paper_errors": paper_errors,
                    "fetch_warnings": fetch_warnings,
                    "auto_publish_after_run": {"enabled": bool(self._settings.workflow_auto_publish_after_run), "queued": 0, "published": 0},
                    "reason": "全部候选文章未通过质量门槛，已进入持久化返工队列；本次不发布未核验内容。",
                }
                crud.finish_run(db, run_id, status="PARTIAL", summary=summary)
                if trigger_source == "scheduler":
                    self._send_automation_alert(db, run_id, summary["reason"])
                self._set_step("note", "failed", "所有候选文章均未通过质量门槛，已排入自动返工队列。")
                self._set_step("zhihu", "pending", "质量返工通过后进入自动发布队列。")
                self._finish_progress(success=False, message=f"Run #{run_id} retained rejected articles for revision.")
                return
            self._set_step(
                "note",
                "done",
                f"Generated {len(papers)} verified paper interpretation(s); rejected {len(paper_errors)}.",
            )

            publication_tasks = self._publication_service.create_tasks_for_run(db, run_id, papers, notes_map)
            run.summary = json.dumps({"article_quality": quality_reports}, ensure_ascii=False)
            db.commit()
            auto_publish_summary = {
                "enabled": bool(self._settings.workflow_auto_publish_after_run),
                "requested": 0,
                "published": 0,
                "drafted": 0,
                "already_published": 0,
                "failed": 0,
                "rate_limited": 0,
                "queued": 0,
                "uncertain": 0,
                "not_found": 0,
            }
            if self._settings.workflow_auto_publish_after_run:
                auto_codes, queued_count = self._publication_service.select_auto_publication_window(
                    db,
                    publication_tasks,
                )
                auto_publish_summary["requested"] = len(publication_tasks)
                auto_publish_summary["queued"] = queued_count
                if auto_codes:
                    if not self._settings.zhihu_auto_publish:
                        logger.info(
                            "workflow_auto_publish_after_run is enabled while zhihu_auto_publish=false; "
                            "the auto stage will save drafts."
                        )
                    self._set_step("zhihu", "active", f"Auto publishing {len(auto_codes)} paper(s) to Zhihu.")
                    publish_result = self._publication_service.publish_codes(
                        db=db,
                        codes=auto_codes,
                        sender="auto_workflow",
                        command_text="auto_publish_after_run",
                        step_callback=self._on_publish_step,
                    )
                    stats = publish_result.get("stats", {})
                    auto_publish_summary["published"] = int(stats.get("PUBLISHED", 0))
                    auto_publish_summary["drafted"] = int(stats.get("DRAFT", 0))
                    auto_publish_summary["already_published"] = int(stats.get("ALREADY_PUBLISHED", 0))
                    auto_publish_summary["failed"] = int(stats.get("FAILED", 0))
                    auto_publish_summary["rate_limited"] = int(stats.get("RATE_LIMITED", 0))
                    auto_publish_summary["queued"] = max(
                        auto_publish_summary["queued"],
                        int(stats.get("PENDING", 0)),
                    )
                    auto_publish_summary["uncertain"] = int(stats.get("PUBLISH_UNCERTAIN", 0))
                    auto_publish_summary["not_found"] = int(stats.get("NOT_FOUND", 0))
                    failed_total = (
                        auto_publish_summary["failed"]
                        + auto_publish_summary["rate_limited"]
                        + auto_publish_summary["uncertain"]
                        + auto_publish_summary["not_found"]
                    )
                    if failed_total > 0:
                        self._set_step(
                            "zhihu",
                            "failed",
                            "Auto publish completed with failures: "
                            f"published={auto_publish_summary['published']}, "
                            f"draft={auto_publish_summary['drafted']}, failed={failed_total}.",
                        )
                    else:
                        self._set_step(
                            "zhihu",
                            "done",
                            "Auto publish completed: "
                            f"published={auto_publish_summary['published']}, "
                            f"draft={auto_publish_summary['drafted']}.",
                        )
                else:
                    if queued_count:
                        self._set_step("zhihu", "done", f"Queued {queued_count} publication(s) for a later daily window.")
                    else:
                        self._set_step("zhihu", "done", "No publication tasks to auto publish.")
            else:
                self._set_step("zhihu", "pending", "Auto publish after workflow is disabled.")

            # Reload to include updated task statuses after optional auto publish.
            publication_tasks = crud.list_publication_tasks(db, run_id=run_id)

            self._set_step("mail", "active", "Sending paper codes email.")
            email_sent = False
            try:
                email_sent = self._email_service.send_paper_code_digest(run_id=run_id, tasks=publication_tasks)
            except Exception as exc:
                email_error = str(exc)
                logger.warning("Email send failed: %s", exc)
            self._set_step("mail", "done", "Email stage finished.")

            summary = {
                "paper_candidates": len(candidates),
                "quality_candidates_attempted": len(quality_reports) + len(paper_errors),
                "quality_attempts": quality_attempts,
                "quality_revisions": quality_revisions,
                "quality_target": requested_papers,
                "selected_papers": len(papers),
                "publication_tasks": len(publication_tasks),
                "email_sent": email_sent,
                "email_error": email_error,
                "article_quality": quality_reports,
                "paper_errors": paper_errors,
                "fetch_warnings": fetch_warnings,
                "auto_publish_after_run": auto_publish_summary,
            }
            failures = bool(
                paper_errors
                or auto_publish_summary["failed"]
                or auto_publish_summary["rate_limited"]
                or auto_publish_summary["uncertain"]
                or auto_publish_summary["not_found"]
            )
            status = "PARTIAL" if failures else "SUCCESS"
            crud.finish_run(db, run_id, status=status, summary=summary)
            self._finish_progress(success=not failures, message=f"Run #{run_id} finished: {status}.")
        except Exception as exc:
            logger.exception("Workflow run failed: %s", exc)
            self._mark_current_step_failed(str(exc))
            if run_id is not None:
                previous = json.loads(run.summary or "{}") if run is not None else {}
                previous.update(email_error=email_error, paper_errors=locals().get("paper_errors", []), article_quality=locals().get("quality_reports", []), quality_revisions=locals().get("quality_revisions", []))
                crud.finish_run(db, run_id, status="FAILED", error_message=str(exc), summary=previous)
                if trigger_source == "scheduler" and crud.scheduled_run_state(db, self._settings)["reason"] == "daily_retry_limit_reached":
                    self._send_automation_alert(db, run_id, str(exc))
            self._finish_progress(success=False, message=f"Run failed: {exc}")
        finally:
            if db is not None:
                db.close()
            self._running = False
            self._lock.release()

    def scheduled_status(self) -> dict:
        with SessionLocal() as db:
            return crud.scheduled_run_state(db, self._settings)

    def _send_automation_alert(self, db: Session, run_id: int, error: str) -> None:
        try:
            sent = self._email_service.send_workflow_alert(run_id, error)
            run = db.get(WorkflowRun, run_id)
            summary = json.loads(run.summary or "{}")
            summary["automation_alert_sent"] = bool(sent)
            run.summary = json.dumps(summary, ensure_ascii=False)
            db.commit()
            if not sent:
                logger.error("Automatic workflow alert could not be delivered for run %s.", run_id)
        except Exception:
            db.rollback()
            logger.exception("Automatic workflow alert failed for run %s.", run_id)

    def _daily_work_has_priority(self, db: Session) -> bool:
        return crud.scheduled_run_state(db, self._settings)["due"]

    def _acquire_workflow_lock(self, *, timeout_seconds: float = 0.0) -> bool:
        """Acquire the single-workflow lock, optionally waiting for a short job.

        The one-minute scheduler catch-up check and the maintenance workers are
        triggered by APScheduler at nearly the same instant. A non-blocking
        acquire lets the short catch-up call win the lock every time and starve
        the publication/revision workers for days. Waiting briefly keeps the
        mutual exclusion while letting maintenance jobs run once the fast
        scheduler probe has finished.
        """
        if self._lock.acquire(blocking=False):
            return True
        if timeout_seconds <= 0:
            return False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if self._lock.acquire(blocking=False):
                return True
        return False

    @staticmethod
    def _queue_article_revision(db, run_id, candidate, score, paper_dir, error) -> ArticleRevisionTask:
        task = db.query(ArticleRevisionTask).filter_by(arxiv_id=candidate.arxiv_id).first()
        payload = asdict(candidate)
        payload["published_at"] = candidate.published_at.isoformat()
        if task is None:
            task = ArticleRevisionTask(
                run_id=run_id, arxiv_id=candidate.arxiv_id,
                candidate_json=json.dumps(payload, ensure_ascii=False), score=score,
                artifact_dir=str(paper_dir.resolve()), status="PENDING", attempts=0,
            )
            db.add(task)
        elif task.status == "PASSED" or task.run_id != run_id:
            task.run_id = run_id
            task.candidate_json = json.dumps(payload, ensure_ascii=False)
            task.score = score
            task.artifact_dir = str(paper_dir.resolve())
            task.attempts = 0
        task.status = "PENDING"
        task.error_message = error[:3000]
        # The dedicated revision worker runs every 15 minutes. Keep the first
        # retry inside the same operating day while still serializing work.
        task.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=15)
        db.commit()
        return task

    @staticmethod
    def _read_failed_quality_report(paper_dir: Path) -> dict | None:
        report_path = paper_dir / "qa_report.json"
        if not report_path.exists():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("passed") is not False:
            return None
        # A provider quota/outage report is not a content-quality rejection. It
        # must never be backfilled into the persistent rework queue; the run is
        # marked FAILED and retried by the scheduler instead.
        issues = payload.get("issues")
        if isinstance(issues, list) and any(
            isinstance(issue, dict) and issue.get("code") == "llm_provider_unavailable"
            for issue in issues
        ):
            return None
        return payload

    def backfill_quality_revisions(self, db: Session) -> dict[str, int]:
        """Recover rejected checkpoints created before the revision queue existed.

        A quality failure must remain actionable even when it happened during an
        older deployment.  This migration is deliberately filesystem/DB only:
        it never opens a browser, calls arXiv, or calls the model.  It is safe to
        run at every startup because ``arxiv_id`` is unique in the revision
        table and existing pending/running/passed tasks are left untouched.
        """
        runs_root = Path(self._settings.artifacts_dir).resolve() / "runs"
        if not runs_root.exists():
            return {"scanned": 0, "queued": 0, "skipped": 0, "invalid": 0}

        counts = {"scanned": 0, "queued": 0, "skipped": 0, "invalid": 0}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for report_path in sorted(runs_root.glob("run_*/papers/*/qa_report.json")):
            counts["scanned"] += 1
            try:
                run_name = report_path.parents[2].name
                run_id = int(run_name.removeprefix("run_"))
                paper_dir = report_path.parent
                failed_report = self._read_failed_quality_report(paper_dir)
                if failed_report is None:
                    counts["skipped"] += 1
                    continue
                paper_ir_path = paper_dir / "paper_ir.json"
                if not paper_ir_path.exists():
                    counts["invalid"] += 1
                    continue
                paper_ir = json.loads(paper_ir_path.read_text(encoding="utf-8"))
                metadata = paper_ir.get("metadata") if isinstance(paper_ir, dict) else None
                if not isinstance(metadata, dict):
                    counts["invalid"] += 1
                    continue
                arxiv_id = str(metadata.get("arxiv_id") or "").strip()
                title = str(metadata.get("title") or "").strip()
                if not arxiv_id or not title:
                    counts["invalid"] += 1
                    continue

                existing = db.query(ArticleRevisionTask).filter_by(arxiv_id=arxiv_id).first()
                if existing is not None:
                    # A later successful revision (or an already-running retry)
                    # must never be replaced by an older checkpoint.
                    counts["skipped"] += 1
                    continue
                if db.query(Paper).filter(Paper.arxiv_id == arxiv_id).first() is not None:
                    # The same paper already has a durable article.  Queueing an
                    # old rejected artifact would supersede a known-good result.
                    counts["skipped"] += 1
                    continue

                run = crud.get_run(db, run_id)
                if run is None:
                    counts["invalid"] += 1
                    continue

                published_at = run.started_at or datetime.utcnow()
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                candidate = {
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "authors": [str(value) for value in metadata.get("authors", []) if str(value).strip()],
                    "abstract": str(metadata.get("abstract") or ""),
                    "categories": [str(value) for value in metadata.get("categories", []) if str(value).strip()],
                    "published_at": published_at.isoformat(),
                    "url": str(metadata.get("url") or ""),
                    "pdf_url": str(metadata.get("pdf_url") or ""),
                }
                issues = failed_report.get("issues", [])
                messages = [
                    str(issue.get("message") or "").strip()
                    for issue in issues
                    if isinstance(issue, dict) and str(issue.get("message") or "").strip()
                ]
                error = " | ".join(messages)[:3000] or "历史质量核验未通过，已回填自动返工队列。"
                task = ArticleRevisionTask(
                    run_id=run_id,
                    arxiv_id=arxiv_id,
                    candidate_json=json.dumps(candidate, ensure_ascii=False),
                    score=0.0,
                    artifact_dir=str(paper_dir.resolve()),
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=now,
                    error_message=error,
                )
                db.add(task)
                summary = json.loads(run.summary or "{}") if run.summary else {}
                revisions = summary.setdefault("quality_revisions", [])
                if not isinstance(revisions, list):
                    revisions = summary["quality_revisions"] = []
                revisions.append({
                    "arxiv_id": arxiv_id,
                    "status": "QUEUED_BACKFILL",
                    "error": error,
                    "artifact_dir": str(paper_dir.resolve()),
                })
                run.summary = json.dumps(summary, ensure_ascii=False)
                db.commit()
                counts["queued"] += 1
            except Exception:
                db.rollback()
                counts["invalid"] += 1
                logger.exception("Unable to backfill quality checkpoint %s", report_path)
        return counts

    def retry_quality_revisions(self, *, manual: bool = False, run_id: int | None = None) -> dict:
        if not self._acquire_workflow_lock(timeout_seconds=20.0):
            return {"skipped": True, "reason": "workflow_busy"}
        db = None
        self._running = True
        task = None
        try:
            db = SessionLocal()
            self._apply_runtime_settings(db)
            if not manual and self._daily_work_has_priority(db):
                return {"skipped": True, "reason": "daily_workflow_due"}
            if not manual and not self._publication_service.daily_publication_window_open():
                return {"skipped": True, "reason": "outside_daily_publication_window"}
            if not manual and self._settings.workflow_auto_publish_after_run and self._settings.zhihu_auto_publish:
                if self._publication_service.remaining_daily_publications(db) <= 0:
                    return {"skipped": True, "reason": "daily_publication_limit_reached"}
                if self._publication_service._list_automatic_publication_queue(db):
                    return {"skipped": True, "reason": "verified_article_already_queued"}
            self._rebuild_ai_services()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            query = db.query(ArticleRevisionTask).filter(
                ArticleRevisionTask.status.in_(["PENDING", "RUNNING"]),
            )
            if manual and run_id is not None:
                query = query.filter(ArticleRevisionTask.run_id == run_id)
            else:
                query = query.filter(ArticleRevisionTask.next_attempt_at <= now)
            task = query.order_by(ArticleRevisionTask.run_id.desc(), ArticleRevisionTask.next_attempt_at, ArticleRevisionTask.id).first()
            if task is None:
                return {"skipped": True, "reason": "no_due_quality_revisions"}
            # Persist a retry lease before model calls; interrupted work resumes
            # from the checkpoint after this time without resetting its history.
            task.status = "RUNNING"
            task.attempts += 1
            task.next_attempt_at = now + timedelta(hours=6)
            db.commit()
            self._start_progress(mode="quality_revision", message=f"Revising {task.arxiv_id} from saved findings.")
            self._set_run_id(task.run_id)
            payload = json.loads(task.candidate_json)
            payload["published_at"] = datetime.fromisoformat(payload["published_at"])
            candidate = CandidatePaper(**payload)
            directory = Path(task.artifact_dir)
            article = self._paper_pipeline.revise_from_checkpoint(
                candidate, directory / (candidate.arxiv_id.replace("/", "_") + ".pdf"), directory,
                progress_callback=lambda message: self._set_step("note", "active", f"{candidate.arxiv_id}: {message}"),
                force_redraft=task.attempts % 2 == 0,
            )
            if not article.quality_report.passed or article.quality_report.mode != "verified":
                raise RuntimeError("Revision did not produce a verified article")
            run = crud.get_run(db, task.run_id)
            if run is None:
                raise RuntimeError("Revision workflow run is missing")
            paper = db.query(Paper).filter_by(run_id=task.run_id, arxiv_id=task.arxiv_id).first()
            if paper is None:
                paper = Paper(run_id=task.run_id, arxiv_id=candidate.arxiv_id, title=candidate.title,
                    authors=", ".join(candidate.authors), abstract=candidate.abstract,
                    categories=", ".join(candidate.categories), published_at=candidate.published_at.replace(tzinfo=None),
                    url=candidate.url, pdf_url=candidate.pdf_url, score=task.score)
                db.add(paper)
                db.flush()
            existing = db.query(PublicationTask).filter_by(paper_id=paper.id).first()
            if existing and existing.status in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                task.status = "PASSED"
                db.commit()
                return {"skipped": True, "reason": "publication_already_started"}
            note = crud.get_paper_note(db, paper.id)
            if note is None:
                note = PaperNote(paper_id=paper.id)
                db.add(note)
            note.markdown_content = article.markdown
            note.image_paths = json.dumps(article.selected_image_urls, ensure_ascii=False)
            summary = json.loads(run.summary or "{}")
            qualities = summary.setdefault("article_quality", [])
            qualities[:] = [item for item in qualities if item.get("arxiv_id") != candidate.arxiv_id]
            qualities.append({"arxiv_id": candidate.arxiv_id, **article.quality_report.model_dump()})
            errors = summary.get("paper_errors", [])
            summary["paper_errors"] = [item for item in errors if item.get("arxiv_id") != candidate.arxiv_id]
            run.summary = json.dumps(summary, ensure_ascii=False)
            task.status = "PASSED"
            task.error_message = None
            db.commit()
            if existing:
                existing.note_markdown = article.markdown
                existing.title = article.document.title
                existing.status = "PENDING"
                existing.error_message = "质量返工已通过；等待自动发布。"
                new_tasks = [existing]
                db.commit()
            else:
                new_tasks = self._publication_service.create_tasks_for_run(db, task.run_id, [paper], {paper.id: article.markdown})
            if self._settings.workflow_auto_publish_after_run:
                # Queue only. The publication worker owns all platform requests.
                for publication in new_tasks:
                    publication.requested_by = "auto_workflow"
                    publication.command_text = "quality_revision_passed"
                db.commit()
            summary["selected_papers"] = db.query(Paper).filter_by(run_id=task.run_id).count()
            summary["publication_tasks"] = db.query(PublicationTask).filter_by(run_id=task.run_id).count()
            summary["last_quality_revision"] = {"arxiv_id": task.arxiv_id, "attempts": task.attempts, "passed": True}
            run.summary = json.dumps(summary, ensure_ascii=False)
            run.status = "PARTIAL"
            run.error_message = None
            db.commit()
            self._finish_progress(success=True, message=f"{task.arxiv_id}: revision verified and queued for publication.")
            return {"revised": task.arxiv_id, "passed": True, "codes": [p.paper_code for p in new_tasks]}
        except Exception as exc:
            if db is not None:
                db.rollback()
            if task is not None and db is not None:
                task.status = "PENDING"
                task.error_message = str(exc)[:3000]
                task.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=6)
                db.commit()
                self._finish_progress(success=False, message=f"Revision retained for retry: {exc}")
            logger.exception("Automatic quality revision deferred")
            return {"passed": False, "error": str(exc)}
        finally:
            if db is not None:
                db.close()
            self._running = False
            self._lock.release()

    def retry_automatic_publications(self) -> dict:
        if not self._acquire_workflow_lock(timeout_seconds=20.0):
            return {"skipped": True, "reason": "workflow_busy"}
        db = None
        try:
            db = SessionLocal()
            self._apply_runtime_settings(db)
            if self._daily_work_has_priority(db):
                return {"skipped": True, "reason": "daily_workflow_due"}
            if not (self._settings.workflow_auto_publish_after_run and self._settings.zhihu_auto_publish):
                return {"skipped": True, "reason": "automatic_publication_disabled"}
            if not self._publication_service.daily_publication_window_open():
                return {"skipped": True, "reason": "outside_daily_publication_window"}
            self._publication_service._zhihu_service._ensure_not_rate_limited()
            if self._publication_service.remaining_daily_publications(db) <= 0:
                return {"skipped": True, "reason": "daily_publication_limit_reached"}
            # PublicationTask.created_at is stored as a naive UTC timestamp.
            # Keep a month of verified, pre-publish failures in the queue. A
            # three-attempt cap used to make a transient Zhihu UI failure
            # disappear permanently after one bad night.
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
            tasks = db.query(PublicationTask).filter(
                PublicationTask.requested_by == "auto_workflow",
                PublicationTask.status.in_(["PENDING", "RATE_LIMITED", "FAILED"]),
                PublicationTask.created_at >= cutoff,
            ).order_by(PublicationTask.created_at).all()
            results = []
            for task in tasks:
                if task.status == "FAILED" and not PublicationService.is_retryable_automatic_failure(task.error_message):
                    continue
                run = crud.get_run(db, task.run_id)
                if run is None:
                    logger.warning("Skipping automatic publication retry for %s: workflow run %s is missing.", task.paper_code, task.run_id)
                    continue
                try:
                    summary = json.loads(run.summary or "{}")
                except (TypeError, ValueError):
                    logger.warning("Resetting malformed retry metadata for workflow run %s.", task.run_id)
                    summary = {}
                retries = summary.setdefault("publication_retries", {})
                attempts = int(retries.get(task.paper_code, {}).get("attempts", 0))
                if attempts >= 14:
                    continue
                last_attempt_at = str(retries.get(task.paper_code, {}).get("last_attempt_at", ""))
                if last_attempt_at:
                    try:
                        previous_attempt = datetime.fromisoformat(last_attempt_at.replace("Z", "+00:00"))
                        if previous_attempt.tzinfo is None:
                            previous_attempt = previous_attempt.replace(tzinfo=timezone.utc)
                        try:
                            local_tz = ZoneInfo(self._settings.timezone)
                        except Exception:
                            local_tz = timezone.utc
                        now_utc = datetime.now(timezone.utc)
                        if previous_attempt.astimezone(local_tz).date() == now_utc.astimezone(local_tz).date():
                            # A failed pre-publish attempt still counts as
                            # today's platform interaction. Do not hammer the
                            # account every five minutes after a UI/network
                            # failure; retry in the next daily window.
                            continue
                        # Back off within a day, then give the task one fresh
                        # attempt on each later daily window.
                        wait_seconds = min(24 * 60 * 60, 15 * 60 * (2 ** attempts))
                        if now_utc - previous_attempt.astimezone(timezone.utc) < timedelta(seconds=wait_seconds):
                            continue
                    except ValueError:
                        pass
                retries[task.paper_code] = {"attempts": attempts + 1, "last_attempt_at": self._now_iso()}
                run.summary = json.dumps(summary, ensure_ascii=False)
                db.commit()
                result = self._publication_service.publish_codes(db, [task.paper_code], sender="auto_workflow", command_text="auto_publish_retry")
                results.append(result)
                # One account interaction per retry worker invocation. This
                # avoids trying several drafts in the same daily window when
                # the first one fails before a confirmed publish response.
                break
            return {"retried": len(results), "results": results}
        except Exception as exc:
            logger.info("Automatic publication retry deferred: %s", exc)
            return {"skipped": True, "reason": str(exc)}
        finally:
            if db is not None:
                db.close()
            self._lock.release()

    @staticmethod
    def _quality_candidate_limit(candidate_count: int, target_count: int, max_candidates: int = 5) -> int:
        """Bound quality-gate replacements so a daily run cannot expand indefinitely."""
        return min(
            max(0, int(candidate_count)),
            max(max(1, int(target_count)), max(1, int(max_candidates))),
        )

    def poll_publish_commands(self) -> dict:
        if not self._acquire_workflow_lock(timeout_seconds=3.0):
            return {"ok": True, "skipped": True, "message": "Workflow is running; mailbox polling deferred."}
        if not self._command_lock.acquire(blocking=False):
            self._lock.release()
            return {"ok": False, "message": "Command polling is already running."}

        db: Session | None = None
        try:
            db = SessionLocal()
            self._apply_runtime_settings(db)
            if self._daily_work_has_priority(db):
                return {"ok": True, "skipped": True, "reason": "daily_workflow_due"}
            self._start_progress(mode="auto_publish", message="Polling mailbox for publish commands.")
            self._set_step("reply", "active", "Reading mailbox replies.")
            summary = self._publication_service.process_mail_commands(db, step_callback=self._on_publish_step)
            result = {
                "ok": True,
                "received_commands": summary.received_commands,
                "published_count": summary.published_count,
                "drafted_count": summary.drafted_count,
                "failed_count": summary.failed_count,
                "ignored_count": summary.ignored_count,
            }
            self._set_step("reply", "done", f"Processed {summary.received_commands} command email(s).")
            if summary.published_count > 0 or summary.failed_count > 0:
                self._set_step(
                    "zhihu",
                    "done" if summary.failed_count == 0 else "failed",
                    f"Publish finished: published={summary.published_count}, draft={summary.drafted_count}, failed={summary.failed_count}.",
                )
            self._finish_progress(success=summary.failed_count == 0, message="Mailbox polling completed.")
            return result
        except Exception as exc:
            logger.exception("Command polling failed: %s", exc)
            self._mark_current_step_failed(str(exc))
            self._finish_progress(success=False, message=f"Mailbox polling failed: {exc}")
            return {"ok": False, "message": str(exc)}
        finally:
            if db is not None:
                db.close()
            self._command_lock.release()
            self._lock.release()

    def publish_codes(self, codes: list[str], sender: str = "manual") -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "Workflow is running. Try again after completion.", "requested": codes, "results": []}
        if not self._command_lock.acquire(blocking=False):
            self._lock.release()
            return {"ok": False, "message": "Another publish job is running.", "requested": codes, "results": []}

        self._start_progress(mode="manual_publish", message="Manual publish started.")
        self._set_step("zhihu", "active", "Publishing selected codes to Zhihu.")

        db: Session = SessionLocal()
        try:
            self._apply_runtime_settings(db)
            result = self._publication_service.publish_codes(
                db,
                codes=codes,
                sender=sender,
                command_text="manual_publish",
                step_callback=self._on_publish_step,
            )
            failed = sum(1 for item in result.get("results", []) if item.get("status") == "FAILED")
            self._set_step(
                "zhihu",
                "done" if failed == 0 else "failed",
                f"Manual publish completed: total={len(result.get('results', []))}, failed={failed}.",
            )
            self._finish_progress(success=failed == 0, message="Manual publish completed.")
            return result
        except Exception as exc:
            self._mark_current_step_failed(str(exc))
            self._finish_progress(success=False, message=f"Manual publish failed: {exc}")
            raise
        finally:
            db.close()
            self._command_lock.release()
            self._lock.release()

    def reconcile_publication(self, code: str, *, published: bool, public_url: str | None = None) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("工作流或知乎操作正在运行，请等待结束后再核实发布状态。")
        db = SessionLocal()
        try:
            return self._publication_service.resolve_uncertain_publication(
                db,
                code,
                published=published,
                public_url=public_url,
            )
        finally:
            db.close()
            self._lock.release()

    def clear_zhihu_breaker(self) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("工作流或知乎操作正在运行，请等待结束后再解除熔断。")
        db = None
        try:
            db = SessionLocal()
            state = self._publication_service._zhihu_service.clear_rate_limit_after_manual_check()
            state["reset_retry_tasks"] = self._publication_service.reset_rate_limited_retry_budget(db)
            return state
        finally:
            if db is not None:
                db.close()
            self._lock.release()

    def list_publication_tasks(self, run_id: int | None = None, status: str | None = None) -> list[PublicationTask]:
        db: Session = SessionLocal()
        try:
            return crud.list_publication_tasks(db, run_id=run_id, status=status)
        finally:
            db.close()

    def delete_run(self, run_id: int) -> dict:
        if self._running:
            return {"ok": False, "message": "Workflow is running. Try again later."}
        db: Session = SessionLocal()
        try:
            deleted = crud.delete_run(db, run_id)
            if not deleted:
                return {"ok": False, "message": "Run not found."}

            run_artifacts = Path(self._settings.artifacts_dir) / "runs" / f"run_{run_id}"
            if run_artifacts.exists():
                shutil.rmtree(run_artifacts, ignore_errors=True)
            return {"ok": True, "message": f"Run #{run_id} deleted."}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            db.close()

    def get_graph(self) -> dict:
        return {
            "nodes": [
                {"id": "trigger", "label": f"{self._settings.schedule_hour:02d}:{self._settings.schedule_minute:02d} 定时或手动触发"},
                {"id": "fetch", "label": "检索 arXiv 近期量化论文"},
                {"id": "select", "label": f"模型与规则筛选 {self._settings.paper_count} 篇论文"},
                {"id": "note", "label": "原文证据、图表与全文核验"},
                {"id": "mail", "label": "发送论文编号通知"},
                {"id": "reply", "label": "自动发布或等待邮件指令" if self._settings.workflow_auto_publish_after_run else "等待邮件发布指令"},
                {"id": "zhihu", "label": f"发布到{self._settings.zhihu_column_name}" if self._settings.zhihu_auto_publish else "保存知乎草稿并验证"},
            ],
            "edges": [
                {"source": "trigger", "target": "fetch"},
                {"source": "fetch", "target": "select"},
                {"source": "select", "target": "note"},
                {"source": "note", "target": "mail"},
                {"source": "mail", "target": "reply"},
                {"source": "reply", "target": "zhihu"},
            ],
        }

    def test_llm_connection(self, overrides: dict[str, str] | None = None) -> dict:
        db: Session = SessionLocal()
        try:
            runtime = crud.list_settings(db)
        finally:
            db.close()

        test_settings = self._settings.model_copy(deep=True)
        self._apply_settings_map(test_settings, runtime)
        if overrides:
            sanitized_overrides = {
                key: str(value)
                for key, value in overrides.items()
                if key not in SECRET_SETTING_KEYS or str(value).strip()
            }
            self._apply_settings_map(test_settings, sanitized_overrides)

        api_key = (test_settings.llm_api_key or test_settings.openai_api_key or "").strip()
        if not api_key:
            return {
                "ok": False,
                "provider": test_settings.llm_provider,
                "model": test_settings.llm_model or test_settings.openai_model,
                "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
                "message": "llm_api_key is empty.",
            }

        if api_key.lower().startswith("http://") or api_key.lower().startswith("https://"):
            return {
                "ok": False,
                "provider": test_settings.llm_provider,
                "model": test_settings.llm_model or test_settings.openai_model,
                "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
                "message": "llm_api_key looks like a URL. Please provide the real API key token.",
            }

        llm = LLMService(test_settings)
        if not llm.enabled:
            return {
                "ok": False,
                "provider": test_settings.llm_provider,
                "model": test_settings.llm_model or test_settings.openai_model,
                "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
                "message": "LLM provider is disabled or client initialization failed.",
            }

        start = time.perf_counter()
        try:
            text = llm.generate(
                system_prompt="You are a connectivity test assistant. Reply in one line.",
                user_prompt="Return only: LLM connectivity OK",
            )
        except LLMProviderUnavailable as exc:
            return {
                "ok": False, "provider": test_settings.llm_provider,
                "model": test_settings.llm_model or test_settings.openai_model,
                "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
                "latency_ms": int((time.perf_counter() - start) * 1000), "message": str(exc),
            }
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if not text:
            return {
                "ok": False,
                "provider": test_settings.llm_provider,
                "model": test_settings.llm_model or test_settings.openai_model,
                "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
                "latency_ms": elapsed_ms,
                "message": "API call failed. Check base URL / API key / model.",
            }

        return {
            "ok": True,
            "provider": test_settings.llm_provider,
            "model": test_settings.llm_model or test_settings.openai_model,
            "base_url": LLMService._normalize_base_url(test_settings.llm_api_base_url),
            "latency_ms": elapsed_ms,
            "response_preview": text[:160],
            "message": "LLM API reachable.",
        }

    def _artifact_url(self, path: Path) -> str:
        artifacts_root = Path(self._settings.artifacts_dir)
        rel = path.relative_to(artifacts_root)
        return f"/artifacts/{rel.as_posix()}"

    def _apply_runtime_settings(self, db: Session) -> None:
        runtime = crud.list_settings(db)
        self._apply_settings_map(self._settings, runtime)

    def _rebuild_ai_services(self) -> None:
        self._llm_service = LLMService(self._settings)
        self._arxiv_service = ArxivService(self._settings, self._llm_service)
        self._paper_pipeline = PaperUnderstandingPipeline(self._llm_service, self._settings)

    def _apply_settings_map(self, target: Settings, values: dict[str, str]) -> None:
        for key, value in values.items():
            if not hasattr(target, key):
                continue
            try:
                if key in self.INT_FIELDS:
                    cast_value = int(value)
                elif key in self.FLOAT_FIELDS:
                    cast_value = float(value)
                elif key in self.BOOL_FIELDS:
                    cast_value = str(value).lower() in {"1", "true", "yes", "on"}
                else:
                    cast_value = value
                setattr(target, key, cast_value)
            except Exception:
                logger.warning("Skip invalid runtime value for %s=%s", key, value)

    def _new_progress_state(self) -> dict:
        return {
            "running": False,
            "mode": "idle",
            "run_id": None,
            "current_step": "idle",
            "message": "Idle",
            "updated_at": self._now_iso(),
            "node_status": {step_id: "pending" for step_id in self.STEP_IDS},
        }

    def _start_progress(self, mode: str, message: str) -> None:
        with self._progress_lock:
            self._progress = self._new_progress_state()
            self._progress["running"] = True
            self._progress["mode"] = mode
            self._progress["message"] = message
            self._progress["updated_at"] = self._now_iso()

    def _set_run_id(self, run_id: int) -> None:
        with self._progress_lock:
            self._progress["run_id"] = run_id
            self._progress["updated_at"] = self._now_iso()

    def _set_step(self, step_id: str, status: str, message: str | None = None) -> None:
        with self._progress_lock:
            if step_id in self._progress["node_status"]:
                self._progress["node_status"][step_id] = status
                if status == "active":
                    self._progress["current_step"] = step_id
            if message:
                self._progress["message"] = message
            self._progress["updated_at"] = self._now_iso()

    def _mark_current_step_failed(self, error_message: str) -> None:
        with self._progress_lock:
            current = self._progress.get("current_step")
            if current in self._progress["node_status"]:
                self._progress["node_status"][current] = "failed"
            self._progress["message"] = error_message
            self._progress["updated_at"] = self._now_iso()

    def _finish_progress(self, success: bool, message: str) -> None:
        with self._progress_lock:
            self._progress["running"] = False
            self._progress["message"] = message
            self._progress["current_step"] = "idle"
            self._progress["updated_at"] = self._now_iso()
            if success:
                for step_id, status in self._progress["node_status"].items():
                    if status == "active":
                        self._progress["node_status"][step_id] = "done"

    @staticmethod
    def _normalize_title(title: str) -> str:
        lowered = title.strip().lower()
        compact = re.sub(r"\s+", " ", lowered)
        normalized = re.sub(r"[^\w ]", "", compact, flags=re.UNICODE)
        return normalized.replace("_", "")

    def _filter_history_candidates(
        self,
        candidates: list[CandidatePaper],
        seen_ids: set[str],
        seen_titles: set[str],
    ) -> list[CandidatePaper]:
        filtered: list[CandidatePaper] = []
        for item in candidates:
            normalized_title = self._normalize_title(item.title)
            if item.arxiv_id in seen_ids or normalized_title in seen_titles:
                continue
            filtered.append(item)
        return filtered

    def _on_publish_step(self, step_id: str, status: str, message: str) -> None:
        self._set_step(step_id, status, message)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

