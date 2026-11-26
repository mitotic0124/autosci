import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.services.zhihu_service import ZhihuService


class PublishedRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = ZhihuService(Settings(_env_file=None, artifacts_dir=self.directory.name,
                                             zhihu_default_tags="Tag", zhihu_column_name="Column"))
        self.payload = {"id": "123", "state": "published", "title": "Title", "content": "Official content",
                        "topics": [{"name": "Tag"}], "column": {"title": "Column"}}
        self.metrics = {"proseText": "Verified body with .", "formulaTexts": ["x"], "imageCount": 0, "imageSources": [], "imageIdentities": []}
        self.page = Mock()
        self.page.context.request.get.return_value = Mock(status=200)
        self.page.context.request.get.return_value.json.return_value = self.payload
        self.page.evaluate.side_effect = lambda *_args: copy.deepcopy(self.metrics)
        self.page.locator.return_value.evaluate_all.return_value = []
        self.service._validate_saved_draft = Mock()
        self.service._read_editor_metrics = Mock(return_value={"invalidImageCount": 0, "unrenderedFormulaCount": 0})

    def verify(self, expected_images=0):
        return self.service._verify_published_api(self.page, "123", "Title", "Verified body with $x$.", expected_images)

    def test_owner_record_and_rendered_editor_confirm_without_resubmission(self):
        self.assertEqual("https://zhuanlan.zhihu.com/p/123", self.verify())
        self.page.context.request.post.assert_not_called()
        self.service._validate_saved_draft.assert_called_once()
        self.assertEqual("published_api", self.service._load_zhihu_state()["public_readback_mode"])

    def test_wrong_identity_or_draft_state_never_proves_publication(self):
        for field, value in (("id", "456"), ("state", "draft"), ("title", "Other"), ("content", "")):
            with self.subTest(field=field):
                changed = {**self.payload, field: value}
                with self.assertRaises(RuntimeError):
                    self.service._assert_published_api_identity(changed, "123", "Title")

    def test_missing_prose_blocks_api_confirmation(self):
        self.metrics["proseText"] = "Missing original body"
        with self.assertRaisesRegex(RuntimeError, "正文缺失"):
            self.verify()
        self.service._validate_saved_draft.assert_not_called()

    def test_changed_equation_blocks_api_confirmation(self):
        self.metrics["formulaTexts"] = ["x+1"]
        with self.assertRaisesRegex(RuntimeError, "公式内容"):
            self.verify()

    def test_missing_image_blocks_api_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "图片数量"):
            self.verify(expected_images=1)

    def test_unrendered_editor_math_still_blocks_confirmation(self):
        self.service._read_editor_metrics.return_value["unrenderedFormulaCount"] = 1
        with self.assertRaisesRegex(RuntimeError, "公式或图片不完整"):
            self.verify()
        self.assertNotIn("public_readback_mode", self.service._load_zhihu_state())

    def test_different_image_identity_blocks_confirmation(self):
        self.metrics.update(imageCount=1, imageSources=["https://pic.zhimg.com/a.jpg"], imageIdentities=["v2-a"])
        self.page.locator.return_value.evaluate_all.return_value = ["https://pic.zhimg.com/b.jpg"]
        with self.assertRaisesRegex(RuntimeError, "图片内容或顺序"):
            self.verify(expected_images=1)

    def test_platform_transcoding_is_matched_by_original_image_token(self):
        self.metrics.update(imageCount=1, imageSources=["https://pic.zhimg.com/transcoded.jpg"], imageIdentities=["v2-original"])
        self.page.locator.return_value.evaluate_all.return_value = ["v2-original"]
        with patch.object(self.service, "_assert_published_image_loads") as load:
            self.verify(expected_images=1)
        load.assert_called_once_with(self.page.context.request, "https://pic.zhimg.com/transcoded.jpg")

    def test_broken_published_media_still_blocks_confirmation(self):
        self.metrics.update(imageCount=1, imageSources=["https://pic.zhimg.com/a.jpg"], imageIdentities=["v2-original"])
        self.page.locator.return_value.evaluate_all.return_value = ["v2-original"]
        with patch.object(self.service, "_assert_published_image_loads", side_effect=RuntimeError("image unavailable")):
            with self.assertRaisesRegex(RuntimeError, "image unavailable"):
                self.verify(expected_images=1)

    def test_owner_api_restriction_is_not_a_success(self):
        self.page.context.request.get.return_value.status = 403
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.verify()

    def test_missing_topic_or_column_still_blocks_published_record(self):
        for field in ("topics", "column"):
            with self.subTest(field=field):
                original = self.payload[field]
                self.payload[field] = [] if field == "topics" else {}
                with self.assertRaises(RuntimeError):
                    self.verify()
                self.payload[field] = original

    def test_public_read_403_uses_record_check_before_global_breaker(self):
        self.page.url = "https://zhuanlan.zhihu.com/p/123"
        self.page.goto.return_value = Mock(status=403)
        with patch.object(self.service, "_verify_published_api", return_value=self.page.url) as verify, \
             patch.object(self.service, "_check_publish_rate_limit") as breaker:
            self.assertEqual(self.page.url, self.service._verify_published_url(self.page, "Title", source_body="Body"))
        verify.assert_called_once()
        breaker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
