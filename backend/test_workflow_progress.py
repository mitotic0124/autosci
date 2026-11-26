import threading
import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.scheduler import SchedulerService
from app.db import crud
from app.db.models import ArticleRevisionTask, Base
from app.services.arxiv_service import CandidatePaper
from app.services.workflow_service import WorkflowService


class WorkflowProgressTests(unittest.TestCase):
    def test_failed_quality_checkpoint_is_detected_for_revision_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "qa_report.json"
            report_path.write_text(json.dumps({"passed": False, "metrics": {"score": 0.4}}), encoding="utf-8")
            report = WorkflowService._read_failed_quality_report(Path(directory))
            self.assertEqual({"score": 0.4}, report["metrics"])

            report_path.write_text(json.dumps({"passed": True}), encoding="utf-8")
            self.assertIsNone(WorkflowService._read_failed_quality_report(Path(directory)))

    def test_quality_candidate_reserve_is_bounded(self) -> None:
        self.assertEqual(0, WorkflowService._quality_candidate_limit(0, 3, 5))
        self.assertEqual(3, WorkflowService._quality_candidate_limit(3, 3, 5))
        self.assertEqual(5, WorkflowService._quality_candidate_limit(15, 3, 5))
        self.assertEqual(3, WorkflowService._quality_candidate_limit(15, 3, 3))

    def test_quality_revision_checkpoint_is_reused_and_reassigned(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        candidate = CandidatePaper(
            arxiv_id="2601.00001v1", title="Title", authors=["Author"], abstract="Abstract",
            categories=["q-fin"], published_at=datetime.now(timezone.utc),
            url="https://arxiv.org/abs/2601.00001v1", pdf_url="https://arxiv.org/pdf/2601.00001v1.pdf",
        )
        try:
            first = WorkflowService._queue_article_revision(session, 1, candidate, 0.8, Path("artifacts/run-1"), "bad")
            self.assertEqual("PENDING", first.status)
            first.status = "PASSED"
            session.commit()
            second = WorkflowService._queue_article_revision(session, 2, candidate, 0.9, Path("artifacts/run-2"), "bad again")
            self.assertIs(first, second)
            self.assertEqual(2, second.run_id)
            self.assertEqual(0.9, second.score)
            self.assertEqual(0, second.attempts)
        finally:
            session.close()
            engine.dispose()

    def test_backfill_rejected_checkpoint_is_idempotent_and_does_not_touch_zhihu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs" / "run_7" / "papers" / "2601.00002v1"
            root.mkdir(parents=True)
            (root / "paper_ir.json").write_text(json.dumps({
                "metadata": {
                    "arxiv_id": "2601.00002v1", "title": "Rejected paper",
                    "authors": ["A"], "abstract": "Abstract", "categories": ["q-fin"],
                    "url": "https://arxiv.org/abs/2601.00002v1",
                    "pdf_url": "https://arxiv.org/pdf/2601.00002v1.pdf",
                }
            }), encoding="utf-8")
            (root / "qa_report.json").write_text(json.dumps({
                "passed": False, "issues": [{"code": "unsupported_paragraph", "message": "fix this"}]
            }), encoding="utf-8")
            settings = Settings(llm_provider="disabled", artifacts_dir=directory)
            service = WorkflowService(settings)
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            session = sessionmaker(bind=engine)()
            run = crud.create_run(session, "scheduler")
            # The artifact directory uses the actual run id.
            root.parent.parent.rename(Path(directory) / "runs" / f"run_{run.id}")
            try:
                first = service.backfill_quality_revisions(session)
                second = service.backfill_quality_revisions(session)
                self.assertEqual(1, first["queued"])
                self.assertEqual(0, second["queued"])
                task = session.query(ArticleRevisionTask).one()
                self.assertEqual("PENDING", task.status)
                self.assertEqual("2601.00002v1", task.arxiv_id)
            finally:
                session.close()
                engine.dispose()

    def test_scheduler_registers_daily_work_and_bounded_retry_job(self) -> None:
        scheduler = SchedulerService(Settings(llm_provider="disabled", schedule_hour=7, schedule_minute=0), Mock())
        try:
            scheduler.start()
            status = scheduler.status()
            self.assertTrue(status["running"])
            self.assertEqual({"daily_quant_pipeline", "daily_workflow_catchup", "poll_publish_commands", "retry_automatic_publications", "retry_quality_revisions"}, {job["id"] for job in status["jobs"]})
            job = scheduler._scheduler.get_job("daily_quant_pipeline")
            self.assertEqual(7, job.next_run_time.hour)
            self.assertEqual(1, job.max_instances)
            revisions = scheduler._scheduler.get_job("retry_quality_revisions")
            publications = scheduler._scheduler.get_job("retry_automatic_publications")
            self.assertGreater(abs((revisions.next_run_time - publications.next_run_time).total_seconds()), 60)
        finally:
            scheduler.shutdown()

    def test_automatic_retry_persists_attempt_limit_and_skips_content_failures(self) -> None:
        service = WorkflowService(Settings(llm_provider="disabled", zhihu_auto_publish=True, workflow_auto_publish_after_run=True))
        service._apply_runtime_settings = Mock()
        service._daily_work_has_priority = Mock(return_value=False)
        service._publication_service = Mock()
        service._publication_service.daily_publication_window_open.return_value = True
        service._publication_service.remaining_daily_publications.return_value = 1
        service._publication_service._zhihu_service._ensure_interaction_cooldown.return_value = None
        service._publication_service.publish_codes.return_value = {"stats": {"PUBLISHED": 1}}
        transient = SimpleNamespace(paper_code="R001-01", run_id=1, status="FAILED", error_message="知乎发布超时: draft save timeout before publish request")
        rejected = SimpleNamespace(paper_code="R001-02", run_id=1, status="FAILED", error_message="Unverified article")
        run = SimpleNamespace(summary="{}")
        db = Mock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [transient, rejected]
        with patch("app.services.workflow_service.SessionLocal", return_value=db), patch("app.services.workflow_service.crud.get_run", return_value=run):
            for _ in range(4):
                service.retry_automatic_publications()
        self.assertEqual(1, service._publication_service.publish_codes.call_count)
        self.assertEqual(1, json.loads(run.summary)["publication_retries"]["R001-01"]["attempts"])
        self.assertNotIn("R001-02", json.loads(run.summary)["publication_retries"])

    def test_automatic_retry_stops_when_daily_quota_is_spent(self) -> None:
        service = WorkflowService(Settings(llm_provider="disabled", zhihu_auto_publish=True, workflow_auto_publish_after_run=True))
        service._apply_runtime_settings = Mock()
        service._daily_work_has_priority = Mock(return_value=False)
        service._publication_service = Mock()
        service._publication_service.daily_publication_window_open.return_value = True
        service._publication_service.remaining_daily_publications.return_value = 0
        db = Mock()
        with patch("app.services.workflow_service.SessionLocal", return_value=db):
            result = service.retry_automatic_publications()
        self.assertEqual("daily_publication_limit_reached", result["reason"])
        service._publication_service.publish_codes.assert_not_called()

    def test_automatic_retry_respects_platform_cooldown(self) -> None:
        service = WorkflowService(Settings(llm_provider="disabled", zhihu_auto_publish=True, workflow_auto_publish_after_run=True))
        service._apply_runtime_settings = Mock()
        service._daily_work_has_priority = Mock(return_value=False)
        service._publication_service = Mock()
        service._publication_service._zhihu_service._ensure_not_rate_limited.side_effect = RuntimeError("cooldown")
        with patch("app.services.workflow_service.SessionLocal", return_value=Mock()):
            self.assertTrue(service.retry_automatic_publications()["skipped"])
        service._publication_service.publish_codes.assert_not_called()
        self.assertFalse(service._lock.locked())

    def test_mail_poll_cannot_overwrite_an_active_workflow(self) -> None:
        service = WorkflowService.__new__(WorkflowService)
        service._lock = threading.Lock()
        service._lock.acquire()
        result = service.poll_publish_commands()
        self.assertTrue(result["skipped"])
        self.assertTrue(service._lock.locked())

    def test_manual_publish_waits_for_active_workflow(self) -> None:
        service = WorkflowService.__new__(WorkflowService)
        service._lock = threading.Lock()
        service._lock.acquire()
        result = service.publish_codes(["R001-01"])
        self.assertFalse(result["ok"])
        self.assertEqual([], result["results"])


if __name__ == "__main__":
    unittest.main()
