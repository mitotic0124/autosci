import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import crud
from app.db.models import Paper, PaperNote, PublicationTask
from app.db.session import SessionLocal
from app.services.arxiv_service import ArxivService
from app.services.email_service import EmailService
from app.services.image_service import PaperImageService
from app.services.llm_service import LLMService
from app.services.note_service import NoteService
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
        "imap_port",
        "command_poll_interval_seconds",
    }
    BOOL_FIELDS = {"smtp_use_tls", "imap_use_ssl", "zhihu_headless", "zhihu_publish_enabled"}

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm_service = LLMService(settings)
        self._arxiv_service = ArxivService(settings, self._llm_service)
        self._image_service = PaperImageService()
        self._note_service = NoteService(self._llm_service, settings)
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

    def run_pipeline(self, trigger_source: str = "manual") -> None:
        if not self._lock.acquire(blocking=False):
            logger.info("Workflow run ignored because another run is executing.")
            return

        self._running = True
        self._start_progress(mode="workflow", message="Workflow started.")
        self._set_step("trigger", "active", "Initializing run context.")

        db: Session = SessionLocal()
        run_id: int | None = None
        email_error: str | None = None
        try:
            self._apply_runtime_settings(db)
            self._rebuild_ai_services()
            run = crud.create_run(db, trigger_source=trigger_source)
            run_id = run.id
            self._set_run_id(run_id)
            self._set_step("trigger", "done", f"Run #{run_id} created.")

            run_dir = Path(self._settings.artifacts_dir) / "runs" / f"run_{run_id}"
            run_dir.mkdir(parents=True, exist_ok=True)

            self._set_step("fetch", "active", "Fetching recent arXiv candidates.")
            candidates = self._arxiv_service.fetch_recent_candidates(hours_back=48)
            if len(candidates) < self._settings.paper_count:
                fallback_candidates = self._arxiv_service.fetch_recent_candidates(hours_back=24 * 30)
                merged_by_id = {item.arxiv_id: item for item in candidates}
                for item in fallback_candidates:
                    merged_by_id[item.arxiv_id] = item
                candidates = list(merged_by_id.values())
            self._set_step("fetch", "done", f"Fetched {len(candidates)} candidates.")

            self._set_step("select", "active", "Ranking papers and selecting top candidates.")
            selected = self._arxiv_service.rank_and_pick(candidates, top_k=self._settings.paper_count)
            if not selected:
                raise RuntimeError("No arXiv papers were selected for this run.")
            self._set_step("select", "done", f"Selected {len(selected)} papers.")

            self._set_step("note", "active", "Generating paper notes with figures.")
            papers: list[Paper] = []
            notes_map: dict[int, str] = {}
            total_selected = len(selected)
            for idx, (candidate, score) in enumerate(selected, start=1):
                self._set_step("note", "active", f"Generating note {idx}/{total_selected}: {candidate.arxiv_id}")
                paper_base = run_dir / "papers" / candidate.arxiv_id.replace("/", "_")
                paper_base.mkdir(parents=True, exist_ok=True)
                pdf_path = paper_base / f"{candidate.arxiv_id.replace('/', '_')}.pdf"

                image_urls: list[str] = []
                try:
                    self._image_service.download_pdf(candidate.pdf_url, pdf_path)
                    images = self._image_service.extract_relevant_images(pdf_path, paper_base / "images", max_images=2)
                    image_urls = [self._artifact_url(path) for path in images]
                except Exception as exc:
                    logger.warning("Image extraction failed for %s: %s", candidate.arxiv_id, exc)

                note_markdown = self._note_service.build_note(candidate, image_urls, pdf_path=pdf_path)
                (paper_base / "note.md").write_text(note_markdown, encoding="utf-8")

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
            self._set_step("note", "done", f"Generated {len(papers)} paper notes.")

            publication_tasks = self._publication_service.create_tasks_for_run(db, run_id, papers, notes_map)

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
                "selected_papers": len(papers),
                "publication_tasks": len(publication_tasks),
                "email_sent": email_sent,
                "email_error": email_error,
            }
            crud.finish_run(db, run_id, status="SUCCESS", summary=summary)
            self._finish_progress(success=True, message=f"Run #{run_id} finished successfully.")
        except Exception as exc:
            logger.exception("Workflow run failed: %s", exc)
            self._mark_current_step_failed(str(exc))
            if run_id is not None:
                crud.finish_run(db, run_id, status="FAILED", error_message=str(exc), summary={"email_error": email_error})
            self._finish_progress(success=False, message=f"Run failed: {exc}")
        finally:
            db.close()
            self._running = False
            self._lock.release()

    def poll_publish_commands(self) -> dict:
        if not self._command_lock.acquire(blocking=False):
            return {"ok": False, "message": "Command polling is already running."}

        self._start_progress(mode="auto_publish", message="Polling mailbox for publish commands.")
        self._set_step("reply", "active", "Reading mailbox replies.")

        db: Session = SessionLocal()
        try:
            self._apply_runtime_settings(db)
            summary = self._publication_service.process_mail_commands(db, step_callback=self._on_publish_step)
            result = {
                "ok": True,
                "received_commands": summary.received_commands,
                "published_count": summary.published_count,
                "failed_count": summary.failed_count,
                "ignored_count": summary.ignored_count,
            }
            self._set_step("reply", "done", f"Processed {summary.received_commands} command email(s).")
            if summary.published_count > 0 or summary.failed_count > 0:
                self._set_step(
                    "zhihu",
                    "done" if summary.failed_count == 0 else "failed",
                    f"Publish finished: success={summary.published_count}, failed={summary.failed_count}.",
                )
            self._finish_progress(success=summary.failed_count == 0, message="Mailbox polling completed.")
            return result
        except Exception as exc:
            logger.exception("Command polling failed: %s", exc)
            self._mark_current_step_failed(str(exc))
            self._finish_progress(success=False, message=f"Mailbox polling failed: {exc}")
            return {"ok": False, "message": str(exc)}
        finally:
            db.close()
            self._command_lock.release()

    def publish_codes(self, codes: list[str], sender: str = "manual") -> dict:
        if not self._command_lock.acquire(blocking=False):
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
                {"id": "trigger", "label": "06:00 Scheduler / Manual Trigger"},
                {"id": "fetch", "label": "arXiv Fetch (Recent Quant Papers)"},
                {"id": "select", "label": f"LLM + Rule Ranking (Top {self._settings.paper_count})"},
                {"id": "note", "label": "Read Note + Figure Extraction"},
                {"id": "mail", "label": "Send Paper Codes Email"},
                {"id": "reply", "label": "Listen Reply: 发布R001-01,R001-02"},
                {"id": "zhihu", "label": "Publish to Zhihu Column"},
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
            self._apply_settings_map(test_settings, {k: str(v) for k, v in overrides.items()})

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
        text = llm.generate(
            system_prompt="You are a connectivity test assistant. Reply in one line.",
            user_prompt="Return only: LLM connectivity OK",
        )
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
        self._note_service = NoteService(self._llm_service, self._settings)

    def _apply_settings_map(self, target: Settings, values: dict[str, str]) -> None:
        for key, value in values.items():
            if not hasattr(target, key):
                continue
            try:
                if key in self.INT_FIELDS:
                    cast_value = int(value)
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

    def _on_publish_step(self, step_id: str, status: str, message: str) -> None:
        self._set_step(step_id, status, message)

    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
