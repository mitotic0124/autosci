import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db import crud
from app.db.models import ArticleRevisionTask, Base, Paper, PublicationTask, WorkflowRun
from app.services.arxiv_service import CandidatePaper
from app.services.publication_service import PublicationService
from app.services.workflow_service import WorkflowService
from app.services.llm_service import LLMProviderUnavailable
from app.api.routes import health_check, trigger_scheduled_workflow


class DailyAutomationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.db = self.sessions()
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(_env_file=None, llm_provider="disabled", artifacts_dir=self.directory.name)
        self.now = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)  # 08:00 Beijing

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.directory.cleanup()

    def state(self, now=None):
        return crud.scheduled_run_state(self.db, self.settings, now or self.now)

    def add_run(self, status, started_at=None, trigger_source="scheduler"):
        started_at = started_at or self.now.replace(tzinfo=None) - timedelta(hours=1)
        run = WorkflowRun(trigger_source=trigger_source, status=status, started_at=started_at,
                          ended_at=started_at if status != "RUNNING" else None)
        self.db.add(run)
        self.db.commit()
        return run

    def test_beijing_schedule_does_not_use_server_timezone(self):
        self.assertFalse(self.state(datetime(2026, 11, 9, 22, 59, tzinfo=timezone.utc))["due"])
        self.assertTrue(self.state(datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc))["due"])
        self.assertEqual("2026-11-10", self.state()["date"])

    def test_restart_after_missed_fire_is_due(self):
        self.assertTrue(self.state()["due"])
        self.assertTrue(self.state(self.now + timedelta(hours=15))["due"])

    def test_completed_partial_and_running_runs_are_not_repeated(self):
        for status in ("SUCCESS", "PARTIAL", "RUNNING"):
            with self.subTest(status=status):
                run = self.add_run(status)
                self.assertFalse(self.state()["due"])
                self.db.delete(run)
                self.db.commit()

    def test_manual_run_and_previous_day_do_not_consume_today(self):
        self.add_run("SUCCESS", trigger_source="manual")
        self.add_run("SUCCESS", started_at=self.now.replace(tzinfo=None) - timedelta(days=1))
        self.assertEqual(0, self.state()["attempts"])
        self.assertTrue(self.state()["due"])

    def test_failed_run_retries_with_backoff_and_daily_limit(self):
        run = self.add_run("FAILED", started_at=self.now.replace(tzinfo=None) - timedelta(minutes=10))
        self.assertEqual("retry_backoff", self.state()["reason"])
        self.assertTrue(self.state(self.now + timedelta(minutes=21))["due"])
        run.ended_at = run.started_at - timedelta(hours=1)
        self.db.commit()
        self.add_run("FAILED")
        self.add_run("INTERRUPTED")
        self.assertEqual("daily_retry_limit_reached", self.state()["reason"])

    def test_startup_recovers_generation_but_never_retries_uncertain_post(self):
        run = self.add_run("RUNNING")
        self.db.add(ArticleRevisionTask(run_id=run.id, arxiv_id="test", candidate_json="{}",
                                       artifact_dir="unused", status="RUNNING"))
        self.db.add(PublicationTask(run_id=run.id, paper_id=1, paper_code="R001-01", title="t",
                                    note_markdown="t", status="PUBLISHING"))
        self.db.commit()
        recovered = crud.recover_interrupted_work(self.db)
        self.assertEqual({"runs": 1, "revisions": 1, "uncertain_publications": 1}, recovered)
        self.assertEqual("INTERRUPTED", run.status)
        self.assertEqual("PENDING", self.db.query(ArticleRevisionTask).one().status)
        self.assertEqual("PUBLISH_UNCERTAIN", self.db.query(PublicationTask).one().status)
        self.assertEqual({"runs": 0, "revisions": 0, "uncertain_publications": 0}, crud.recover_interrupted_work(self.db))

    def test_busy_seven_am_run_is_still_due_when_lock_released(self):
        service = WorkflowService(self.settings)
        service._lock.acquire()
        result = service.run_pipeline("scheduler")
        self.assertEqual("workflow_busy", result["reason"])
        self.assertTrue(self.state()["due"])
        service._lock.release()

    def test_maintenance_yields_to_daily_work_before_external_requests(self):
        service = WorkflowService(self.settings)
        service._daily_work_has_priority = Mock(return_value=True)
        service._publication_service = Mock()
        service._paper_pipeline = Mock()
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            self.assertEqual("daily_workflow_due", service.retry_quality_revisions()["reason"])
            self.assertEqual("daily_workflow_due", service.retry_automatic_publications()["reason"])
            self.assertEqual("daily_workflow_due", service.poll_publish_commands()["reason"])
        service._publication_service.publish_codes.assert_not_called()
        service._publication_service.process_mail_commands.assert_not_called()
        service._paper_pipeline.revise_from_checkpoint.assert_not_called()
        self.assertFalse(service._lock.locked())

    def test_scheduled_flow_persists_once_and_reaches_publication_without_agent(self):
        self.settings.workflow_auto_publish_after_run = True
        self.settings.zhihu_auto_publish = True
        self.settings.paper_count = 1
        service = WorkflowService(self.settings)
        candidate = CandidatePaper("2611.00001v1", "Title", ["A"], "Abstract", ["q-fin.TR"],
                                   datetime.now(timezone.utc), "https://arxiv.org/abs/test", "https://arxiv.org/pdf/test")
        service._rebuild_ai_services = Mock()
        service._arxiv_service = Mock()
        service._arxiv_service.fetch_recent_candidates.return_value = [candidate]
        service._arxiv_service.rank_and_pick.return_value = [(candidate, 1.0)]
        service._paper_pipeline = Mock()
        service._paper_pipeline.run.return_value = SimpleNamespace(
            markdown="# Title\n\nVerified body", selected_image_urls=[],
            quality_report=SimpleNamespace(passed=True, score=1.0, mode="verified", metrics={
                "article_context_verified": True, "article_global_verified": True,
            }),
        )
        service._email_service = Mock()
        service._email_service.send_paper_code_digest.return_value = True
        publisher = service._publication_service
        publisher.remaining_daily_publications = Mock(return_value=1)
        publisher._zhihu_service.publish_article = Mock(return_value="https://zhuanlan.zhihu.com/p/123")
        original_state = crud.scheduled_run_state
        local_evening = datetime.now(ZoneInfo(self.settings.timezone)).replace(hour=23, minute=1)
        with patch("app.services.workflow_service.SessionLocal", self.sessions), \
             patch("app.services.workflow_service.crud.scheduled_run_state", side_effect=lambda db, settings: original_state(db, settings, local_evening)):
            # Pin the date without freezing the independent article timestamps.
            service.run_pipeline("scheduler")
            service.run_pipeline("scheduler")
        self.db.expire_all()
        self.assertEqual(1, self.db.query(WorkflowRun).count())
        self.assertEqual("SUCCESS", self.db.query(WorkflowRun).one().status)
        self.assertEqual("PUBLISHED", self.db.query(PublicationTask).one().status)
        publisher._zhihu_service.publish_article.assert_called_once()

    def test_revision_publication_codes_are_unique_within_same_run(self):
        run = self.add_run("PARTIAL")
        service = PublicationService(self.settings)
        codes = []
        for idx in range(3):
            paper = Paper(run_id=run.id, arxiv_id=str(idx), title="Title", authors="A", abstract="A", url="u", pdf_url="p")
            self.db.add(paper)
            self.db.commit()
            tasks = service.create_tasks_for_run(self.db, run.id, [paper], {paper.id: "# Title\n\nBody"})
            codes.extend(task.paper_code for task in tasks)
        self.assertEqual([f"R{run.id:03d}-01", f"R{run.id:03d}-02", f"R{run.id:03d}-03"], codes)

    def test_empty_or_invalid_publish_url_is_never_success(self):
        self.settings.zhihu_auto_publish = True
        service = PublicationService(self.settings)
        for url in (None, "", "https://www.zhihu.com/", "https://zhuanlan.zhihu.com/write"):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, "结果不确定"):
                service._status_from_result_url(url)
        self.assertEqual("PUBLISHED", service._status_from_result_url("https://zhuanlan.zhihu.com/p/123"))
        self.assertEqual("DRAFT", service._status_from_result_url("https://zhuanlan.zhihu.com/p/123/edit"))

    def test_final_scheduled_failure_is_persisted_and_alerted_once(self):
        local_now = datetime.now(ZoneInfo(self.settings.timezone))
        local_evening = local_now.replace(hour=23, minute=1)
        earlier = local_now.replace(hour=0, minute=0).astimezone(timezone.utc).replace(tzinfo=None)
        self.add_run("FAILED", started_at=earlier)
        self.add_run("FAILED", started_at=earlier)
        service = WorkflowService(self.settings)
        # Even initialization failures must consume a persisted attempt.
        service._rebuild_ai_services = Mock(side_effect=RuntimeError("provider setup failed"))
        service._email_service = Mock()
        service._email_service.send_workflow_alert.return_value = True
        original_state = crud.scheduled_run_state
        with patch("app.services.workflow_service.SessionLocal", self.sessions), \
             patch("app.services.workflow_service.crud.scheduled_run_state", side_effect=lambda db, settings: original_state(db, settings, local_evening)):
            service.run_pipeline("scheduler")
            result = service.run_pipeline("scheduler")
        self.assertEqual("daily_retry_limit_reached", result["reason"])
        self.db.expire_all()
        self.assertEqual(3, self.db.query(WorkflowRun).count())
        last = self.db.query(WorkflowRun).order_by(WorkflowRun.id.desc()).first()
        self.assertEqual("FAILED", last.status)
        self.assertIn("provider setup failed", last.error_message)
        self.assertTrue(json.loads(last.summary)["automation_alert_sent"])
        service._email_service.send_workflow_alert.assert_called_once()
        self.assertFalse(service._lock.locked())

    def test_health_exposes_disabled_publication_and_today_state(self):
        service = WorkflowService(self.settings)
        service.scheduled_status = Mock(return_value=self.state())
        scheduler = Mock()
        scheduler.status.return_value = {"running": True, "jobs": []}
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workflow_service=service, scheduler=scheduler)))
        result = health_check(request)
        self.assertFalse(result["automatic_publication"])
        self.assertGreaterEqual(len(result["automation_issues"]), 2)
        self.assertEqual("2026-11-10", result["daily_workflow"]["date"])
        service.trigger_async = Mock(return_value=(True, "accepted"))
        self.assertTrue(trigger_scheduled_workflow(request).accepted)
        service.trigger_async.assert_called_once_with(trigger_source="scheduler")

    def test_network_failures_without_checkpoints_do_not_falsely_complete_day(self):
        service = WorkflowService(self.settings)
        candidate = CandidatePaper("2611.00001v1", "Title", ["A"], "Abstract", ["q-fin.TR"],
                                   datetime.now(timezone.utc), "https://arxiv.org/abs/test", "https://arxiv.org/pdf/test")
        service._rebuild_ai_services = Mock()
        service._arxiv_service = Mock()
        service._arxiv_service.fetch_recent_candidates.return_value = [candidate]
        service._arxiv_service.rank_and_pick.return_value = [(candidate, 1.0)]
        service._paper_pipeline = Mock()
        service._paper_pipeline.run.side_effect = TimeoutError("PDF download timed out")
        service._email_service = Mock()
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            service.run_pipeline("manual")
        self.assertEqual("FAILED", self.db.query(WorkflowRun).one().status)
        self.assertEqual(0, self.db.query(ArticleRevisionTask).count())
        self.assertEqual(0, self.db.query(PublicationTask).count())

    def test_backend_startup_recovers_before_starting_scheduler(self):
        from app import main

        run = self.add_run("RUNNING")
        service = WorkflowService(self.settings)
        scheduler = Mock()
        with patch.object(main, "engine", self.engine), \
             patch.object(main, "SessionLocal", self.sessions), \
             patch.object(main, "settings", self.settings), \
             patch.object(main, "workflow_service", service), \
             patch.object(main, "scheduler_service", scheduler):
            main.on_startup()
        self.db.refresh(run)
        self.assertEqual("INTERRUPTED", run.status)
        scheduler.start.assert_called_once()

    def test_model_account_failure_is_returned_instead_of_api_500(self):
        self.settings.llm_provider = "openai_compatible"
        self.settings.llm_api_key = "test-only-key"
        service = WorkflowService(self.settings)
        llm = Mock()
        llm.enabled = True
        llm.generate.side_effect = LLMProviderUnavailable("Insufficient balance (HTTP 402)")
        with patch("app.services.workflow_service.SessionLocal", self.sessions), \
             patch("app.services.workflow_service.LLMService", return_value=llm):
            result = service.test_llm_connection()
        self.assertFalse(result["ok"])
        self.assertIn("402", result["message"])

    def test_refreshed_session_retains_only_zhihu_cookies(self):
        from pathlib import Path
        from app.services.zhihu_service import ZhihuService

        service = ZhihuService(self.settings)
        context = Mock()
        cookie = {"domain": ".zhihu.com", "name": "z_c0", "value": "test-only-session"}
        context.cookies.return_value = [cookie, {"domain": ".unrelated.test", "name": "private", "value": "not-exported"}]
        profile = Path(self.directory.name)
        self.assertTrue(service._save_managed_session(context, profile))
        stored = json.loads((profile / "autosci-zhihu-cookies.json").read_text(encoding="utf-8"))
        self.assertEqual([cookie], stored)
        new_context = Mock()
        service._restore_managed_session(new_context, profile)
        new_context.add_cookies.assert_called_once_with([cookie])
        self.assertEqual("", service.rate_limit_status()["session_error"])

    def test_session_restore_rejects_non_zhihu_cookies(self):
        from pathlib import Path
        from app.services.zhihu_service import ZhihuService

        profile = Path(self.directory.name)
        (profile / "autosci-zhihu-cookies.json").write_text(json.dumps([{"domain": ".unrelated.test"}]), encoding="utf-8")
        context = Mock()
        with self.assertRaisesRegex(RuntimeError, "non-Zhihu"):
            ZhihuService(self.settings)._restore_managed_session(context, profile)
        context.add_cookies.assert_not_called()

    def test_kimi_k3_text_and_vision_omit_fixed_sampling_parameters(self):
        from app.services.llm_service import LLMService
        from pathlib import Path

        settings = self.settings.model_copy(update={"llm_api_base_url": "https://api.kimi.com/coding/v1",
                                                   "llm_model": "k3.1-pro", "llm_reasoning_effort": "high"})
        service = LLMService(settings)
        service._client = Mock()
        service._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok":true}'))]
        )
        self.assertEqual({"ok": True}, service.generate_json("JSON", "JSON"))
        kwargs = service._client.chat.completions.create.call_args.kwargs
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("max_tokens", kwargs)
        self.assertEqual("high", kwargs["reasoning_effort"])
        image = Path(self.directory.name) / "test.png"
        image.write_bytes(b"test-image")
        self.assertEqual({"ok": True}, service.generate_vision_json("JSON", "JSON", image))
        kwargs = service._client.chat.completions.create.call_args.kwargs
        self.assertNotIn("temperature", kwargs)
        self.assertEqual("high", kwargs["reasoning_effort"])
        self.assertTrue(kwargs["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_new_workflow_does_not_revive_expired_publication_backlog(self):
        run = self.add_run("SUCCESS")
        for idx, age in enumerate((40, 1), start=1):
            self.db.add(PublicationTask(run_id=run.id, paper_id=idx, paper_code=f"R001-0{idx}", title="Title",
                                       note_markdown="Body", requested_by="auto_workflow", status="PENDING",
                                       created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age)))
        self.db.commit()
        queue = PublicationService._list_automatic_publication_queue(self.db)
        self.assertEqual(["R001-02"], [task.paper_code for task in queue])

    def test_quality_worker_does_not_spend_model_quota_after_daily_target_is_met(self):
        self.settings.workflow_auto_publish_after_run = True
        self.settings.zhihu_auto_publish = True
        service = WorkflowService(self.settings)
        service._daily_work_has_priority = Mock(return_value=False)
        service._publication_service = Mock()
        service._publication_service.daily_publication_window_open.return_value = True
        service._publication_service.remaining_daily_publications.return_value = 0
        service._rebuild_ai_services = Mock()
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            result = service.retry_quality_revisions()
        self.assertEqual("daily_publication_limit_reached", result["reason"])
        service._rebuild_ai_services.assert_not_called()

    def test_numeric_repairs_receive_the_same_whitelist_as_the_gate(self):
        from app.services.paper_pipeline import PaperUnderstandingPipeline

        llm = Mock()
        llm.generate_json.return_value = {"text": "该比较结果得到原文证据支持，修复后不再保留未经核验的额外数值。"}
        pipeline = PaperUnderstandingPipeline(llm, self.settings)
        block = SimpleNamespace(id="b1", claim_ids=["c1"], text="The gain is 3.35%")
        claim = SimpleNamespace(id="c1", claim="Table value 3.35", evidence=[])
        pipeline._repair_block(block, {"c1": claim}, ["unsupported numeric token 3.35%"])
        prompt = llm.generate_json.call_args.kwargs["user_prompt"]
        self.assertIn('数字白名单（含单位后缀，必须逐字匹配）：["3.35"]', prompt)
        self.assertIn("不能自行添加百分号", prompt)

    def test_manual_revision_is_scoped_to_explicit_run_even_before_due_time(self):
        old_run = self.add_run("PARTIAL")
        target = self.add_run("PARTIAL")
        for run in (old_run, target):
            candidate = {"arxiv_id": f"2611.{run.id:05d}v1", "title": "Title", "authors": ["A"],
                         "abstract": "A", "categories": ["q-fin.TR"], "published_at": self.now.isoformat(),
                         "url": "u", "pdf_url": "p"}
            self.db.add(ArticleRevisionTask(run_id=run.id, arxiv_id=candidate["arxiv_id"],
                                           candidate_json=json.dumps(candidate), artifact_dir=self.directory.name,
                                           status="PENDING", next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=6)))
        self.db.commit()
        service = WorkflowService(self.settings)
        service._rebuild_ai_services = Mock()
        service._paper_pipeline = Mock()
        service._paper_pipeline.revise_from_checkpoint.side_effect = RuntimeError("stop after target selection")
        with patch("app.services.workflow_service.SessionLocal", self.sessions):
            service.retry_quality_revisions(manual=True, run_id=target.id)
        chosen = service._paper_pipeline.revise_from_checkpoint.call_args.args[0]
        self.assertEqual(f"2611.{target.id:05d}v1", chosen.arxiv_id)
        self.db.expire_all()
        self.assertEqual(0, self.db.query(ArticleRevisionTask).filter_by(run_id=old_run.id).one().attempts)
        self.assertEqual(1, self.db.query(ArticleRevisionTask).filter_by(run_id=target.id).one().attempts)


if __name__ == "__main__":
    unittest.main()
