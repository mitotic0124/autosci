import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.models import ArticleRevisionTask, Base, PublicationTask, WorkflowRun
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMProviderUnavailable, LLMService
from app.services.paper_pipeline import PaperUnderstandingPipeline
from app.services.workflow_service import WorkflowService


def _candidate(arxiv_id: str = "2611.12389v1") -> CandidatePaper:
    return CandidatePaper(
        arxiv_id,
        "Diversification Across Predictors",
        ["A"],
        "Abstract",
        ["q-fin.ST"],
        datetime.now(timezone.utc),
        f"https://arxiv.org/abs/{arxiv_id}",
        f"https://arxiv.org/pdf/{arxiv_id}",
    )


class ProviderOutageRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.db = self.sessions()
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(_env_file=None, llm_provider="disabled", artifacts_dir=self.directory.name)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.directory.cleanup()

    def test_quota_429_stops_all_further_model_requests(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="test-only-key",
            llm_model="k3.1-pro",
            llm_api_base_url="https://api.kimi.com/coding/v1",
        )
        service = LLMService(settings)
        client = Mock()
        error = Exception(
            "Error code: 429 - You've reached your 5-hour usage limit. Your quota will reset."
        )
        error.status_code = 429
        client.chat.completions.create.side_effect = error
        service._client = client

        with self.assertRaises(LLMProviderUnavailable) as raised:
            service.generate("system", "user")
        message = str(raised.exception)
        self.assertIn("429", message)
        self.assertIn("额度", message)
        self.assertEqual(1, client.chat.completions.create.call_count)

        # A terminal provider error must not be retried inside this run.
        with self.assertRaises(LLMProviderUnavailable):
            service.generate("system", "user")
        self.assertEqual(1, client.chat.completions.create.call_count)

    def test_plain_rate_limit_429_is_also_terminal(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="test-only-key",
            llm_model="k3.1-pro",
            llm_api_base_url="https://api.kimi.com/coding/v1",
        )
        service = LLMService(settings)
        client = Mock()
        error = Exception("Error code: 429 - rate limit reached")
        error.status_code = 429
        client.chat.completions.create.side_effect = error
        service._client = client
        with self.assertRaisesRegex(LLMProviderUnavailable, "限流"):
            service.generate("system", "user")

    def test_repeated_transport_failures_are_terminal(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="test-only-key",
            llm_model="k3.1-pro",
            llm_api_base_url="https://api.kimi.com/coding/v1",
        )
        service = LLMService(settings)
        client = Mock()
        client.chat.completions.create.side_effect = ConnectionError("Connection reset by peer")
        service._client = client
        # Two transient failures may legitimately let a caller fall back...
        self.assertIsNone(service.generate("system", "user"))
        self.assertIsNone(service.generate("system", "user"))
        # ...but a persistent outage must stop the run instead of turning into
        # an "incomplete verification" quality rejection.
        with self.assertRaisesRegex(LLMProviderUnavailable, "连续请求失败"):
            service.generate("system", "user")

    def test_backfill_ignores_provider_outage_reports(self) -> None:
        from app.db import crud
        from app.db.models import WorkflowRun

        run = WorkflowRun(trigger_source="scheduler", status="FAILED")
        self.db.add(run)
        self.db.commit()
        paper_dir = Path(self.directory.name) / "runs" / f"run_{run.id}" / "papers" / "2611.00001v1"
        paper_dir.mkdir(parents=True)
        (paper_dir / "paper_ir.json").write_text(json.dumps({
            "metadata": {
                "arxiv_id": "2611.00001v1",
                "title": "Provider outage paper",
                "authors": ["A"],
                "abstract": "A",
                "categories": ["q-fin.ST"],
                "url": "u",
                "pdf_url": "p",
            }
        }), encoding="utf-8")
        (paper_dir / "qa_report.json").write_text(json.dumps({
            "passed": False,
            "issues": [{"code": "llm_provider_unavailable", "message": "HTTP 429 quota"}],
        }), encoding="utf-8")
        service = WorkflowService(self.settings)
        counts = service.backfill_quality_revisions(self.db)
        self.assertEqual(0, counts["queued"])
        self.assertEqual(1, counts["skipped"])
        self.assertEqual(0, self.db.query(ArticleRevisionTask).count())

    def test_workflow_aborts_on_provider_outage_without_fake_revision(self) -> None:
        service = WorkflowService(self.settings)
        candidate = _candidate()
        service._rebuild_ai_services = Mock()
        service._arxiv_service = Mock()
        service._arxiv_service.fetch_recent_candidates.return_value = [candidate]
        service._arxiv_service.rank_and_pick.return_value = [(candidate, 1.0)]
        service._paper_pipeline = Mock()
        service._paper_pipeline.run.side_effect = LLMProviderUnavailable(
            "模型服务用量额度已用尽，需等待额度窗口重置或补充额度后重试 (HTTP 429)。本次运行已停止模型请求。"
        )
        service._email_service = Mock()
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            service.run_pipeline("scheduler")
        self.db.expire_all()
        run = self.db.query(WorkflowRun).one()
        self.assertEqual("FAILED", run.status)
        self.assertIn("429", run.error_message or "")
        self.assertEqual(0, self.db.query(ArticleRevisionTask).count())
        self.assertEqual(0, self.db.query(PublicationTask).count())

    def test_maintenance_lock_waits_out_a_brief_scheduler_probe(self) -> None:
        service = WorkflowService(self.settings)
        service._lock.acquire()

        def release() -> None:
            time.sleep(0.4)
            service._lock.release()

        thread = threading.Thread(target=release)
        thread.start()
        started = time.monotonic()
        try:
            self.assertTrue(service._acquire_workflow_lock(timeout_seconds=5.0))
            self.assertGreaterEqual(time.monotonic() - started, 0.2)
            self.assertTrue(service._lock.locked())
        finally:
            if service._lock.locked():
                service._lock.release()
            thread.join(timeout=5)

    def test_maintenance_lock_times_out_instead_of_blocking_forever(self) -> None:
        service = WorkflowService(self.settings)
        service._lock.acquire()
        started = time.monotonic()
        try:
            self.assertFalse(service._acquire_workflow_lock(timeout_seconds=0.3))
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            service._lock.release()

    def test_manual_revision_bypasses_daily_publication_capacity(self) -> None:
        self.settings.workflow_auto_publish_after_run = True
        self.settings.zhihu_auto_publish = True
        run_row = WorkflowRun(trigger_source="scheduler", status="PARTIAL", summary="{}")
        self.db.add(run_row)
        self.db.commit()
        task = ArticleRevisionTask(
            run_id=run_row.id,
            arxiv_id="2611.12389v1",
            candidate_json=json.dumps({
                "arxiv_id": "2611.12389v1", "title": "Title", "authors": ["A"],
                "abstract": "Abstract", "categories": ["q-fin.ST"],
                "published_at": datetime.now(timezone.utc).isoformat(),
                "url": "u", "pdf_url": "p",
            }),
            artifact_dir=self.directory.name,
            status="PENDING",
            next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.db.add(task)
        self.db.commit()
        service = WorkflowService(self.settings)
        service._apply_runtime_settings = Mock()
        service._rebuild_ai_services = Mock()
        service._publication_service = Mock()
        service._publication_service.daily_publication_window_open.return_value = False
        service._publication_service.remaining_daily_publications.return_value = 0
        service._publication_service._list_automatic_publication_queue.return_value = [Mock()]
        service._paper_pipeline = Mock()
        service._paper_pipeline.revise_from_checkpoint.side_effect = RuntimeError("stop after selection")
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            result = service.retry_quality_revisions(manual=True, run_id=run_row.id)
        self.assertFalse(result["passed"])
        self.assertIn("stop after selection", result["error"])
        service._paper_pipeline.revise_from_checkpoint.assert_called_once()
        self.db.expire_all()
        self.assertEqual(1, self.db.query(ArticleRevisionTask).one().attempts)

    def test_pipeline_outage_does_not_write_false_quality_rejection(self) -> None:
        settings = Settings(_env_file=None, llm_provider="disabled", article_fetch_arxiv_source=False)
        pipeline = PaperUnderstandingPipeline(Mock(enabled=True), settings)
        with tempfile.TemporaryDirectory() as directory:
            paper_dir = Path(directory)
            pdf = paper_dir / "paper.pdf"
            pdf.touch()
            pipeline._parser.parse = Mock(side_effect=LLMProviderUnavailable("quota (HTTP 429)"))
            with self.assertRaises(LLMProviderUnavailable):
                pipeline.run(_candidate(), pdf, paper_dir)
            self.assertTrue((paper_dir / "provider_error.json").exists())
            self.assertFalse((paper_dir / "qa_report.json").exists())


if __name__ == "__main__":
    unittest.main()
