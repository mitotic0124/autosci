import json
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.services.publication_service import PublicationService
from app.services.zhihu_service import ZhihuPublicationError


class PublicationQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_type = Settings

        def isolated_settings(**values):
            options = {"_env_file": None, "artifacts_dir": directory.name}
            options.update(values)
            return settings_type(**options)

        settings_patch = patch(f"{__name__}.Settings", side_effect=isolated_settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)

    def test_rate_limited_task_becomes_retryable_after_manual_clear(self) -> None:
        service = PublicationService(Settings(
            llm_provider="disabled",
            zhihu_auto_publish=True,
            workflow_auto_publish_after_run=True,
        ))
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="RATE_LIMITED",
            zhihu_url=None,
            title="Title",
            note_markdown="# Title\n\nBody",
            error_message="知乎发布冷却中",
            requested_by="auto_workflow",
            command_text="auto_publish_after_run",
        )

        def update(_db, _task_id, **fields):
            for key, value in fields.items():
                setattr(task, key, value)

        with patch.object(service, "remaining_daily_publications", return_value=1), \
             patch.object(service._zhihu_service, "rate_limit_status", return_value={"active": False}), \
             patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch("app.services.publication_service.crud.update_publication_task", side_effect=update), \
             patch.object(service, "_assert_publishable_quality"), \
             patch.object(service._zhihu_service, "publish_article", return_value="https://zhuanlan.zhihu.com/p/123") as publish:
            result = service.publish_codes(None, ["R001-01"], sender="auto_workflow")

        self.assertEqual("PUBLISHED", result["results"][0]["status"])
        self.assertEqual("PUBLISHED", task.status)
        publish.assert_called_once()

    def test_rate_limited_task_stays_blocked_until_manual_clear(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="RATE_LIMITED",
            zhihu_url=None,
            title="Title",
            note_markdown="# Title\n\nBody",
            error_message="知乎发布冷却中",
            requested_by="auto_workflow",
        )
        with patch.object(service, "remaining_daily_publications", return_value=1), \
             patch.object(service._zhihu_service, "rate_limit_status", return_value={"active": True}), \
             patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch.object(service._zhihu_service, "publish_article") as publish:
            result = service.publish_codes(None, ["R001-01"], sender="auto_workflow")

        self.assertEqual("RATE_LIMITED", result["results"][0]["status"])
        publish.assert_not_called()

    def test_manual_clear_moves_rate_limited_tasks_back_to_pending(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True, workflow_auto_publish_after_run=True))
        task = SimpleNamespace(
            id=1, run_id=1, paper_code="R001-01", status="RATE_LIMITED",
            requested_by="auto_workflow", command_text="auto_publish_after_run",
            error_message="知乎发布熔断中",
        )
        run = SimpleNamespace(summary="{}")
        db = Mock()
        db.query.return_value.filter.return_value.all.return_value = [task]
        with patch.object(service._zhihu_service, "release_publication_attempt"), \
             patch("app.services.publication_service.crud.get_run", return_value=run), \
             patch("app.services.publication_service.crud.update_publication_task"):
            # The helper intentionally works on ORM objects; the mocked query
            # is enough to assert the state transition without a live database.
            result = service.reset_rate_limited_retry_budget(db)
        self.assertEqual("PENDING", task.status)
        self.assertIn("人工确认解除", task.error_message)
        self.assertEqual(0, result)

    def test_uncertain_publish_result_is_never_submitted_again(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="PUBLISH_UNCERTAIN",
            zhihu_url="https://zhuanlan.zhihu.com/p/123/edit",
            title="Title",
            note_markdown="# Title\n\nBody",
        )
        with patch.object(service, "remaining_daily_publications", return_value=1), \
             patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch.object(service._zhihu_service, "publish_article") as publish:
            result = service.publish_codes(None, ["R001-01"])

        self.assertEqual("PUBLISH_UNCERTAIN", result["results"][0]["status"])
        publish.assert_not_called()

    def test_daily_cap_queues_article_without_opening_zhihu(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="PENDING",
            zhihu_url=None,
            title="Title",
            note_markdown="# Title\n\nBody",
            error_message=None,
        )
        with patch.object(service, "remaining_daily_publications", return_value=0), \
             patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch("app.services.publication_service.crud.update_publication_task") as update, \
             patch.object(service._zhihu_service, "publish_article") as publish:
            result = service.publish_codes(None, ["R001-01"])

        self.assertEqual("PENDING", result["results"][0]["status"])
        self.assertEqual(1, result["stats"]["PENDING"])
        update.assert_called_once()
        publish.assert_not_called()

    def test_auto_publish_window_prefers_oldest_queued_task(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True, zhihu_max_publications_per_day=1))
        older = SimpleNamespace(id=1, paper_code="R001-01", created_at=1, status="PENDING", error_message=None, requested_by="auto_workflow", command_text=None, note_markdown="Body")
        newer = SimpleNamespace(id=2, paper_code="R002-01", created_at=2, status="PENDING", error_message=None, requested_by="auto_workflow", command_text=None, note_markdown="Body")
        query = SimpleNamespace(order_by=lambda *_args: query)
        query.all = Mock(return_value=[older, newer])
        db = Mock()
        db.query.return_value.filter.return_value = query
        with patch.object(service, "remaining_daily_publications", return_value=1):
            codes, queued = service.select_auto_publication_window(db, [newer])

        self.assertEqual(["R001-01"], codes)
        self.assertEqual(1, queued)
        self.assertEqual("auto_workflow", newer.requested_by)

    def test_draft_validation_timeout_is_retryable(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        failed = SimpleNamespace(
            status="FAILED",
            error_message="知乎草稿保存校验超时：状态=保存中",
            note_markdown="Body",
            requested_by="auto_workflow",
        )
        query = SimpleNamespace(order_by=lambda *_args: query)
        query.all = Mock(return_value=[failed])
        db = Mock()
        db.query.return_value.filter.return_value = query

        queue = service._list_automatic_publication_queue(db)

        self.assertEqual([failed], queue)

    def test_confirmed_unpublished_releases_only_that_task_reservation(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="PUBLISH_UNCERTAIN",
            zhihu_url="https://zhuanlan.zhihu.com/p/123/edit",
            requested_by="auto_workflow",
        )

        def update(_db, _task_id, **fields):
            for key, value in fields.items():
                setattr(task, key, value)

        with patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch("app.services.publication_service.crud.update_publication_task", side_effect=update), \
             patch.object(service._zhihu_service, "release_publication_attempt") as release:
            result = service.resolve_uncertain_publication(None, "R001-01", published=False)

        self.assertEqual("PENDING", result["status"])
        release.assert_called_once_with("R001-01")

    def test_confirmed_published_requires_zhihu_public_article_url(self) -> None:
        with self.assertRaisesRegex(ValueError, r"https://zhuanlan\.zhihu\.com/p/"):
            PublicationService._normalize_public_url("https://example.test/p/123")
        self.assertEqual(
            "https://zhuanlan.zhihu.com/p/123",
            PublicationService._normalize_public_url("https://zhuanlan.zhihu.com/p/123/"),
        )

    def test_superseded_draft_cannot_be_released_by_disabling_quality_checks(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", article_require_verified=False))
        with self.assertRaisesRegex(RuntimeError, "已由新版本替代"):
            service._assert_publishable_quality(None, SimpleNamespace(status="SUPERSEDED"))

    def test_failed_publish_retains_saved_draft_for_retry(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        url = "https://zhuanlan.zhihu.com/p/123/edit"
        task = SimpleNamespace(id=1, paper_code="R001-01", status="DRAFT", zhihu_url=None, title="Title", note_markdown="# Title\n\nBody")

        def update(_db, _task_id, **fields):
            for key, value in fields.items():
                setattr(task, key, value)

        with patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch("app.services.publication_service.crud.update_publication_task", side_effect=update), \
             patch.object(service, "_assert_publishable_quality"), \
             patch.object(service._zhihu_service, "publish_article", side_effect=[ZhihuPublicationError("Click failed", url), url]) as publish:
            result = service.publish_codes(None, ["R001-01"])
            self.assertEqual("FAILED", result["results"][0]["status"])
            self.assertEqual(url, task.zhihu_url)
            service.publish_codes(None, ["R001-01"])
            self.assertEqual(url, publish.call_args.kwargs["draft_url"])
            self.assertFalse(publish.call_args.kwargs["replace_content"])

    def test_failed_draft_validation_is_rewritten_on_retry(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled", zhihu_auto_publish=True))
        url = "https://zhuanlan.zhihu.com/p/123/edit"
        task = SimpleNamespace(
            id=1,
            paper_code="R001-01",
            status="FAILED",
            zhihu_url=url,
            error_message="知乎草稿有 1 个公式未生成可视图形",
            title="Title",
            note_markdown="# Title\n\nBody",
        )

        def update(_db, _task_id, **fields):
            for key, value in fields.items():
                setattr(task, key, value)

        with patch("app.services.publication_service.crud.get_publication_task_by_code", return_value=task), \
             patch("app.services.publication_service.crud.update_publication_task", side_effect=update), \
             patch.object(service, "_assert_publishable_quality"), \
             patch.object(service._zhihu_service, "publish_article", return_value=url) as publish:
            result = service.publish_codes(None, ["R001-01"])

        self.assertEqual("DRAFT", result["results"][0]["status"])
        self.assertTrue(publish.call_args.kwargs["replace_content"])

    def test_legacy_fallback_never_reaches_zhihu(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled"))
        task = SimpleNamespace(run_id=1, paper=SimpleNamespace(arxiv_id="paper-1"))
        run = SimpleNamespace(summary=json.dumps({"article_quality": [{"arxiv_id": "paper-1", "passed": True, "mode": "extractive_fallback"}]}))
        with patch("app.services.publication_service.crud.get_run", return_value=run):
            with self.assertRaisesRegex(RuntimeError, "重新生成"):
                service._assert_publishable_quality(None, task)

    def test_verified_task_can_proceed_to_browser_validation(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled"))
        task = SimpleNamespace(run_id=1, paper=SimpleNamespace(arxiv_id="paper-1"))
        run = SimpleNamespace(summary=json.dumps({"article_quality": [{"arxiv_id": "paper-1", "passed": True, "mode": "verified", "metrics": {"article_context_verified": True, "article_global_verified": True}}]}))
        with patch("app.services.publication_service.crud.get_run", return_value=run):
            service._assert_publishable_quality(None, task)

    def test_local_verification_without_document_review_is_not_publishable(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled"))
        task = SimpleNamespace(run_id=1, paper=SimpleNamespace(arxiv_id="paper-1"))
        run = SimpleNamespace(summary=json.dumps({"article_quality": [{"arxiv_id": "paper-1", "passed": True, "mode": "verified"}]}))
        with patch("app.services.publication_service.crud.get_run", return_value=run):
            with self.assertRaisesRegex(RuntimeError, "重新生成"):
                service._assert_publishable_quality(None, task)

    def test_context_review_without_global_audit_is_not_publishable(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled"))
        task = SimpleNamespace(run_id=1, paper=SimpleNamespace(arxiv_id="paper-1"))
        run = SimpleNamespace(summary=json.dumps({"article_quality": [{"arxiv_id": "paper-1", "passed": True, "mode": "verified", "metrics": {"article_context_verified": True}}]}))
        with patch("app.services.publication_service.crud.get_run", return_value=run):
            with self.assertRaisesRegex(RuntimeError, "重新生成"):
                service._assert_publishable_quality(None, task)

    def test_unverified_figure_caption_blocks_otherwise_verified_article(self) -> None:
        service = PublicationService(Settings(llm_provider="disabled"))
        task = SimpleNamespace(run_id=1, paper=SimpleNamespace(arxiv_id="paper-1"))
        quality = {"arxiv_id": "paper-1", "passed": True, "mode": "verified", "metrics": {"article_context_verified": True, "article_global_verified": True, "figure_count": 1}}
        run = SimpleNamespace(summary=json.dumps({"article_quality": [quality]}))
        with patch("app.services.publication_service.crud.get_run", return_value=run):
            with self.assertRaisesRegex(RuntimeError, "图片说明"):
                service._assert_publishable_quality(None, task)


if __name__ == "__main__":
    unittest.main()
