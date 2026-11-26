import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz

from app.services.image_service import PaperImageService
from app.services.publication_service import PublicationService
from app.services.zhihu_service import ZhihuService


class _TitleLocator:
    def __init__(self, *, fill_fails: bool = False) -> None:
        self.value = ""
        self.fill_fails = fill_fails
        self.click_called = False

    def count(self) -> int:
        return 1

    def click(self) -> None:
        self.click_called = True
        raise AssertionError("The title writer must not depend on pointer clicks")

    def fill(self, value: str, timeout: int) -> None:
        if self.fill_fails:
            raise RuntimeError("covered by a sticky toolbar")
        self.value = value

    def evaluate(self, _script: str, value: str | None = None):
        if value is not None:
            self.value = value
            return True
        return self.value


class _TitlePage:
    def __init__(self, locator: _TitleLocator) -> None:
        self.title_locator = locator

    def locator(self, selector: str):
        return SimpleNamespace(first=self.title_locator if "textarea[placeholder*='请输入标题']" in selector else _MissingLocator())

    def get_by_placeholder(self, _placeholder: str):
        return SimpleNamespace(first=_MissingLocator())


class _MissingLocator:
    def count(self) -> int:
        return 0


class ZhihuMarkdownConversionTests(unittest.TestCase):
    def test_rate_limit_breaker_requires_manual_clearance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ZhihuService(SimpleNamespace(artifacts_dir=directory, timezone="Asia/Shanghai"))
            service._mark_rate_limited("40362 test")
            self.assertTrue(service.rate_limit_status()["active"])
            with self.assertRaisesRegex(RuntimeError, "知乎发布熔断中"):
                service._ensure_not_rate_limited()

            service.clear_rate_limit_after_manual_check()
            self.assertFalse(service.rate_limit_status()["active"])
            service._ensure_not_rate_limited()

    def test_daily_publication_attempt_is_persisted_and_limits_reached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ZhihuService(SimpleNamespace(
                artifacts_dir=directory,
                timezone="Asia/Shanghai",
                zhihu_max_publications_per_day=1,
            ))
            service._reserve_publication_attempt("R001-01")
            self.assertEqual(0, service.remaining_publication_attempts())
            stored = json.loads((Path(directory) / "zhihu_state.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(stored["public_publication_keys_by_date"][service._publication_date()]))
            with self.assertRaisesRegex(RuntimeError, "每日知乎公开发布上限"):
                service._ensure_publication_capacity()
            stored["public_publication_attempts_by_date"] = {service._publication_date(): 1}
            (Path(directory) / "zhihu_state.json").write_text(json.dumps(stored), encoding="utf-8")
            service.release_publication_attempt("R001-01")
            self.assertEqual(1, service.remaining_publication_attempts())

    def test_corrupt_zhihu_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "zhihu_state.json"
            state_path.write_text("{broken", encoding="utf-8")
            service = ZhihuService(SimpleNamespace(artifacts_dir=directory, timezone="Asia/Shanghai"))
            self.assertTrue(service.rate_limit_status()["active"])
            with self.assertRaisesRegex(RuntimeError, "熔断中"):
                service._ensure_not_rate_limited()

    def test_invalid_cooldown_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "zhihu_state.json"
            state_path.write_text(json.dumps({"rate_limit_until": "not-a-date"}), encoding="utf-8")
            service = ZhihuService(SimpleNamespace(artifacts_dir=directory, timezone="Asia/Shanghai"))
            self.assertTrue(service.rate_limit_status()["active"])
            with self.assertRaisesRegex(RuntimeError, "无法解析"):
                service._ensure_not_rate_limited()

    def test_account_interaction_cooldown_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ZhihuService(SimpleNamespace(
                artifacts_dir=directory,
                timezone="Asia/Shanghai",
                zhihu_min_interaction_interval_seconds=1800,
            ))
            service._reserve_interaction()
            with self.assertRaisesRegex(RuntimeError, "账号交互冷却中"):
                service._ensure_interaction_cooldown()
            state = json.loads((Path(directory) / "zhihu_state.json").read_text(encoding="utf-8"))
            self.assertIn("last_interaction_at", state)
            self.assertEqual(1800, state["interaction_interval_seconds"])

    def test_failed_interaction_does_not_consume_the_whole_local_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = ZhihuService(SimpleNamespace(
                artifacts_dir=directory,
                timezone="Asia/Shanghai",
                zhihu_min_interaction_interval_seconds=0,
            ))
            service._reserve_interaction()
            self.assertTrue(service.has_interacted_today())
            service._ensure_daily_interaction_capacity()

    def test_interaction_cap_is_pending_not_failed(self) -> None:
        self.assertEqual(
            "PENDING",
            PublicationService._status_from_exception(
                RuntimeError("本日知乎账号交互次数已达上限；等待下一个自然日窗口。")
            ),
        )

    def test_writer_links_cannot_be_used_as_new_draft_identity(self) -> None:
        service = ZhihuService(SimpleNamespace())
        page = Mock()
        page.url = "https://zhuanlan.zhihu.com/write"
        page.evaluate.return_value = ["https://zhuanlan.zhihu.com/p/2084489848891629645"]
        self.assertEqual(page.url, service._resolve_draft_url(page))

    def test_publish_5xx_is_treated_as_uncertain(self) -> None:
        response = Mock(status=502)
        service = ZhihuService(SimpleNamespace())
        with self.assertRaisesRegex(RuntimeError, "是否生效不确定"):
            service._assert_publish_response(response)

    def test_definite_publish_rejection_releases_local_reservation(self) -> None:
        service = ZhihuService(SimpleNamespace())
        self.assertTrue(
            service._should_release_publication_reservation(
                True,
                False,
                RuntimeError("知乎拒绝发布：请求被拒绝"),
            )
        )
        self.assertFalse(
            service._should_release_publication_reservation(
                True,
                False,
                RuntimeError("知乎发布请求结果不确定"),
            )
        )
        self.assertFalse(
            service._should_release_publication_reservation(
                True,
                True,
                RuntimeError("知乎拒绝发布：页面校验失败"),
            )
        )

    def test_topic_search_results_do_not_count_as_selected_topics(self) -> None:
        service = ZhihuService(SimpleNamespace(zhihu_default_tags="Topic", zhihu_column_name="Column"))
        state = {"topicText": "Topic", "selectedTags": [], "columnText": "Column", "columnOn": True}
        with patch.object(service, "_publication_settings_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "话题未完成"):
                service._assert_publication_settings(Mock())
            state["selectedTags"] = ["Topic"]
            service._assert_publication_settings(Mock())

    def test_topic_api_selects_exact_and_relevant_topics(self) -> None:
        settings = SimpleNamespace(
            zhihu_default_tags="投资",
            zhihu_topic_api_enabled=True,
            zhihu_topic_max_count=2,
            zhihu_topic_min_relevance=0.45,
        )
        service = ZhihuService(settings)
        page = Mock()
        response = Mock(status=200)
        response.json.return_value = [
            {"type": "topic", "name": "投资", "id": "1"},
            {"type": "topic", "name": "量化投资", "id": "2"},
        ]
        page.context.request.get.return_value = response
        with patch.object(service, "_publication_settings_state", return_value={"selectedTags": []}):
            selected = service._auto_select_topic_names(page, ["投资"], "投资策略与量化投资组合研究")
        self.assertEqual("投资", selected[0])
        self.assertIn("量化投资", selected)

    def test_topic_api_failure_keeps_existing_valid_topic(self) -> None:
        settings = SimpleNamespace(
            zhihu_default_tags="投资",
            zhihu_topic_api_enabled=True,
            zhihu_topic_max_count=2,
            zhihu_topic_min_relevance=0.45,
        )
        service = ZhihuService(settings)
        page = Mock()
        page.context.request.get.side_effect = RuntimeError("offline")
        with patch.object(service, "_publication_settings_state", return_value={"selectedTags": ["投资"]}):
            self.assertEqual(["投资"], service._auto_select_topic_names(page, ["投资"], "投资研究"))

    def test_source_validation_checks_prose_and_formula_order(self) -> None:
        source = "## Method\n\nFirst **paragraph** with $x^2$.\n\nSecond paragraph.\n\n$$y = 2x$$"
        metrics = {"proseText": "MethodFirst paragraph with .Second paragraph.", "formulaTexts": ["x^2", "y=2x"]}
        ZhihuService._assert_source_content(metrics, source)
        for mutation in (
            {**metrics, "proseText": "MethodFirst paragraph with ."},
            {**metrics, "proseText": "MethodSecond paragraph.First paragraph with ."},
            {**metrics, "formulaTexts": ["y=2x", "x^2"]},
            {**metrics, "formulaTexts": ["x^3", "y=2x"]},
        ):
            with self.assertRaises(RuntimeError):
                ZhihuService._assert_source_content(mutation, source)

    def test_saved_draft_reloads_once_on_transient_rendering_failure(self) -> None:
        service = ZhihuService(SimpleNamespace())
        page = Mock()
        page.locator.return_value.first.input_value.return_value = "Title"
        with patch.object(service, "_check_access_block"), patch.object(service, "_dismiss_creator_assistant"), \
             patch.object(service, "_assert_draft_rendered", side_effect=[RuntimeError("公式未生成可视图形"), None]) as check, \
             patch.object(service, "_assert_images_rendered"), patch.object(service, "_assert_publication_settings"), \
             patch.object(service, "_save_draft_validation_snapshot"):
            service._validate_saved_draft(page, "https://zhuanlan.zhihu.com/p/123/edit", "Title", "Body", 0)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(page.goto.call_count, 2)

    def test_public_gate_rejects_lost_media_topics_and_column(self) -> None:
        service = ZhihuService(SimpleNamespace(zhihu_default_tags="Topic", zhihu_column_name="Column"))
        source = "A verified paragraph with $x^2$."
        metrics = {"proseText": "A verified paragraph with .", "formulaTexts": ["x^2"],
                   "imageCount": 1, "invalidImageCount": 0, "unrenderedFormulaCount": 0,
                   "topics": ["Topic"], "columns": ["Column"]}
        service._assert_public_content(metrics, source, 1)
        for changes in ({"imageCount": 0}, {"unrenderedFormulaCount": 1}, {"topics": []}, {"columns": []}, {"formulaTexts": ["x^3"]}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                service._assert_public_content({**metrics, **changes}, source, 1)

    def test_saved_draft_does_not_retry_missing_content(self) -> None:
        service = ZhihuService(SimpleNamespace())
        page = Mock()
        with patch.object(service, "_check_access_block"), patch.object(service, "_dismiss_creator_assistant"), \
             patch.object(service, "_assert_draft_rendered", side_effect=RuntimeError("草稿正文缺失")):
            with self.assertRaisesRegex(RuntimeError, "正文缺失"):
                service._validate_saved_draft(page, "https://zhuanlan.zhihu.com/p/123/edit", "Title", "Body", 0)
        self.assertEqual(page.goto.call_count, 1)

    def test_retryable_zhihu_ui_failure_rewrites_partial_draft(self) -> None:
        task = SimpleNamespace(
            status="FAILED",
            zhihu_url="https://zhuanlan.zhihu.com/p/123/edit",
            error_message="知乎发布超时: Locator.fill timeout before publish request",
        )
        self.assertTrue(PublicationService.is_retryable_automatic_failure(task.error_message))
        self.assertTrue(PublicationService._should_replace_failed_draft(task))

    def test_uncertain_publish_error_is_not_classified_as_retryable(self) -> None:
        message = "知乎发布请求结果不确定，已禁止自动重试以避免重复文章"
        self.assertFalse(PublicationService.is_retryable_automatic_failure(message))

    def test_body_replace_refuses_to_append_when_old_content_cannot_be_cleared(self) -> None:
        page = Mock()
        page.locator.return_value.first.count.return_value = 1
        metrics = {"text": "Old paragraph", "formulaCount": 2, "imageCount": 1}
        with patch.object(ZhihuService, "_read_editor_metrics", return_value=metrics):
            with self.assertRaisesRegex(RuntimeError, "旧正文未清空"):
                ZhihuService._clear_body_editor(page)

    def test_body_replace_checks_text_and_atomic_media_after_selection(self) -> None:
        page = Mock()
        page.locator.return_value.first.count.return_value = 1
        old = {"text": "", "formulaCount": 2, "imageCount": 1}
        empty = {"text": "", "formulaCount": 0, "imageCount": 0}
        with patch.object(ZhihuService, "_read_editor_metrics", side_effect=[old, empty]):
            ZhihuService._clear_body_editor(page)
        page.keyboard.press.assert_any_call("Control+Shift+End")
        page.keyboard.press.assert_any_call("Backspace")

    def test_public_url_without_article_body_is_not_a_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            ZhihuService._assert_public_article_state({
                "title": "", "text": "", "hasEditor": False,
                "pageText": "你似乎来到了没有知识存在的荒原",
            }, "Test article")

    def test_publication_rate_limit_hints_include_zhihu_access_fence(self) -> None:
        self.assertIn("暂时限制本次访问", ZhihuService.RATE_LIMIT_HINTS)
        self.assertIn("当前请求存在异常", ZhihuService.RATE_LIMIT_HINTS)

    def test_access_fence_persists_cooldown_and_uses_retryable_error(self) -> None:
        service = ZhihuService(SimpleNamespace())
        until = 1_800_000_000
        page = Mock()
        page.content.return_value = '<pre>{"code":40362,"message":"暂时限制本次访问"}</pre>'
        with patch.object(service, "_mark_rate_limited", return_value=until) as mark, \
             patch.object(service, "_format_dt", return_value="2026-09-24 16:00"):
            with self.assertRaisesRegex(RuntimeError, "知乎发布熔断.*40362"):
                service._check_access_block(page)

        mark.assert_called_once()

    def test_public_validation_checks_access_fence_after_navigation(self) -> None:
        service = ZhihuService(SimpleNamespace())
        page = Mock()
        page.url = "https://zhuanlan.zhihu.com/p/123/edit"
        page.goto.return_value = Mock(status=200)
        page.evaluate.return_value = ""
        page.content.return_value = ""
        with patch.object(service, "_load_zhihu_state", return_value={}), \
             patch.object(service, "_check_access_block", side_effect=RuntimeError("知乎发布冷却中：40362")) as check:
            with self.assertRaisesRegex(RuntimeError, "40362"):
                service._verify_published_url(page, expected_title="Title")
        check.assert_called_once_with(page)

    def test_public_validation_requires_matching_article_content(self) -> None:
        state = {"title": "Test article", "text": "Verified article content. " * 20, "hasEditor": False}
        ZhihuService._assert_public_article_state(state, "Test article")
        with self.assertRaisesRegex(RuntimeError, "标题"):
            ZhihuService._assert_public_article_state(state, "Different article")
        state["hasEditor"] = True
        with self.assertRaisesRegex(RuntimeError, "未找到公开文章正文"):
            ZhihuService._assert_public_article_state(state, "Test article")

    def test_title_fill_does_not_click_through_overlays(self) -> None:
        locator = _TitleLocator()

        filled = ZhihuService._fill_title(_TitlePage(locator), "A title")

        self.assertTrue(filled)
        self.assertEqual("A title", locator.value)
        self.assertFalse(locator.click_called)

    def test_title_fill_falls_back_to_native_input_event_and_enforces_limit(self) -> None:
        locator = _TitleLocator(fill_fails=True)

        filled = ZhihuService._fill_title(_TitlePage(locator), "A\n" + "b" * 120)

        self.assertTrue(filled)
        self.assertEqual("A " + "b" * 98, locator.value)
        self.assertEqual(100, len(locator.value))
        self.assertFalse(locator.click_called)

    def test_converts_adjacent_inline_and_display_formulas_without_crossing_code(self) -> None:
        source = r"""# Formula check

Inline $\mathbb{E}[X] = \mu$ and $\operatorname{Var}(X) = \sigma^2$ stay separate.

$$
\int_0^T r_t\,dt = \log(P_T / P_0)
$$

`price = "$10"` must remain code.

```python
raw = "$not_math$"
```
"""

        converted, formulas = ZhihuService._convert_latex_formulas(source)

        self.assertEqual(3, len(formulas))
        self.assertEqual(3, ZhihuService._formula_image_count(converted))
        self.assertIn('raw = "$not_math$"', converted)
        self.assertNotIn("$\\mathbb", converted)
        self.assertNotIn("$\\operatorname", converted)

    def test_multiline_formula_with_equals_does_not_become_a_markdown_heading(self) -> None:
        source = """Before

$$
G(A)
=
\\frac{1}{2}\\hat{\\mathbb{E}}[X]
$$

After
"""

        converted, formulas = ZhihuService._convert_latex_formulas(source)
        rendered = ZhihuService._markdown_to_rich_html(converted)

        self.assertEqual(1, len(formulas))
        self.assertEqual(1, ZhihuService._formula_image_count(rendered))
        self.assertNotIn("&lt;img", rendered)
        self.assertNotIn("<h2>", rendered)
        self.assertIn('alt="G(A) = \\frac{1}{2}\\hat{\\mathbb{E}}[X]"', rendered)

    def test_corrupted_stored_formula_cannot_reach_zhihu(self) -> None:
        for source in (r"滑点为 $ ext{Slippage}=frac12 x$。", "$\x0crac{1}{2}x$"):
            with self.assertRaisesRegex(RuntimeError, "公式源码损坏"):
                ZhihuService._convert_latex_formulas(source)

    def test_currency_amounts_do_not_consume_prose_as_math(self) -> None:
        source = 'Will BTC reach $100k+ by December? Or $100k by January? True math $100 + x$ and $x_i$.'
        converted, formulas = ZhihuService._convert_latex_formulas(source)
        self.assertEqual(["100 + x", "x_i"], formulas)
        rendered = ZhihuService._markdown_to_rich_html(converted)
        self.assertIn("$100k+ by December", rendered)
        self.assertIn("$100k by January", rendered)

    def test_text_font_commands_use_zhihu_supported_equivalents(self) -> None:
        _, formulas = ZhihuService._convert_latex_formulas(r"$\|Z\|_{\textnormal{BMO}_G}$ and $\textsc{buy}$")
        self.assertEqual([r"\|Z\|_{\text{BMO}_G}", r"\text{buy}"], formulas)

    def test_trailing_whitespace_in_inline_math_is_rendered(self) -> None:
        source = r"协方差 $\Sigma_0$ = B $\Sigma_f$ $B^\top $。"
        converted, formulas = ZhihuService._convert_latex_formulas(source)
        self.assertEqual([r"\Sigma_0", r"\Sigma_f", r"B^\top"], formulas)
        self.assertNotIn("$", converted)

    def test_converts_tex_environments_and_parenthesized_math(self) -> None:
        source = r"""正文

\begin{cases}
  x + y &= 1 \\
  x - y &= 0
\end{cases}

以及 \(\operatorname{Var}(X) = \sigma^2\)。
"""
        converted, formulas = ZhihuService._convert_latex_formulas(source)
        self.assertEqual(2, len(formulas))
        self.assertEqual(2, ZhihuService._formula_image_count(converted))
        self.assertIn(r"\begin{cases}", formulas[0])
        self.assertIn(r"\operatorname", converted)

    def test_nested_math_is_converted_once_and_retains_alignment(self) -> None:
        source = r"$$\begin{align} f(x) &= \begin{cases}x & x>0 \\ 0 & x\leq0\end{cases} \\ g(x) &= x^2 \end{align}$$"
        converted, formulas = ZhihuService._convert_latex_formulas(source)
        self.assertEqual(1, len(formulas))
        self.assertEqual(1, ZhihuService._formula_image_count(converted))
        self.assertNotIn("<img", formulas[0])
        self.assertIn(r"\begin{aligned}", formulas[0])
        self.assertIn(r"\begin{cases}", formulas[0])
        self.assertNotIn("$$", converted)

    def test_expands_tex_layout_conditionals_for_zhihu_mathjax(self) -> None:
        source = r"$$\ifdefined\ORLtwocolumn \begin{aligned} a&=b \end{aligned} \else \begin{aligned} a&=b,\qquad c&=d \end{aligned} \fi$$"
        converted, formulas = ZhihuService._convert_latex_formulas(source)
        self.assertEqual(1, len(formulas))
        self.assertIn(r"\begin{aligned} a&=b \end{aligned}", formulas[0])
        self.assertNotIn(r"\ifdefined", formulas[0])
        self.assertNotIn(r"\else", formulas[0])
        self.assertNotIn(r"\fi", formulas[0])
        self.assertEqual(1, ZhihuService._formula_image_count(converted))

    def test_converter_never_reprocesses_protected_formulas(self) -> None:
        source = r"Before $x$ then $y$ and $$z = x + y$$ after."
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                artifacts_dir=temp_dir,
                zhihu_markdown_converter_cmd="md2zhihu",
            )
            converted = ZhihuService(settings)._convert_markdown_for_zhihu(source)

        self.assertEqual(3, ZhihuService._formula_image_count(converted))
        self.assertNotIn("$x$", converted)
        self.assertNotIn("$y$", converted)
        self.assertNotIn("$$z", converted)

    def test_removes_only_the_leading_article_heading(self) -> None:
        source = "# Article title\n\n## Method\n\nBody"

        stripped = ZhihuService._strip_leading_h1(source)

        self.assertEqual("## Method\n\nBody", stripped)

    def test_rejects_attachment_file_inputs_for_markdown_import(self) -> None:
        attachment_accept = ".pdf,.md,.docx,.webp,.png,.jpg"
        import_accept = ".md,.doc,.docx,text/markdown"

        self.assertFalse(ZhihuService._is_document_import_file_input(attachment_accept, True))
        self.assertFalse(ZhihuService._is_document_import_file_input(attachment_accept, False))
        self.assertTrue(ZhihuService._is_document_import_file_input(import_accept, False))

    def test_converts_markdown_to_sanitized_rich_html(self) -> None:
        source = """## Method

- First item
- Second item

| Metric | Value |
| --- | ---: |
| Sharpe | 1.2 |

<img src="https://www.zhihu.com/equation?tex=x%5E2" alt="x^2" data-eeimg="1" eeimg="1">

[[AUTOSCI_IMG_001]]

<script>alert('unsafe')</script>
<a href="javascript:alert(1)" onclick="alert(2)">unsafe link</a>
"""

        rendered = ZhihuService._markdown_to_rich_html(source)

        self.assertIn("<h2>Method</h2>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("<table>", rendered)
        self.assertIn("data-eeimg=\"1\"", rendered)
        self.assertIn("[[AUTOSCI_IMG_001]]", rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn("alert('unsafe')", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("onclick", rendered)

        plain_text = ZhihuService._rich_html_to_plain_text(rendered)
        self.assertIn("Method", plain_text)
        self.assertIn("First item", plain_text)
        self.assertIn("[[AUTOSCI_IMG_001]]", plain_text)
        self.assertNotIn("<h2>", plain_text)
        self.assertNotIn("javascript:", plain_text)


class PaperImageExtractionTests(unittest.TestCase):
    def test_renders_vector_figure_region_with_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "vector-figure.pdf"
            output_dir = root / "images"

            doc = fitz.open()
            page = doc.new_page(width=612, height=792)
            page.draw_rect(fitz.Rect(90, 120, 520, 360), color=(0, 0, 0), width=2)
            for index in range(5):
                y = 150 + index * 38
                page.draw_line((110, y), (480, 330 - index * 22), color=(0.1, 0.4, 0.7), width=2)
            page.insert_text((92, 392), "Figure 1. Synthetic vector chart", fontsize=11)
            doc.save(pdf_path)
            doc.close()

            outputs = PaperImageService().extract_relevant_images(pdf_path, output_dir, max_images=1)

            self.assertEqual(1, len(outputs))
            self.assertTrue(outputs[0].exists())
            image_doc = fitz.open(outputs[0])
            try:
                self.assertGreater(image_doc[0].rect.width, 500)
                self.assertGreater(image_doc[0].rect.height, 200)
            finally:
                image_doc.close()


if __name__ == "__main__":
    unittest.main()
