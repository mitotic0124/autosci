import configparser
import html
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import markdown as markdown_lib
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import Settings
from app.services.math_validation import escape_currency_dollars, malformed_math_reason

logger = logging.getLogger(__name__)


class ZhihuPublicationError(RuntimeError):
    def __init__(self, message: str, draft_url: str | None = None) -> None:
        super().__init__(message)
        self.draft_url = draft_url


class ZhihuPublishUncertainError(ZhihuPublicationError):
    """The publish request may have committed; retrying could create a duplicate."""


class _FormulaRenderSnapshotParser(HTMLParser):
    """Count rendered TeX spans in a serialized editor DOM snapshot."""

    def __init__(self) -> None:
        super().__init__()
        self._target: dict[str, bool] | None = None
        self._depth = 0
        self.items: list[dict[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = values.get("class") or ""
        if self._target is None and tag == "span" and values.get("data-tex") is not None:
            self._target = {"rendered": False, "error": False}
            self._depth = 1
            return
        if self._target is None:
            return
        self._depth += 1
        if tag in {"svg", "math", "mjx-container"} or "katex" in classes:
            self._target["rendered"] = True
        if any(token.lower() in f"{tag} {classes}".lower() for token in ("MathJax_Error", "merror", "katex-error", "data-mjx-error")):
            self._target["error"] = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, _tag: str) -> None:
        if self._target is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.items.append(self._target)
            self._target = None
            self._depth = 0

    @classmethod
    def state(cls, markup: str) -> tuple[int, int]:
        if not isinstance(markup, str):
            return 0, 1
        parser = cls()
        parser.feed(markup)
        unrendered = sum(not item["rendered"] or item["error"] for item in parser.items)
        return len(parser.items), unrendered


class ZhihuPublicationWindowClosed(RuntimeError):
    pass


class _RichHtmlSanitizer(HTMLParser):
    _allowed_tags = {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "del",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
    _void_tags = {"br", "hr", "img"}
    _blocked_tags = {"embed", "iframe", "object", "script", "style"}
    _global_attributes = {"aria-label", "class", "data-eeimg", "data-tex", "eeimg", "title"}
    _tag_attributes = {
        "a": {"href", "rel", "target"},
        "code": {"class"},
        "img": {"alt", "class", "data-eeimg", "data-tex", "eeimg", "src"},
        "td": {"align", "colspan", "rowspan"},
        "th": {"align", "colspan", "rowspan"},
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._blocked_depth = 0

    @staticmethod
    def _safe_url(value: str) -> bool:
        normalized = html.unescape(value or "").strip().lower()
        if not normalized:
            return False
        return not normalized.startswith(("javascript:", "vbscript:", "data:text/html"))

    def _clean_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = self._global_attributes | self._tag_attributes.get(tag, set())
        output: list[str] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            if name not in allowed or name.startswith("on"):
                continue
            value = raw_value or ""
            if name in {"href", "src"} and not self._safe_url(value):
                continue
            output.append(f' {name}="{html.escape(value, quote=True)}"')
        return "".join(output)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags:
            self._blocked_depth += 1
            return
        if self._blocked_depth or tag not in self._allowed_tags:
            return
        suffix = self._clean_attributes(tag, attrs)
        self._parts.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags:
            if self._blocked_depth:
                self._blocked_depth -= 1
            return
        if self._blocked_depth or tag not in self._allowed_tags or tag in self._void_tags:
            return
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self._parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self._blocked_depth:
            self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._blocked_depth:
            self._parts.append(f"&#{name};")

    def sanitized_html(self) -> str:
        return "".join(self._parts)


class _RichHtmlPlainTextExtractor(HTMLParser):
    _line_break_tags = {
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def _append_break(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._line_break_tags:
            self._append_break()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._line_break_tags:
            self._append_break()

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def plain_text(self) -> str:
        value = "".join(self._parts).replace("\xa0", " ")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


class ZhihuService:
    RATE_LIMIT_HINTS = (
        "24小时内发布频率过高",
        "发布频率过高",
        "发布过于频繁",
        "操作过于频繁",
        "暂时限制本次访问",
        "当前请求存在异常",
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._publish_lock = threading.Lock()

    def publish_article(
        self,
        title: str,
        markdown_content: str,
        draft_url: str | None = None,
        *,
        replace_content: bool = False,
        publish_to_public: bool | None = None,
        publication_key: str | None = None,
    ) -> str:
        with self._publish_lock:
            return self._publish_article_locked(
                title,
                markdown_content,
                draft_url,
                replace_content=replace_content,
                publish_to_public=publish_to_public,
                publication_key=publication_key,
            )

    def _publish_article_locked(
        self,
        title: str,
        markdown_content: str,
        draft_url: str | None,
        *,
        replace_content: bool,
        publish_to_public: bool | None,
        publication_key: str | None,
    ) -> str:
        saved_url = draft_url if self._is_edit_url(draft_url) else None
        will_publish = self._settings.zhihu_auto_publish if publish_to_public is None else publish_to_public
        reservation_key = (publication_key or f"manual:{uuid.uuid4().hex}").strip()
        if not self._settings.zhihu_publish_enabled:
            raise RuntimeError("知乎发布功能已禁用。")
        self._ensure_not_rate_limited()
        if will_publish:
            self._ensure_publication_capacity()

        profile_path = self._resolve_firefox_profile_path()
        if not profile_path:
            raise RuntimeError("未找到 Firefox 登录配置，请设置 ZHIHU_FIREFOX_PROFILE_PATH。")
        if will_publish:
            # Keep a cooldown between browser attempts, but do not make one
            # failed pre-publish validation consume the whole local day. The
            # publication-attempt ledger still enforces the daily public-post
            # limit, while the cooldown lets the unattended worker recover
            # from transient editor/math-rendering failures later the same day.
            self._ensure_interaction_cooldown()
            self._reserve_interaction()

        source_body = markdown_content.strip()
        if len(source_body) > 18000:
            raise RuntimeError("文章超过 18000 字符，拒绝截断已核验正文，请调整生成长度后重试。")
        source_body = self._strip_leading_h1(source_body)

        body = self._convert_markdown_for_zhihu(source_body)
        body_text, image_slots = self._split_body_and_images(body)
        publish_request_started = False
        publish_response_confirmed = False
        with sync_playwright() as playwright:
            launch_profile = self._prepare_launch_profile(profile_path)
            try:
                context = playwright.firefox.launch_persistent_context(
                    user_data_dir=str(launch_profile),
                    channel="firefox",
                    headless=self._settings.zhihu_headless,
                    viewport={"width": 1600, "height": 900},
                    firefox_user_prefs={
                        "dom.events.testing.asyncClipboard": True,
                        "dom.events.asyncClipboard.clipboardItem": True,
                    },
                    timeout=45_000,
                )
            except Exception:
                self._cleanup_launch_profile(launch_profile, profile_path)
                raise
            page = None
            session_confirmed = False
            try:
                self._restore_managed_session(context, profile_path)
                page = context.new_page()
                draft_tracker = self._track_draft_saves(page)
                self._ensure_browser_interactable(page)
                target_url = draft_url if self._is_edit_url(draft_url) else (self._settings.zhihu_column_url or "https://zhuanlan.zhihu.com/write")
                page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                self._ensure_browser_interactable(page)
                page.wait_for_timeout(2000)
                self._check_access_block(page)
                self._dismiss_creator_assistant(page)
                if will_publish:
                    self._check_publish_rate_limit(page, stage="打开写作页")

                current_url = page.url.lower()
                if "signin" in current_url or "login" in current_url:
                    self._record_session_status(False)
                    raise RuntimeError(f"检测到知乎未登录或登录态失效，当前URL: {page.url}")

                self._ensure_browser_interactable(page)
                draft_tracker["events"].clear()
                if self._is_edit_url(draft_url) and not replace_content:
                    page.wait_for_timeout(1800)
                    self._assert_body_written(page, min_chars=30)
                    fill_mode = "existing_draft"
                    inserted_images = self._count_editor_images(page)
                else:
                    title_filled = self._fill_title(page, title)
                    if not title_filled:
                        body_text = f"{title}\n\n{body_text}"
                    self._check_access_block(page)
                    fill_mode = self._fill_body(page, body_text)
                    self._assert_body_written(page, min_chars=30)
                    inserted_images = self._upload_images(page, image_slots)
                session_confirmed = True
                try:
                    self._configure_publication_settings(page, article_context=f"{title}\n{source_body}")
                except RuntimeError as exc:
                    if "未找到“发布到专栏”选项" not in str(exc):
                        raise
                    # New drafts can receive their ID before Zhihu mounts the
                    # column selector. A saved-page reload initializes it.
                    saved_url = self._wait_for_draft_saved(page, draft_tracker, source_body)
                    page.goto(saved_url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(2000)
                    self._check_access_block(page)
                    self._dismiss_creator_assistant(page)
                    self._configure_publication_settings(page, article_context=f"{title}\n{source_body}")
                self._assert_draft_rendered(page, source_body, fill_mode)
                if not self._is_edit_url(draft_url) or replace_content:
                    self._assert_images_rendered(page, inserted_images)
                saved_url = self._wait_for_draft_saved(page, draft_tracker, source_body)
                self._validate_saved_draft(page, saved_url, title, source_body, len(image_slots))
                if not will_publish:
                    return saved_url
                self._configure_publication_settings(page, article_context=f"{title}\n{source_body}")
                self._ensure_browser_interactable(page)
                self._reserve_publication_attempt(reservation_key)
                with page.expect_response(
                    lambda response: response.request.method == "POST" and (
                        "/api/v4/content/publish" in response.url
                        or re.search(r"/api/articles/\d+/publish", response.url)
                    ),
                    timeout=30_000,
                ) as publication_response:
                    publish_request_started = True
                    self._click_publish(page)
                self._assert_publish_response(publication_response.value)
                publish_response_confirmed = True
                page.wait_for_timeout(2500)
                # Zhihu's official article record can lag behind the publish
                # response for a few seconds while uploaded images are attached.
                # Retry only the image-count mismatch branch; prose/formula/topic
                # mismatches remain an immediate failure so verification is not
                # weakened. A persistent mismatch is still reported as uncertain.
                verification_error: RuntimeError | None = None
                for verification_attempt in range(3):
                    if verification_attempt:
                        page.wait_for_timeout(6000)
                    try:
                        return self._verify_published_url(
                            page,
                            expected_title=title,
                            source_body=source_body,
                            expected_images=len(image_slots),
                        )
                    except RuntimeError as exc:
                        verification_error = exc
                        if "图片数量与已核验原文不一致" not in str(exc):
                            raise
                assert verification_error is not None
                raise verification_error
            except PlaywrightTimeoutError as exc:
                debug_path = self._save_debug_snapshot(page) if page else ""
                retained_url = saved_url or (page.url if page and self._is_edit_url(page.url) else None)
                if publish_request_started:
                    raise ZhihuPublishUncertainError(
                        f"知乎发布请求结果不确定，已禁止自动重试以避免重复文章: {exc} | 调试快照: {debug_path}",
                        retained_url,
                    ) from exc
                self.release_publication_attempt(reservation_key)
                raise ZhihuPublicationError(f"知乎发布超时: {exc} | 调试快照: {debug_path}", retained_url) from exc
            except Exception as exc:
                debug_path = self._save_debug_snapshot(page) if page else ""
                retained_url = saved_url or (page.url if page and self._is_edit_url(page.url) else None)
                if publish_request_started and (
                    publish_response_confirmed or not self._is_definite_publish_response(exc)
                ):
                    raise ZhihuPublishUncertainError(
                        f"知乎发布请求结果不确定，已禁止自动重试以避免重复文章: {exc} | 调试快照: {debug_path}",
                        retained_url,
                    ) from exc
                # A definite rejection (for example an HTTP 4xx or an explicit
                # rate-limit response) did not create a public article. Release
                # the local reservation so the next queued paper can proceed.
                # Once a successful publish response was observed, keep the
                # reservation and require manual reconciliation instead.
                if self._should_release_publication_reservation(
                    publish_request_started,
                    publish_response_confirmed,
                    exc,
                ):
                    self.release_publication_attempt(reservation_key)
                if "Target page, context or browser has been closed" in str(exc):
                    raise ZhihuPublicationError(
                        f"页面在编辑过程中被关闭，可能触发知乎风控或账号状态异常。调试快照: {debug_path}", retained_url
                    ) from exc
                raise ZhihuPublicationError(f"{exc} | 调试快照: {debug_path}", retained_url) from exc
            finally:
                if session_confirmed:
                    self._save_managed_session(context, profile_path)
                try:
                    context.close()
                except Exception as exc:
                    logger.warning("Closing Zhihu browser context failed: %s", exc)
                self._cleanup_launch_profile(launch_profile, profile_path)

    def _ensure_browser_interactable(self, page) -> None:
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.evaluate(
                """
                () => {
                  try { window.focus(); } catch (e) {}
                  const el = document.activeElement;
                  if (el && typeof el.blur === "function") {
                    try { el.blur(); } catch (e) {}
                  }
                  return true;
                }
                """
            )
        except Exception:
            pass
        if self._settings.zhihu_headless:
            return
        self._activate_firefox_window()
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass

    @staticmethod
    def _activate_firefox_window() -> None:
        if os.name != "nt":
            return
        powershell_cmd = (
            "Add-Type -Namespace Win32 -Name User32 -MemberDefinition '[DllImport(\"user32.dll\")] "
            "public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow); "
            "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd); "
            "[DllImport(\"user32.dll\")] public static extern bool IsIconic(IntPtr hWnd);'; "
            "$procs = Get-Process | Where-Object { "
            "  $_.MainWindowHandle -ne 0 -and ("
            "    $_.ProcessName -match 'firefox|nightly' -or "
            "    $_.MainWindowTitle -match 'Firefox|Nightly|Mozilla|火狐'"
            "  ) "
            "} | Sort-Object StartTime -Descending; "
            "foreach($p in $procs){ "
            "  try { "
            "    $h = [IntPtr]$p.MainWindowHandle; "
            "    if($h -eq [IntPtr]::Zero){ continue }; "
            "    if([Win32.User32]::IsIconic($h)){ [Win32.User32]::ShowWindowAsync($h, 9) | Out-Null } "
            "    else { [Win32.User32]::ShowWindowAsync($h, 5) | Out-Null }; "
            "    [Win32.User32]::SetForegroundWindow($h) | Out-Null; "
            "    Start-Sleep -Milliseconds 120; "
            "    break "
            "  } catch {} "
            "}"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", powershell_cmd],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    @staticmethod
    def _topic_normalize(value: str) -> str:
        return re.sub(r"[\s\u200b\ufeff]+", "", str(value or "")).lower()

    @staticmethod
    def _topic_tokens(value: str) -> set[str]:
        return {
            token.lower()
            for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9+#.-]{1,}", str(value or ""))
        }

    def _topic_api_candidates(self, page, query: str) -> list[dict]:
        query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not query:
            return []
        url = (
            "https://zhuanlan.zhihu.com/api/autocomplete/topics?"
            f"token={quote(query)}&max_matches=8&use_similar=0&topic_filter=1"
        )
        try:
            response = page.context.request.get(url, timeout=15_000)
            if response.status != 200:
                logger.warning("Zhihu topic API returned HTTP %s for query %s", response.status, query)
                return []
            payload = response.json()
            if not isinstance(payload, list):
                return []
            return [
                item for item in payload
                if isinstance(item, dict) and item.get("type") == "topic" and str(item.get("name") or "").strip()
            ]
        except Exception as exc:
            logger.warning("Zhihu topic API request failed for %s: %s", query, exc)
            return []

    def _topic_candidate_score(self, candidate: dict, query: str, article_context: str) -> float:
        name = str(candidate.get("name") or "").strip()
        normalized_name = self._topic_normalize(name)
        normalized_query = self._topic_normalize(query)
        context_normalized = self._topic_normalize(article_context)
        if normalized_name == normalized_query:
            return 1.0
        if normalized_name and normalized_name in context_normalized:
            return 0.92
        name_tokens = self._topic_tokens(name)
        query_tokens = self._topic_tokens(query)
        context_tokens = self._topic_tokens(article_context)
        query_overlap = len(name_tokens & query_tokens) / max(1, len(name_tokens))
        context_overlap = len(name_tokens & context_tokens) / max(1, len(name_tokens))
        return 0.55 * query_overlap + 0.45 * context_overlap

    def _auto_select_topic_names(self, page, configured_tags: list[str], article_context: str) -> list[str]:
        current = self._publication_settings_state(page).get("selectedTags", [])
        configured = [tag for tag in configured_tags if tag]
        if not bool(getattr(self._settings, "zhihu_topic_api_enabled", True)):
            return configured

        max_count = max(1, int(getattr(self._settings, "zhihu_topic_max_count", 2)))
        minimum = max(0.0, min(1.0, float(getattr(self._settings, "zhihu_topic_min_relevance", 0.45))))
        selected: list[str] = [tag for tag in configured if tag in current]
        queries = list(configured)
        context_lines = str(article_context or "").splitlines()
        title = context_lines[0] if context_lines else ""
        title_queries = re.findall(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9+#.-]{1,11}", title)
        queries.extend(chunk for chunk in title_queries if self._topic_normalize(chunk) not in {self._topic_normalize(q) for q in queries})

        scored: dict[str, tuple[float, str]] = {}
        for query in queries[:8]:
            candidates = self._topic_api_candidates(page, query)
            for candidate in candidates:
                name = str(candidate.get("name") or "").strip()
                score = self._topic_candidate_score(candidate, query, article_context)
                key = self._topic_normalize(name)
                if key and (key not in scored or score > scored[key][0]):
                    scored[key] = (score, name)

        for tag in configured:
            if tag in selected:
                continue
            exact = scored.get(self._topic_normalize(tag))
            if exact and exact[0] >= minimum:
                selected.append(exact[1])

        for score, name in sorted(scored.values(), key=lambda item: item[0], reverse=True):
            if len(selected) >= max_count:
                break
            if score < minimum or any(self._topic_normalize(name) == self._topic_normalize(item) for item in selected):
                continue
            selected.append(name)

        if not selected:
            if current:
                return current[:max_count]
            raise RuntimeError("知乎话题 API 未返回与文章相关性达到阈值的话题。")
        logger.info("Zhihu topic selection: configured=%s selected=%s", configured, selected[:max_count])
        return selected[:max_count]

    def _configure_publication_settings(self, page, *, article_context: str = "") -> None:
        """Select API-resolved relevant topics and verify the column before publishing."""
        configured_tags = [
            re.sub(r"\s+", " ", value).strip()
            for value in str(getattr(self._settings, "zhihu_default_tags", "") or "").split(",")
            if re.sub(r"\s+", " ", value).strip()
        ]
        tags = self._auto_select_topic_names(page, configured_tags, article_context)
        column_name = re.sub(r"\s+", " ", str(getattr(self._settings, "zhihu_column_name", "") or "")).strip()

        if tags:
            topic_input = self._wait_for_locator(
                page,
                "input[aria-label='搜索话题'], input[placeholder*='搜索话题']",
                timeout_ms=15_000,
            )
            if topic_input is None:
                raise RuntimeError("知乎发布设置中未找到“搜索话题”入口。")
            for tag in tags:
                for attempt in range(2):
                    if tag in self._publication_settings_state(page).get("selectedTags", []):
                        break
                    if not topic_input.is_visible():
                        add_buttons = page.get_by_role("button", name="添加话题", exact=True)
                        clicked = False
                        for index in range(min(add_buttons.count(), 4)):
                            candidate = add_buttons.nth(index)
                            try:
                                if candidate.is_visible():
                                    candidate.evaluate("element => element.click()")
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            self._click_visible_exact_text(page, "添加话题")
                        # The current editor no longer renders an "添加话题"
                        # button. The selected-topic field itself is the
                        # disclosure control, while the search input remains
                        # mounted but hidden until that field receives a click.
                        if not topic_input.is_visible():
                            for selector in ("xpath=..", "xpath=../.."):
                                try:
                                    topic_input.locator(selector).click(timeout=3_000, force=True)
                                except Exception:
                                    pass
                                if topic_input.is_visible():
                                    break
                        if not topic_input.is_visible():
                            try:
                                field = page.locator("label").filter(has_text="文章话题").last
                                if field.count() > 0:
                                    field.click(timeout=3_000, force=True)
                            except Exception:
                                pass
                        if not topic_input.is_visible():
                            try:
                                topic_input.evaluate(
                                    """element => {
                                      element.style.display = 'block';
                                      element.style.visibility = 'visible';
                                      element.style.opacity = '1';
                                      element.style.pointerEvents = 'auto';
                                      element.focus();
                                      element.dispatchEvent(new Event('focus', {bubbles: true}));
                                    }"""
                                )
                            except Exception:
                                pass
                        topic_input = self._wait_for_locator(
                            page,
                            "input[aria-label='搜索话题'], input[placeholder*='搜索话题']",
                            timeout_ms=8_000,
                        )
                        if topic_input is None:
                            raise RuntimeError("知乎发布设置中未找到“搜索话题”入口。")
                    self._set_topic_search_value(topic_input, tag)
                    page.wait_for_timeout(1600)
                    if not self._click_visible_exact_text(page, tag):
                        result = page.get_by_role("option", name=tag, exact=True).last
                        if result.count() == 0:
                            result = page.get_by_role("listitem", name=tag, exact=True).last
                        if result.count() == 0:
                            raise RuntimeError(f"知乎话题搜索未找到：{tag}")
                        result.click(timeout=8_000)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if tag in self._publication_settings_state(page).get("selectedTags", []):
                            break
                        page.wait_for_timeout(250)
                if tag not in self._publication_settings_state(page).get("selectedTags", []):
                    raise RuntimeError(f"知乎话题未成功选中：{tag}")

        if column_name:
            radio = self._wait_for_locator(page, "#PublishPanel-columnLabel-1", timeout_ms=15_000)
            if radio is None:
                raise RuntimeError("知乎发布设置中未找到“发布到专栏”选项。")
            try:
                if not radio.is_checked():
                    radio.check(force=True)
            except Exception:
                pass
            # Zhihu's custom RadioButton can report a successful Playwright
            # check without updating React state. Click the associated label
            # and fall back to the input itself, then wait for the real checked
            # property before opening the column selector.
            if not radio.is_checked():
                label = page.locator("label[for='PublishPanel-columnLabel-1']").first
                try:
                    if label.count() > 0:
                        label.click(timeout=4_000, force=True)
                except Exception:
                    pass
            if not radio.is_checked():
                try:
                    radio.evaluate("element => { element.click(); element.dispatchEvent(new Event('change', {bubbles: true})); }")
                except Exception:
                    pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not radio.is_checked():
                page.wait_for_timeout(200)
            if not radio.is_checked():
                raise RuntimeError("知乎发布设置未能启用“发布到专栏”。")
            page.wait_for_timeout(900)

            state = self._publication_settings_state(page)
            selected_column = str(state.get("columnText", ""))
            # A reloaded draft normally already has the desired column selected.
            # Re-opening its selector leaves a Popover over the publish button.
            if not state.get("columnOn") or column_name not in selected_column:
                # Selecting the radio reveals the account's available columns.
                column_scope = self._wait_for_locator(page, ".ColumnSetting-ColumnSelector", timeout_ms=8_000)
                selectors = [
                    "[aria-label*='专栏']",
                    "input[placeholder*='专栏']",
                    "button[aria-haspopup='listbox']",
                    "[role='combobox']",
                    "button",
                ]
                opened = False
                for selector in selectors:
                    nodes = column_scope.locator(selector) if column_scope is not None else page.locator(selector)
                    for index in range(min(nodes.count(), 12)):
                        node = nodes.nth(index)
                        try:
                            if not node.is_visible():
                                continue
                            node.click(timeout=3_000)
                            opened = True
                            break
                        except Exception:
                            continue
                    if opened:
                        break
                if not opened:
                    # The column selector can be a div with an onClick handler.
                    opened = self._click_visible_exact_text(page, "发布到专栏")
                    page.wait_for_timeout(500)
                if not opened:
                    raise RuntimeError("知乎专栏选择器未打开。")
                page.wait_for_timeout(500)
                search = page.locator("input[placeholder*='搜索'], input[aria-label*='搜索']").last
                if search.count() > 0 and search.is_visible():
                    search.fill(column_name)
                    page.wait_for_timeout(700)
                if not self._click_visible_exact_text(page, column_name):
                    raise RuntimeError(f"知乎专栏列表中未找到：{column_name}")
                page.wait_for_timeout(500)

            self._close_open_popovers(page)

        self._assert_publication_settings(page)

    @staticmethod
    def _wait_for_locator(page, selector: str, *, timeout_ms: int = 10_000):
        """Wait for a mounted control without letting a missing locator consume 30s."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    return locator
            except Exception:
                pass
            try:
                page.wait_for_timeout(250)
            except Exception:
                break
        return None

    @staticmethod
    def _set_topic_search_value(locator, value: str) -> None:
        """Drive Zhihu's React search input even when its zero-size shell is hidden."""
        locator.evaluate(
            """(element, nextValue) => {
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
              if (setter) setter.call(element, nextValue);
              else element.value = nextValue;
              element.focus();
              element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: nextValue}));
              element.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )

    @staticmethod
    def _publication_settings_state(page) -> dict:
        return page.evaluate(r"""() => {
          const field = Array.from(document.querySelectorAll('label')).find(n => n.textContent.replace(/[\s\u200b]/g, '') === '文章话题');
          const column = document.querySelector('.ColumnSetting-ColumnSelector');
          const selectedTags = field ? Array.from(field.parentElement.querySelectorAll('svg.ZDI--CircleClose24')).map(n => n.closest('button')?.parentElement.innerText.trim()).filter(Boolean) : [];
          return {topicText: field ? field.parentElement.innerText : '', selectedTags, columnText: column ? column.innerText : '', columnOn: !!document.querySelector('#PublishPanel-columnLabel-1:checked')};
        }""")

    def _assert_publication_settings(self, page) -> None:
        tags = [value.strip() for value in self._settings.zhihu_default_tags.split(",") if value.strip()]
        column_name = self._settings.zhihu_column_name.strip()
        state = self._publication_settings_state(page)
        selected_tags = state.get("selectedTags", [])
        missing_tags = [tag for tag in tags if tag not in selected_tags]
        if missing_tags:
            raise RuntimeError(f"知乎文章话题未完成：{', '.join(missing_tags)}")
        if column_name and (not state.get("columnOn") or column_name not in state.get("columnText", "")):
            raise RuntimeError(f"知乎专栏未完成：{column_name}")

    @staticmethod
    def _page_body_text(page) -> str:
        try:
            return str(page.evaluate("() => (document.body && document.body.innerText) || ''"))
        except Exception:
            return ""

    @staticmethod
    def _click_visible_exact_text(page, text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (target) => {
                      const nodes = Array.from(document.querySelectorAll('button, [role="option"], [role="listitem"], li, div, span'));
                      for (const node of nodes.reverse()) {
                        const style = window.getComputedStyle(node);
                        const value = (node.innerText || node.textContent || '').trim();
                        if (style.display === 'none' || style.visibility === 'hidden' || value !== target) continue;
                        const rect = node.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        node.click();
                        return true;
                      }
                      return false;
                    }
                    """,
                    text,
                )
            )
        except Exception:
            return False

    @staticmethod
    def _click_publish(page) -> None:
        ZhihuService._close_open_popovers(page)
        publish_buttons = page.get_by_role("button", name="发布", exact=True)
        if publish_buttons.count() == 0:
            raise RuntimeError("未找到发布按钮。")

        publish_button = publish_buttons.last
        try:
            publish_button.click(timeout=5_000)
        except PlaywrightTimeoutError:
            # force=True still clicks the overlapping image at the same coordinates.
            # Dispatch directly to the enabled button after the normal hit test fails.
            publish_button.evaluate("element => { if (element.disabled) throw new Error('Publish is disabled'); element.click(); }")
        page.wait_for_timeout(800)

        dialog = page.locator("[role='dialog']:visible").first
        if dialog.count() == 0:
            return

        for label in ("确认发布", "立即发布", "发布文章", "发布", "发表"):
            button = dialog.get_by_role("button", name=label, exact=True).first
            if button.count() > 0:
                button.click(timeout=15_000)
                page.wait_for_timeout(1500)
                return

    @staticmethod
    def _close_open_popovers(page) -> None:
        """Close a lingering selector popover before clicking the fixed publish button."""
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
        for selector in (
            "button[role='combobox'][aria-expanded='true']",
            "button[aria-haspopup='listbox'][aria-expanded='true']",
        ):
            try:
                nodes = page.locator(selector)
                for index in range(nodes.count()):
                    node = nodes.nth(index)
                    if node.is_visible():
                        node.click(timeout=2_000)
                        page.wait_for_timeout(200)
            except Exception:
                continue

    @staticmethod
    def _fill_title(page, title: str) -> bool:
        title_value = re.sub(r"[\r\n]+", " ", title).strip()[:100]
        if not title_value:
            return False

        selectors = [
            "h1[contenteditable='true']",
            "div[contenteditable='true'][data-placeholder*='标题']",
            "div[contenteditable='true'][placeholder*='标题']",
            "textarea[placeholder*='请输入标题']",
            "textarea[placeholder*='标题']",
            "input[placeholder*='标题']",
            "textarea.Post-Title-input",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() > 0:
                if ZhihuService._write_title_value(locator, title_value):
                    return True

        fallback = page.get_by_placeholder("请输入标题").first
        if fallback.count() > 0:
            return ZhihuService._write_title_value(fallback, title_value)

        return False

    @staticmethod
    def _write_title_value(locator, title: str) -> bool:
        # `fill` does not require the element to receive pointer events. This is
        # important because Zhihu's sticky toolbar and onboarding tips can cover
        # part of the title control even though the textarea remains editable.
        try:
            locator.fill(title, timeout=10_000)
        except Exception:
            try:
                locator.evaluate(
                    """
                    (element, value) => {
                      element.focus();
                      if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
                        const prototype = element instanceof HTMLTextAreaElement
                          ? HTMLTextAreaElement.prototype
                          : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
                        if (!setter) return false;
                        setter.call(element, value);
                      } else if (element.isContentEditable) {
                        element.textContent = value;
                      } else {
                        return false;
                      }
                      element.dispatchEvent(new InputEvent("input", {
                        bubbles: true,
                        inputType: "insertText",
                        data: value,
                      }));
                      element.dispatchEvent(new Event("change", {bubbles: true}));
                      return true;
                    }
                    """,
                    title,
                )
            except Exception:
                return False

        try:
            written = locator.evaluate(
                """
                (element) => element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement
                  ? element.value
                  : (element.textContent || "")
                """
            )
        except Exception:
            return False
        return str(written).strip() == title

    def _fill_body(self, page, body: str) -> str:
        self._clear_body_editor(page)
        if self._fill_rich_html(page, body):
            return "rich_html"
        if self._import_markdown_file(page, body):
            return "markdown_import"
        if self._fill_markdown_textarea(page, body):
            return "markdown_textarea"
        if self._looks_like_markdown_body(body):
            raise RuntimeError("未找到知乎 Markdown 导入入口，已中止发布以避免草稿显示源码。")

        selectors = [
            "div.public-DraftEditor-content",
            "div.DraftEditor-editorContainer div[contenteditable='true']",
            "div[class*='RichText'] div[contenteditable='true']",
            "article div[contenteditable='true']",
            "main div[contenteditable='true']",
            "div[contenteditable='true']",
        ]
        for selector in selectors:
            locators = page.locator(selector)
            count = min(locators.count(), 6)
            for i in range(count):
                locator = locators.nth(i)
                placeholder = (locator.get_attribute("data-placeholder") or "") + (locator.get_attribute("placeholder") or "")
                if "标题" in placeholder:
                    continue
                locator.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                try:
                    locator.fill(body)
                except Exception:
                    page.keyboard.insert_text(body)
                return "rich_text_fallback"
        raise RuntimeError("未找到知乎正文编辑器，页面结构可能已变化。")

    @classmethod
    def _clear_body_editor(cls, page) -> None:
        editor = page.locator("div.public-DraftEditor-content[contenteditable='true'], div.DraftEditor-editorContainer div[contenteditable='true']").first
        if editor.count() == 0:
            return
        for attempt in range(2):
            metrics = cls._read_editor_metrics(page)
            if not str(metrics.get("text", "")).strip(" \n\r\t\u200b\ufeff") and not metrics.get("formulaCount") and not metrics.get("imageCount"):
                return
            editor.focus()
            if attempt == 0:
                page.keyboard.press("Control+Home")
                page.keyboard.press("Control+Shift+End")
            else:
                editor.evaluate("""element => {
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(element);
                    selection.removeAllRanges();
                    selection.addRange(range);
                }""")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(500)
        metrics = cls._read_editor_metrics(page)
        if str(metrics.get("text", "")).strip(" \n\r\t\u200b\ufeff") or metrics.get("formulaCount") or metrics.get("imageCount"):
            raise RuntimeError("知乎旧正文未清空，已停止写入，避免新旧文章重复叠加。")

    @classmethod
    def _markdown_to_rich_html(cls, body: str) -> str:
        rendered = markdown_lib.markdown(
            body,
            extensions=["extra", "sane_lists"],
            output_format="html5",
        )
        sanitizer = _RichHtmlSanitizer()
        sanitizer.feed(rendered)
        sanitizer.close()
        return sanitizer.sanitized_html().strip()

    @staticmethod
    def _rich_html_to_plain_text(rich_html: str) -> str:
        extractor = _RichHtmlPlainTextExtractor()
        extractor.feed(rich_html)
        extractor.close()
        return extractor.plain_text()

    @classmethod
    def _fill_rich_html(cls, page, body: str) -> bool:
        rich_html = cls._markdown_to_rich_html(body)
        if not rich_html:
            return False
        plain_text = cls._rich_html_to_plain_text(rich_html)
        if not plain_text:
            return False

        selectors = (
            "div.public-DraftEditor-content[contenteditable='true']",
            "div.DraftEditor-editorContainer div[contenteditable='true']",
            "article div[contenteditable='true']",
            "main div[contenteditable='true']",
        )
        for selector in selectors:
            editors = page.locator(selector)
            count = min(editors.count(), 6)
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    placeholder = (editor.get_attribute("data-placeholder") or "") + (
                        editor.get_attribute("placeholder") or ""
                    )
                    if "标题" in placeholder:
                        continue
                    cls._clear_body_editor(page)
                    editor.focus()
                    clipboard_written = False
                    try:
                        clipboard_written = bool(
                            page.evaluate(
                                """
                                async ({richHtml, plainText}) => {
                                  if (!navigator.clipboard || typeof ClipboardItem === "undefined") {
                                    return false;
                                  }
                                  const item = new ClipboardItem({
                                    "text/html": new Blob([richHtml], {type: "text/html"}),
                                    "text/plain": new Blob([plainText], {type: "text/plain"}),
                                  });
                                  await navigator.clipboard.write([item]);
                                  return true;
                                }
                                """,
                                {"richHtml": rich_html, "plainText": plain_text},
                            )
                        )
                        if not clipboard_written:
                            continue
                        editor.focus()
                        page.keyboard.press("Control+V")
                        page.wait_for_timeout(900)
                        metrics = cls._read_editor_metrics(page)
                        written_text = str(metrics.get("text", "")).strip()
                        if len(written_text) >= min(20, max(1, len(plain_text.strip()))):
                            return True
                    finally:
                        if clipboard_written:
                            try:
                                page.evaluate(
                                    """
                                    async () => {
                                      if (navigator.clipboard) {
                                        await navigator.clipboard.writeText("");
                                      }
                                    }
                                    """
                                )
                            except Exception:
                                pass
                except Exception:
                    continue
        return False

    @staticmethod
    def _dismiss_creator_assistant(page) -> None:
        close_buttons = page.locator("button[aria-label='关闭创作助手']")
        if close_buttons.count() == 0:
            return
        try:
            close_buttons.first.evaluate("(element) => element.click()")
        except Exception:
            return
        page.wait_for_timeout(350)

    @staticmethod
    def _fill_markdown_textarea(page, body: str) -> bool:
        selectors = [
            "textarea[placeholder*='请输入正文']",
            "textarea[placeholder*='正文']",
            "textarea[placeholder*='Markdown']",
            "textarea[aria-label*='正文']",
            "textarea[aria-label*='Markdown']",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.click()
            locator.fill(body)
            page.wait_for_timeout(600)
            return True
        return False

    def _import_markdown_file(self, page, body: str) -> bool:
        temp_dir = Path(self._settings.artifacts_dir) / "zhihu_debug"
        temp_dir.mkdir(parents=True, exist_ok=True)
        md_path = temp_dir / f"zhihu_import_{int(time.time() * 1000)}.md"
        md_path.write_text(body, encoding="utf-8")

        def detect_import_error() -> str | None:
            for token in ("文件加载失败", "素材文件加载失败", "导入失败", "上传失败"):
                try:
                    if page.get_by_text(token).count() > 0:
                        return token
                except Exception:
                    continue
            return None

        def is_file_picker_open() -> bool:
            try:
                return bool(
                    page.evaluate(
                        """
                        () => {
                          const candidates = Array.from(
                            document.querySelectorAll("[role='dialog'], .Modal-wrapper, .Modal")
                          );
                          return candidates.some((node) => {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            if (!style || style.display === "none" || style.visibility === "hidden") {
                              return false;
                            }
                            const text = (node.innerText || node.textContent || "").trim();
                            return text.includes("选择文件");
                          });
                        }
                        """
                    )
                )
            except Exception:
                return False

        def complete_file_picker() -> bool:
            selected = False
            for _ in range(12):
                error_token = detect_import_error()
                if error_token:
                    raise RuntimeError(f"正文导入失败：知乎提示“{error_token}”。")

                if not is_file_picker_open():
                    self._wait_for_imported_body(page, expected_filename=md_path.name)
                    return True

                dialog = page.locator(
                    ".Modal-wrapper:has-text('选择文件'), .Modal:has-text('选择文件'), [role='dialog']:has-text('选择文件')"
                ).first

                if not selected:
                    try:
                        file_labels = dialog.get_by_text(md_path.name, exact=True)
                        if file_labels.count() == 0:
                            page.wait_for_timeout(400)
                            continue
                        file_label = file_labels.first
                        rows = file_label.locator("xpath=ancestor::*[@tabindex='0'][1]")
                        if rows.count() > 0:
                            rows.first.click(timeout=3_000, force=True)
                        else:
                            file_label.click(timeout=3_000, force=True)
                        selected = True
                        page.wait_for_timeout(500)
                    except Exception:
                        selected = False
                        page.wait_for_timeout(400)
                        continue

                confirmed = False
                for action_label in ("添加文件", "导入", "确认", "确定", "完成", "继续", "插入", "下一步"):
                    button = dialog.get_by_role("button", name=action_label).first
                    if button.count() > 0:
                        try:
                            button.click(timeout=2_500)
                            confirmed = True
                            break
                        except Exception:
                            pass
                    try:
                        action_labels = dialog.get_by_text(action_label, exact=True)
                        if action_labels.count() == 0:
                            continue
                        action = action_labels.first.locator("xpath=ancestor::*[@tabindex='0'][1]")
                        if action.count() > 0:
                            action.first.click(timeout=3_000, force=True)
                        else:
                            action_labels.first.click(timeout=3_000, force=True)
                        confirmed = True
                        break
                    except Exception:
                        continue

                if not confirmed:
                    try:
                        action_nodes = dialog.locator("[aria-disabled='false']")
                        count = min(action_nodes.count(), 12)
                        for index in range(count):
                            action = action_nodes.nth(index)
                            text = (action.inner_text(timeout=700) or "").strip()
                            if md_path.name in text or not text:
                                continue
                            if any(token in text for token in ("选择", "添加", "导入", "确认", "确定", "完成", "继续", "插入")):
                                action.click(timeout=3_000, force=True)
                                confirmed = True
                                break
                    except Exception:
                        confirmed = False

                page.wait_for_timeout(900 if confirmed else 450)

            if is_file_picker_open():
                return False
            self._wait_for_imported_body(page, expected_filename=md_path.name)
            return True

        def try_set_file(scope=None) -> bool:
            host = scope or page
            selectors = [
                "input[type='file'][accept*='.md']",
                "input[type='file'][accept*='markdown']",
                "input[type='file'][accept*='text/markdown']",
                "input[type='file'][accept*='text/plain']",
                "input[type='file'][accept*='.doc']",
                "input[type='file'][accept*='.docx']",
                "input[type='file'][accept*='msword']",
                "input[type='file'][accept*='officedocument.wordprocessingml.document']",
            ]
            for selector in selectors:
                file_inputs = host.locator(selector)
                count = min(file_inputs.count(), 5)
                for idx in range(count):
                    item = file_inputs.nth(idx)
                    try:
                        accept = item.get_attribute("accept") or ""
                        allows_multiple = item.get_attribute("multiple") is not None
                        if not self._is_document_import_file_input(accept, allows_multiple):
                            continue
                        item.set_input_files(str(md_path))
                        page.wait_for_timeout(3200)
                        if complete_file_picker():
                            return True
                    except Exception:
                        continue
            generic_inputs = host.locator("input[type='file']")
            count = min(generic_inputs.count(), 8)
            for idx in range(count):
                item = generic_inputs.nth(idx)
                try:
                    accept = (item.get_attribute("accept") or "").lower()
                    allows_multiple = item.get_attribute("multiple") is not None
                except Exception:
                    accept = ""
                    allows_multiple = False
                if not self._is_document_import_file_input(accept, allows_multiple):
                    continue
                try:
                    item.set_input_files(str(md_path))
                    page.wait_for_timeout(3200)
                    if complete_file_picker():
                        return True
                except Exception:
                    continue
            return False

        def try_file_chooser(locator) -> bool:
            try:
                with page.expect_file_chooser(timeout=4_500) as chooser_info:
                    locator.click(timeout=4_500)
                chooser_info.value.set_files(str(md_path))
                page.wait_for_timeout(3200)
                return complete_file_picker()
            except Exception:
                return False

        def try_choose_local_document() -> bool:
            for label in ("选择本地文档", "选择本地文件", "本地文档", "本地文件", "上传文档", "上传文件"):
                button = page.get_by_role("button", name=label).first
                if button.count() == 0:
                    continue
                if try_file_chooser(button):
                    return True
                try:
                    button.click(timeout=4_500)
                    page.wait_for_timeout(500)
                except Exception:
                    continue
                if try_set_file():
                    return True
            dialog = page.locator("[role='dialog']").last
            if dialog.count() > 0 and try_set_file(dialog):
                return True
            return False

        def try_menu_entries() -> bool:
            for menu_label in (
                "导入文档",
                "导入文档 MD/Doc",
                "导入文档 MD/DOC",
                "MD/Doc",
                "MD/DOC",
                "Markdown 文件",
                "Markdown",
                "导入 Markdown",
                "上传 Markdown",
                "本地文件",
                "本地文档",
            ):
                for role in ("menuitem", "button"):
                    menu_entry = page.get_by_role(role, name=menu_label).first
                    if menu_entry.count() == 0:
                        continue
                    if try_file_chooser(menu_entry):
                        return True
                    try:
                        menu_entry.click(timeout=4_500)
                        page.wait_for_timeout(500)
                    except Exception:
                        continue
                    if try_set_file():
                        return True
                    if try_choose_local_document():
                        return True
            return False

        if try_set_file():
            return True

        for label in ("导入 Markdown", "导入markdown", "导入文档", "导入", "Markdown"):
            button = page.get_by_role("button", name=label).first
            if button.count() == 0:
                continue
            if try_file_chooser(button):
                return True
            page.wait_for_timeout(500)
            if try_set_file():
                return True
            if try_menu_entries():
                return True
            if try_choose_local_document():
                return True
            try:
                button.click(timeout=8_000)
                page.wait_for_timeout(600)
                if try_set_file():
                    return True
                if try_menu_entries():
                    return True
                if try_choose_local_document():
                    return True
            except Exception:
                continue

        if is_file_picker_open():
            return complete_file_picker()
        return False

    @staticmethod
    def _is_document_import_file_input(accept: str, allows_multiple: bool) -> bool:
        if allows_multiple:
            return False
        normalized = (accept or "").lower()
        document_tokens = (".md", "markdown", ".doc", ".docx", "msword", "wordprocessingml")
        attachment_tokens = ("image/", ".png", ".jpg", ".pdf", ".ppt", ".xls", ".epub", ".mobi")
        return any(token in normalized for token in document_tokens) and not any(
            token in normalized for token in attachment_tokens
        )

    @classmethod
    def _wait_for_imported_body(
        cls,
        page,
        min_chars: int = 30,
        timeout_ms: int = 60_000,
        expected_filename: str = "",
    ) -> None:
        error_tokens = ("文件加载失败", "素材文件加载失败", "导入失败", "上传失败", "解析失败")
        pending_tokens = ("正在导入", "导入中", "正在转换", "转换中", "正在解析", "解析中")
        deadline = time.time() + timeout_ms / 1000.0
        stable_rounds = 0
        last_length = 0
        last_pending = ""
        picker_open = False

        while time.time() < deadline:
            metrics = cls._read_editor_metrics(page)
            editor_text = str(metrics.get("text", "")).strip()
            current_length = len(editor_text)
            try:
                state = page.evaluate(
                    """
                    (expectedFilename) => {
                      const dialogs = Array.from(
                        document.querySelectorAll("[role='dialog'], .Modal-wrapper, .Modal")
                      );
                      const pickerOpen = dialogs.some((node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style || style.display === "none" || style.visibility === "hidden") {
                          return false;
                        }
                        const text = (node.innerText || node.textContent || "").trim();
                        return text.includes("选择文件");
                      });
                      const attachmentInserted = expectedFilename
                        ? Array.from(document.querySelectorAll(".LinkCard--fileLinkCard")).some((node) =>
                            [
                              node.getAttribute("data-text") || "",
                              node.getAttribute("data-draft-title") || "",
                              node.textContent || "",
                            ].some((value) => value.includes(expectedFilename))
                          )
                        : false;
                      return {
                        pickerOpen,
                        attachmentInserted,
                        pageText: (document.body && document.body.innerText) || "",
                      };
                    }
                    """,
                    expected_filename,
                )
            except Exception:
                state = {}

            page_text = str(state.get("pageText", "")) if isinstance(state, dict) else ""
            picker_open = bool(state.get("pickerOpen", False)) if isinstance(state, dict) else False
            attachment_inserted = bool(state.get("attachmentInserted", False)) if isinstance(state, dict) else False
            if attachment_inserted:
                raise RuntimeError("正文导入失败：Markdown 文件被插入为附件卡片，未转换为富文本。")
            error_token = next((token for token in error_tokens if token in page_text), "")
            if error_token:
                raise RuntimeError(f"正文导入失败：知乎提示“{error_token}”。")

            last_pending = next((token for token in pending_tokens if token in page_text), "")
            body_ready = current_length >= min_chars and not picker_open and not last_pending
            if body_ready:
                stable_rounds = stable_rounds + 1 if current_length == last_length else 1
                if stable_rounds >= 3:
                    return
            else:
                stable_rounds = 0

            last_length = current_length
            page.wait_for_timeout(500)

        status = last_pending or ("选择文件弹窗未关闭" if picker_open else "未识别到转换状态")
        raise RuntimeError(
            f"正文导入超时：等待 {timeout_ms // 1000} 秒后正文长度为 {last_length}，状态={status}。"
        )

    def _upload_images(self, page, image_slots: list[tuple[str, Path]]) -> int:
        baseline_count = self._count_editor_images(page)
        if not image_slots:
            return baseline_count

        missing = [str(path) for _, path in image_slots if not path.exists() or not path.is_file()]
        if missing:
            raise RuntimeError(f"图片文件不存在，已中止保存：{'; '.join(missing[:3])}")
        if len(image_slots) > 20:
            raise RuntimeError(f"单篇文章包含 {len(image_slots)} 张本地图片，超过 20 张安全上限。")

        total = len(image_slots)
        for idx, (marker, path) in enumerate(image_slots, start=1):
            is_last = idx == total
            expected_after_insert = baseline_count + idx
            uploaded = False
            last_error: Exception | None = None
            for attempt in range(1, 4):
                current_count = self._count_editor_images(page)
                if current_count == expected_after_insert:
                    uploaded = True
                    break
                if current_count > expected_after_insert:
                    raise RuntimeError(
                        f"图片重复插入：处理 {marker} 后正文已有 {current_count} 张，"
                        f"期望 {expected_after_insert} 张。"
                    )
                if not self._set_cursor_to_marker(page, marker):
                    last_error = RuntimeError(f"未找到图片占位标记：{marker}")
                    break
                try:
                    self._upload_inline_image_at_cursor(
                        page,
                        path,
                        timeout_ms=60_000 if is_last else 40_000,
                    )
                    uploaded = True
                    break
                except Exception as exc:
                    last_error = exc
                    self._ensure_browser_interactable(page)
                    page.wait_for_timeout(2_500)
                    current_count = self._count_editor_images(page)
                    if current_count == expected_after_insert:
                        uploaded = True
                        break
                    if current_count > expected_after_insert:
                        raise RuntimeError(
                            f"图片重复插入：处理 {marker} 后正文已有 {current_count} 张，"
                            f"期望 {expected_after_insert} 张。"
                        ) from exc
            if not uploaded:
                raise RuntimeError(
                    f"图片插入失败（{idx}/{total}）：{path.name}"
                    + (f"；原因：{last_error}" if last_error else "")
                )
            actual_count = self._count_editor_images(page)
            if actual_count != expected_after_insert:
                raise RuntimeError(
                    f"图片数量校验失败：处理 {marker} 后正文有 {actual_count} 张，"
                    f"期望 {expected_after_insert} 张。"
                )
            if not self._remove_marker_text(page, marker):
                raise RuntimeError(f"图片已上传，但占位标记清理失败：{marker}")
            if is_last:
                page.wait_for_timeout(1200)
        return baseline_count + total

    def _upload_inline_image_at_cursor(self, page, image_path: Path, timeout_ms: int = 15_000) -> None:
        before_count = self._count_editor_images(page)
        image_button = page.get_by_role("button", name="图片", exact=True)
        last_error: Exception | None = None

        # Prefer the editor's hidden inline-image input. Setting the file on
        # this input preserves the Draft.js selection at the marker; opening
        # the visible picker first can move focus to the preceding image after
        # the editor remounts, which is how duplicate images and stale markers
        # were produced on the fourth figure.
        direct_inputs = page.locator("input[type='file'][accept*='image']")
        for index in range(min(direct_inputs.count(), 8)):
            input_locator = direct_inputs.nth(index)
            try:
                accept = (input_locator.get_attribute("accept") or "").lower()
                if "image" not in accept:
                    continue
                input_locator.set_input_files(str(image_path))
                self._wait_image_upload_stable(page, timeout_ms=timeout_ms)
                self._wait_for_editor_image_count(page, before_count + 1, timeout_ms=timeout_ms)
                self._dismiss_image_picker(page)
                return
            except Exception as exc:
                last_error = exc
                self._dismiss_unexpected_editor_dialog(page)

        if image_button.count() > 0:
            try:
                image_button.last.evaluate("(element) => element.click()")
                page.wait_for_timeout(500)
                modal = page.locator(".Modal:visible").last
                # Zhihu currently advertises a concrete list of image MIME
                # types rather than the older literal `image/*` accept value.
                # Restrict to the single-file picker so the document
                # attachment input is never mistaken for an inline image.
                dialog_input = modal.locator("input[type='file'][accept*='image']:not([multiple])")
                if modal.count() > 0 and dialog_input.count() > 0:
                    dialog_input.first.set_input_files(str(image_path))
                    self._wait_image_upload_stable(page, timeout_ms=timeout_ms)

                    deadline = time.time() + timeout_ms / 1000.0
                    insert_button = modal.get_by_role("button", name="插入图片", exact=True)
                    while time.time() < deadline:
                        if insert_button.count() > 0 and insert_button.first.is_visible() and insert_button.first.is_enabled():
                            try:
                                insert_button.first.click(timeout=5_000)
                            except Exception:
                                insert_button.first.evaluate("(element) => element.click()")
                            break
                        page.wait_for_timeout(350)
                    else:
                        raise RuntimeError("图片已上传到知乎面板，但未找到可用的“插入图片”按钮。")

                    self._wait_for_editor_image_count(page, before_count + 1, timeout_ms=timeout_ms)
                    self._dismiss_image_picker(page)
                    return
            except Exception as exc:
                # A mounted picker can accept the file but fail to insert it at
                # the current Draft.js selection. Continue through the hidden
                # input/file-chooser paths before reporting the failure.
                last_error = exc
                self._dismiss_unexpected_editor_dialog(page)

        selectors = [
            "input[type='file'][multiple][accept*='image/webp']",
            "input[type='file'][multiple][accept*='image']",
            "input[type='file'][accept*='image/webp']",
            "input[type='file'][accept*='image']",
        ]
        for selector in selectors:
            file_inputs = page.locator(selector)
            count = min(file_inputs.count(), 6)
            for idx in range(count):
                input_locator = file_inputs.nth(idx)
                try:
                    accept = (input_locator.get_attribute("accept") or "").lower()
                except Exception:
                    accept = ""
                if accept and "image" not in accept:
                    continue
                try:
                    input_locator.set_input_files(str(image_path))
                    self._wait_image_upload_stable(page, timeout_ms=timeout_ms)
                    self._wait_for_editor_image_count(page, before_count + 1, timeout_ms=timeout_ms)
                    self._dismiss_image_picker(page)
                    return
                except Exception as exc:
                    last_error = exc
                    self._dismiss_unexpected_editor_dialog(page)
                    continue

        if image_button.count() > 0:
            try:
                with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    try:
                        image_button.last.click(timeout=5_000)
                    except Exception:
                        image_button.last.click(timeout=3_000, force=True)
                chooser_info.value.set_files(str(image_path))
                self._wait_image_upload_stable(page, timeout_ms=timeout_ms)
                self._wait_for_editor_image_count(page, before_count + 1, timeout_ms=timeout_ms)
                self._dismiss_image_picker(page)
                return
            except Exception as exc:
                last_error = exc
                self._dismiss_unexpected_editor_dialog(page)
        if last_error:
            raise RuntimeError(f"图片上传后未进入正文：{image_path.name}；最后原因：{last_error}") from last_error
        raise RuntimeError(f"未找到正文图片上传入口，无法插入图片：{image_path.name}")

    @staticmethod
    def _dismiss_unexpected_editor_dialog(page) -> None:
        visible_dialogs = page.locator("[role='dialog']:visible, .Modal-wrapper:visible, .Modal:visible")
        if visible_dialogs.count() == 0:
            return
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            return

    @staticmethod
    def _dismiss_image_picker(page) -> None:
        def visible_picker_modal():
            modals = page.locator(".Modal:visible, [role='dialog']:visible")
            for index in range(modals.count() - 1, -1, -1):
                modal = modals.nth(index)
                try:
                    text = (modal.inner_text(timeout=1_000) or "").strip()
                    if "上传图片" in text and ("公共图片库" in text or "插入图片" in text):
                        return modal
                except Exception:
                    continue
            return None

        def picker_visible() -> bool:
            if visible_picker_modal() is not None:
                return True
            for text in ("上传图片", "公共图片库", "手机扫码上传"):
                nodes = page.get_by_text(text, exact=True)
                for index in range(min(nodes.count(), 5)):
                    try:
                        if nodes.nth(index).is_visible():
                            return True
                    except Exception:
                        continue
            return False

        if not picker_visible():
            return

        modal = visible_picker_modal()
        if modal is not None:
            modal_close_controls = modal.locator(
                ".Modal-closeButton, button[aria-label='关闭'], button[aria-label*='关闭'], "
                "button[aria-label='Close'], button[aria-label*='close' i]"
            )
            for index in range(min(modal_close_controls.count(), 8)):
                control = modal_close_controls.nth(index)
                try:
                    if not control.is_visible():
                        continue
                    control.click(timeout=2_500)
                    page.wait_for_timeout(350)
                    if not picker_visible():
                        return
                except Exception:
                    continue

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)
        except Exception:
            pass
        if not picker_visible():
            return

        close_controls = page.locator(
            ".Modal-closeButton, button[aria-label='关闭'], button[aria-label*='关闭图片'], "
            "button[aria-label='Close'], button[aria-label*='close' i]"
        )
        for index in range(min(close_controls.count(), 12)):
            control = close_controls.nth(index)
            try:
                if not control.is_visible():
                    continue
                control.click(timeout=2_500)
                page.wait_for_timeout(350)
                if not picker_visible():
                    return
            except Exception:
                continue
        raise RuntimeError("图片已插入正文，但知乎图片选择弹窗未能关闭。")

    @staticmethod
    def _wait_image_upload_stable(page, timeout_ms: int = 15_000) -> None:
        def any_visible(locator) -> bool:
            count = min(locator.count(), 12)
            for index in range(count):
                try:
                    if locator.nth(index).is_visible():
                        return True
                except Exception:
                    continue
            return False

        def visible_text(text: str) -> bool:
            return any_visible(page.get_by_text(text, exact=True))

        deadline = time.time() + timeout_ms / 1000.0
        stable_rounds = 0
        while time.time() < deadline:
            if visible_text("素材文件加载失败") or visible_text("文件加载失败"):
                raise RuntimeError("图片上传失败：知乎提示“素材文件加载失败”。")
            if visible_text("上传失败"):
                raise RuntimeError("图片上传失败：知乎提示“上传失败”。")

            pending = (
                visible_text("上传中")
                or visible_text("处理中")
                or visible_text("加载中")
                or any_visible(page.locator("[role='progressbar']"))
                or any_visible(page.locator("[class*='progress']"))
            )
            if pending:
                stable_rounds = 0
            else:
                stable_rounds += 1
                if stable_rounds >= 3:
                    return
            page.wait_for_timeout(450)

        raise RuntimeError("图片上传超时：知乎未在预期时间内完成处理。")

    @staticmethod
    def _count_editor_images(page) -> int:
        try:
            return int(
                page.evaluate(
                    """
                    () => {
                      const root = document.querySelector("div.public-DraftEditor-content")
                        || document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                      if (!root) return 0;
                      return Array.from(root.querySelectorAll("img")).filter((img) => {
                        const src = img.getAttribute("src") || "";
                        return !img.hasAttribute("data-eeimg")
                          && !img.hasAttribute("eeimg")
                          && !/equation\\?tex=/i.test(src);
                      }).length;
                    }
                    """
                )
                or 0
            )
        except Exception:
            return 0

    @classmethod
    def _wait_for_editor_image_count(cls, page, expected: int, timeout_ms: int) -> None:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            current = cls._count_editor_images(page)
            if current == expected:
                return
            if current > expected:
                raise RuntimeError(f"图片重复插入：正文已有 {current} 张，期望 {expected} 张。")
            page.wait_for_timeout(400)
        raise RuntimeError(f"图片上传后未进入正文：期望至少 {expected} 张。")

    @staticmethod
    def _set_cursor_to_marker(page, marker: str) -> bool:
        try:
            selected = bool(
                page.evaluate(
                    """
                    (target) => {
                      const textareas = Array.from(document.querySelectorAll("textarea"));
                      for (const ta of textareas) {
                        const value = ta && typeof ta.value === "string" ? ta.value : "";
                        const idx = value.indexOf(target);
                        if (idx >= 0) {
                          ta.focus();
                          ta.setSelectionRange(idx, idx);
                          return true;
                        }
                      }
                      const root =
                        document.querySelector("div.public-DraftEditor-content") ||
                        document.querySelector("[data-contents='true']") ||
                        document.querySelector("main") ||
                        document.body;
                      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                      let node = walker.nextNode();
                      while (node) {
                        const value = node.nodeValue || "";
                        const idx = value.indexOf(target);
                        if (idx >= 0) {
                          const editable = node.parentElement && node.parentElement.closest("[contenteditable='true']");
                          if (editable) editable.focus();
                          if (node.parentElement) node.parentElement.scrollIntoView({block: 'center'});
                          const range = document.createRange();
                          range.setStart(node, idx);
                          range.setEnd(node, idx);
                          const sel = window.getSelection();
                          if (sel) {
                            sel.removeAllRanges();
                            sel.addRange(range);
                          }
                          return true;
                        }
                        node = walker.nextNode();
                      }
                      return false;
                    }
                    """,
                    marker,
                )
            )
            if selected:
                # Draft.js must observe a native selection event before its
                # toolbar acts; a DOM range alone can leave the previous image selected.
                page.keyboard.press("ArrowRight")
                page.keyboard.press("ArrowLeft")
            return selected
        except Exception:
            return False

    @staticmethod
    def _move_cursor_to_end(page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  const textarea = document.querySelector("textarea");
                  if (textarea && typeof textarea.value === "string") {
                    const len = textarea.value.length;
                    textarea.focus();
                    textarea.setSelectionRange(len, len);
                    return;
                  }
                  const editable = document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                  if (!editable) return;
                  editable.focus();
                  const range = document.createRange();
                  range.selectNodeContents(editable);
                  range.collapse(false);
                  const sel = window.getSelection();
                  if (sel) {
                    sel.removeAllRanges();
                    sel.addRange(range);
                  }
                }
                """
            )
        except Exception:
            return

    @staticmethod
    def _select_marker_text(page, marker: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (target) => {
                      const textareas = Array.from(document.querySelectorAll("textarea"));
                      for (const ta of textareas) {
                        const value = ta && typeof ta.value === "string" ? ta.value : "";
                        const idx = value.indexOf(target);
                        if (idx >= 0) {
                          ta.focus();
                          ta.setSelectionRange(idx, idx + target.length);
                          return true;
                        }
                      }
                      const root =
                        document.querySelector("div.public-DraftEditor-content") ||
                        document.querySelector("[data-contents='true']") ||
                        document.querySelector("main") ||
                        document.body;
                      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                      let node = walker.nextNode();
                      while (node) {
                        const value = node.nodeValue || "";
                        const idx = value.indexOf(target);
                        if (idx >= 0) {
                          const editable = node.parentElement && node.parentElement.closest("[contenteditable='true']");
                          if (editable) editable.focus();
                          const range = document.createRange();
                          range.setStart(node, idx);
                          range.setEnd(node, idx + target.length);
                          const sel = window.getSelection();
                          if (sel) {
                            sel.removeAllRanges();
                            sel.addRange(range);
                          }
                          return true;
                        }
                        node = walker.nextNode();
                      }
                      return false;
                    }
                    """,
                    marker,
                )
            )
        except Exception:
            return False

    def _remove_marker_text(self, page, marker: str) -> bool:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if marker not in self._read_editor_text(page):
                return True
            if not self._select_marker_text(page, marker):
                page.wait_for_timeout(250)
                continue
            try:
                page.keyboard.press("Delete")
                page.wait_for_timeout(450)
            except Exception:
                page.wait_for_timeout(250)
        return marker not in self._read_editor_text(page)

    @staticmethod
    def _assert_body_written(page, min_chars: int = 20) -> None:
        length = 0
        import_dialog_open = False
        try:
            metrics = page.evaluate(
                """
                () => {
                  const dialogs = Array.from(document.querySelectorAll("[role='dialog'], .Modal-wrapper, .Modal"));
                  const importDialogOpen = dialogs.some((node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (!style || style.display === "none" || style.visibility === "hidden") {
                      return false;
                    }
                    const text = (node.innerText || node.textContent || "").trim();
                    return text.includes("选择文件");
                  });
                  const textarea = document.querySelector("textarea[placeholder*='请输入正文'], textarea[placeholder*='正文']");
                  if (textarea && 'value' in textarea) {
                    return { length: (textarea.value || '').trim().length, importDialogOpen };
                  }
                  const slateNodes = Array.from(document.querySelectorAll("[data-contents='true'] [data-text='true']"));
                  if (slateNodes.length > 0) {
                    const text = slateNodes.map((node) => node.textContent || "").join("\\n");
                    return { length: text.trim().length, importDialogOpen };
                  }
                  const editable = document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                  if (editable) {
                    return { length: (editable.innerText || '').trim().length, importDialogOpen };
                  }
                  return { length: 0, importDialogOpen };
                }
                """
            )
            if isinstance(metrics, dict):
                length = int(metrics.get("length", 0) or 0)
                import_dialog_open = bool(metrics.get("importDialogOpen", False))
            else:
                length = int(metrics or 0)
        except Exception:
            length = 0
            import_dialog_open = False
        if int(length) < min_chars:
            if import_dialog_open:
                raise RuntimeError("正文导入未完成：检测到“选择文件”弹窗仍未关闭。")
            hints = ""
            try:
                hints = page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
            except Exception:
                hints = ""
            for token in ("文件加载失败", "素材文件加载失败", "导入失败", "上传失败"):
                if isinstance(hints, str) and token in hints:
                    raise RuntimeError(f"正文导入失败：知乎提示“{token}”。")
            raise RuntimeError(f"正文写入校验失败，当前正文长度仅 {length} 字符。")
    def _assert_draft_rendered(self, page, source_body: str, fill_mode: str) -> None:
        serialized_rendering_confirmed = self._wait_for_math_rendering(page)
        page.wait_for_timeout(1200)
        metrics = self._read_editor_metrics(page)
        editor_text = str(metrics.get("text", "")).strip()
        if not editor_text:
            raise RuntimeError("知乎草稿格式校验失败：未读取到正文内容。")

        if self._looks_like_raw_markdown(editor_text):
            preview = editor_text.replace("\n", " ")[:120]
            raise RuntimeError(f"知乎草稿仍显示 Markdown 源码（mode={fill_mode}，preview={preview}），已中止发布。")

        if re.search(r"(?:<img\b|&lt;img\b|equation\?tex=)", editor_text, flags=re.IGNORECASE):
            raise RuntimeError("知乎草稿含有未解析的公式 HTML，已中止保存。")

        expected_formulas = self._count_latex_formulas(source_body)
        actual_formulas = int(metrics.get("formulaCount", 0) or 0)
        malformed_formulas = int(metrics.get("malformedFormulaCount", 0) or 0)
        unrendered_formulas = int(metrics.get("unrenderedFormulaCount", 0) or 0)
        if malformed_formulas:
            raise RuntimeError(f"知乎草稿存在 {malformed_formulas} 个内容污染的公式节点，已中止保存。")
        if unrendered_formulas and serialized_rendering_confirmed:
            logger.info(
                "Zhihu editor metrics lagged serialized DOM; serialized render proof covers %s nodes.",
                unrendered_formulas,
            )
            unrendered_formulas = 0
        if unrendered_formulas:
            raise RuntimeError(f"知乎草稿有 {unrendered_formulas} 个公式未生成可视图形，仍显示 LaTeX 占位文本，已中止保存。")
        if actual_formulas != expected_formulas:
            raise RuntimeError(
                f"知乎公式转换不完整：源文 {expected_formulas} 个公式，草稿仅识别 {actual_formulas} 个。"
            )

        self._assert_source_content(metrics, source_body)

        structure_checks = (
            (bool(re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", source_body)), "headingCount", "标题"),
            (bool(re.search(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+\S", source_body)), "listCount", "列表"),
            (bool(re.search(r"(?m)^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$", source_body)), "tableCount", "表格"),
        )
        for expected, metric_key, label in structure_checks:
            if expected and int(metrics.get(metric_key, 0) or 0) == 0:
                raise RuntimeError(f"知乎草稿格式校验失败：Markdown {label}未转换为富文本结构。")

    @classmethod
    def _assert_source_content(cls, metrics: dict, source_body: str) -> None:
        converted, expected_formulas = cls._convert_latex_formulas(source_body)
        source_text = cls._rich_html_to_plain_text(cls._markdown_to_rich_html(converted))
        normalize = lambda value: re.sub(r"\s+|\u200b|\ufeff", "", unicodedata.normalize("NFKC", value))
        actual_text = normalize(str(metrics.get("proseText", "")))
        expected_parts = [normalize(line) for line in source_text.splitlines() if normalize(line)]
        position = 0
        for part in expected_parts:
            found = actual_text.find(part, position)
            if found < 0:
                raise RuntimeError(f"知乎草稿正文缺失或顺序不一致，已中止发布：{part[:80]}")
            position = found + len(part)
        actual_formulas = metrics.get("formulaTexts", [])
        normalize_formula = lambda value: re.sub(r"\s+", "", html.unescape(value))
        if [normalize_formula(value) for value in actual_formulas] != [normalize_formula(value) for value in expected_formulas]:
            raise RuntimeError("知乎草稿公式内容或顺序与已核验正文不一致，已中止发布。")

    def _validate_saved_draft(self, page, draft_url: str, title: str, source_body: str, expected_images: int, *, verify_settings: bool = True) -> None:
        for attempt in range(2):
            page.goto(draft_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            self._check_access_block(page)
            self._dismiss_creator_assistant(page)
            try:
                self._assert_draft_rendered(page, source_body, "reloaded_draft")
                self._assert_images_rendered(page, expected_images)
                break
            except RuntimeError as exc:
                if attempt or not any(token in str(exc) for token in ("未生成可视图形", "未完成加载")):
                    raise
                logger.warning("Reloading saved draft after transient media loading failure: %s", exc)
        actual_title = page.locator("textarea[placeholder*='标题'], input[placeholder*='标题']").first.input_value()
        expected_title = re.sub(r"\s+", " ", title).strip()[:100]
        if actual_title != expected_title:
            raise RuntimeError("知乎草稿标题与已核验文章不一致，已中止发布。")
        if verify_settings:
            self._assert_publication_settings(page)
        self._save_draft_validation_snapshot(page, draft_url, source_body)

    @staticmethod
    def _wait_for_math_rendering(page) -> bool:
        """Drive Zhihu's lazy MathJax renderer before validating the draft.

        A long imported article can contain dozens of equations. Zhihu only
        typesets equations as they enter the viewport, so a single pass can
        observe a transient placeholder and incorrectly reject an otherwise
        valid draft. Revisit every formula for a bounded number of rounds and
        inspect the same metrics used by the quality gate after each round.
        """
        # Long imported drafts can leave a few MathJax nodes in their
        # placeholder state for more than one editor repaint. Give the
        # browser a full bounded settle window before rejecting the draft;
        # otherwise a transient render lag consumes the only unattended
        # publication attempt for the day.
        deadline = time.time() + 90
        last_state: tuple[int, int] | None = None
        while time.time() < deadline:
            formulas = page.locator("span[data-tex]")
            formula_count = formulas.count()
            if formula_count == 0:
                return False

            # The editor observes viewport changes asynchronously. Scrolling
            # all nodes in one tight Playwright loop can leave the last few
            # formulas in a placeholder state, so give each node a short
            # render turn and explicitly request a MathJax typeset pass when
            # the page exposes the standard API.
            try:
                page.evaluate(
                    """async () => {
                      const nodes = Array.from(document.querySelectorAll('span[data-tex]'));
                      for (const node of nodes) {
                        node.scrollIntoView({block: 'center', inline: 'nearest'});
                        node.dispatchEvent(new Event('scroll', {bubbles: true}));
                        await new Promise(resolve => setTimeout(resolve, 80));
                      }
                      if (window.MathJax && typeof window.MathJax.typesetPromise === 'function') {
                        try { await window.MathJax.typesetPromise(nodes); } catch (error) {}
                      }
                    }"""
                )
            except Exception:
                pass

            for index in range(formula_count):
                try:
                    # Zhihu typesets equations lazily when they enter the
                    # viewport. Do not wait on each node individually: one
                    # slow equation would otherwise multiply the timeout by
                    # the number of equations in the article.
                    formulas.nth(index).scroll_into_view_if_needed(timeout=1_500)
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

            page.wait_for_timeout(800)
            try:
                metrics = ZhihuService._read_editor_metrics(page)
                rendered_count = int(metrics.get("formulaCount", 0) or 0)
                unrendered_count = int(metrics.get("unrenderedFormulaCount", 0) or 0)
                state = (rendered_count, unrendered_count)
                if rendered_count == formula_count and unrendered_count == 0:
                    return True
                if state != last_state:
                    logger.info(
                        "Waiting for Zhihu formula rendering: rendered=%s/%s, unrendered=%s",
                        rendered_count,
                        formula_count,
                        unrendered_count,
                    )
                last_state = state
            except Exception:
                # The editor may briefly remount while MathJax updates. The
                # next round will reacquire the current formula nodes.
                pass
            page.wait_for_timeout(500)

        try:
            # Serializing the live DOM forces Playwright to observe the final
            # MathJax subtree after its asynchronous replacement.
            page.content()
            page.wait_for_timeout(2_500)
            serialized_count, serialized_unrendered = _FormulaRenderSnapshotParser.state(page.content())
            if serialized_count > 0 and serialized_unrendered == 0:
                logger.info("Zhihu serialized editor DOM confirms %s rendered formulas.", serialized_count)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _read_editor_metrics(page) -> dict:
        try:
            metrics = page.evaluate(
                """
                () => {
                  const root = document.querySelector("div.public-DraftEditor-content")
                    || document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                  if (!root) {
                    const textarea = document.querySelector("textarea[placeholder*='正文']");
                    return {
                      text: textarea && 'value' in textarea ? (textarea.value || '') : '',
                      formulaCount: 0,
                      malformedFormulaCount: 0,
                      headingCount: 0,
                      listCount: 0,
                      tableCount: 0,
                      imageCount: 0,
                      invalidImageCount: 0,
                    };
                  }
                  const formulas = Array.from(
                    root.querySelectorAll("span[data-tex], img[src*='equation?tex='], img[data-eeimg='1'], img[eeimg='1']")
                  );
                  const formulaPayloads = formulas.map((node) =>
                    node.getAttribute("data-tex")
                    || node.getAttribute("alt")
                    || node.getAttribute("src")
                    || ''
                  );
                  const prose = root.cloneNode(true);
                  prose.querySelectorAll("span[data-tex], img, script, style").forEach(node => node.remove());
                  const malformedFormulaCount = formulaPayloads.filter((value) =>
                    /(?:<img|&lt;img|equation\\?tex=.*equation\\?tex=)/i.test(value)
                  ).length;
                  const unrenderedFormulaCount = formulas.filter(node => {
                    if (node.tagName === 'IMG') return !node.complete || node.naturalWidth === 0;
                    return !node.querySelector('svg, math, mjx-container, .katex')
                      || !!node.querySelector('.MathJax_Error, .merror, merror, .MJXp-merror, .katex-error, [data-mjx-error]')
                      || Array.from(node.querySelectorAll('mtext[mathcolor="red"]')).some(n => /^\\\\[A-Za-z]/.test(n.textContent || ''));
                  }).length;
                  const images = Array.from(root.querySelectorAll("img")).filter((img) => {
                    const src = img.getAttribute("src") || "";
                    return !img.hasAttribute("data-eeimg")
                      && !img.hasAttribute("eeimg")
                      && !/equation\\?tex=/i.test(src);
                  });
                  const invalidImageCount = images.filter((img) => {
                    const src = img.getAttribute("src") || "";
                    return !src || src.startsWith("blob:") || img.getAttribute("data-state") === "loading" || !img.complete || img.naturalWidth === 0;
                  }).length;
                  return {
                    text: root.innerText || root.textContent || '',
                    proseText: prose.textContent || '',
                    formulaTexts: formulas.map(node => node.getAttribute('data-tex') || node.getAttribute('alt') || ''),
                    formulaCount: formulas.length,
                    malformedFormulaCount,
                    unrenderedFormulaCount,
                    headingCount: root.querySelectorAll("h1, h2, h3, h4, h5, h6").length,
                    listCount: root.querySelectorAll("ul, ol").length,
                    tableCount: root.querySelectorAll("table").length,
                    imageCount: images.length,
                    invalidImageCount,
                  };
                }
                """
            )
            return metrics if isinstance(metrics, dict) else {}
        except Exception:
            return {}

    @classmethod
    def _read_editor_text(cls, page) -> str:
        return str(cls._read_editor_metrics(page).get("text", ""))

    def _assert_images_rendered(self, page, expected_images: int) -> None:
        self._wait_for_image_loading(page)
        metrics = self._read_editor_metrics(page)
        editor_text = str(metrics.get("text", ""))
        if "[[AUTOSCI_IMG_" in editor_text:
            raise RuntimeError("知乎草稿仍含图片占位标记，已中止保存。")
        actual_images = int(metrics.get("imageCount", 0) or 0)
        invalid_images = int(metrics.get("invalidImageCount", 0) or 0)
        if invalid_images:
            raise RuntimeError(f"知乎草稿存在 {invalid_images} 张未完成加载的图片，已中止保存。")
        if actual_images != expected_images:
            raise RuntimeError(
                f"知乎图片数量不一致：计划保留 {expected_images} 张，正文识别到 {actual_images} 张。"
            )

    @staticmethod
    def _wait_for_image_loading(page) -> None:
        images = page.locator("[contenteditable] img:not([data-eeimg]):not([eeimg])")
        for index in range(images.count()):
            image = images.nth(index)
            try:
                image.scroll_into_view_if_needed(timeout=5000)
                deadline = time.time() + 12
                while time.time() < deadline:
                    ready = image.evaluate("img => img.complete && img.naturalWidth > 0 && img.getAttribute('data-state') !== 'loading'")
                    if ready:
                        break
                    page.wait_for_timeout(300)
            except PlaywrightTimeoutError:
                continue
        page.wait_for_timeout(600)

    @staticmethod
    def _looks_like_markdown_body(text: str) -> bool:
        checks = 0
        patterns = (
            r"(?m)^\s{0,3}#{1,6}\s+\S",
            r"(?m)^\s{0,3}>+\s+\S",
            r"!\[[^\]]*\]\([^)]+\)",
            r"(?m)^\s*\|.+\|\s*$",
            r"(?m)^\s*```",
            r"\$\$[\s\S]{1,1200}?\$\$",
        )
        for pattern in patterns:
            if re.search(pattern, text):
                checks += 1
        return checks >= 1

    @staticmethod
    def _looks_like_raw_markdown(text: str) -> bool:
        hits = 0
        patterns = (
            r"(?m)^\s{0,3}#{1,6}\s+\S",
            r"(?m)^\s{0,3}>+\s+\S",
            r"(?m)^\s*```",
            r"!\[[^\]]*\]\([^)]+\)",
            r"\$\$[\s\S]{1,1200}?\$\$",
            r"(?m)^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$",
        )
        for pattern in patterns:
            if re.search(pattern, text):
                hits += 1
        return hits >= 1

    def _split_body_and_images(self, body: str) -> tuple[str, list[tuple[str, Path]]]:
        image_slots: list[tuple[str, Path]] = []

        def push_slot(file_path: Path) -> str:
            marker = f"[[AUTOSCI_IMG_{len(image_slots) + 1:03d}]]"
            image_slots.append((marker, file_path))
            return marker

        def replace_image(match: re.Match) -> str:
            url = (match.group(2) or "").strip()
            file_path = self._resolve_local_image_path(url)
            if file_path:
                return f"\n{push_slot(file_path)}\n"
            return match.group(0)

        body_text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace_image, body)

        def replace_html_img(match: re.Match) -> str:
            whole = match.group(0)
            src = (match.group(1) or "").strip()
            file_path = self._resolve_local_image_path(src)
            if file_path:
                return f"\n{push_slot(file_path)}\n"
            return whole

        body_text = re.sub(r"""<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>""", replace_html_img, body_text, flags=re.IGNORECASE)
        return body_text, image_slots

    def _resolve_local_image_path(self, url: str) -> Path | None:
        if not url:
            return None
        normalized = url.strip().strip("'\"").split("?", 1)[0].split("#", 1)[0]
        if not normalized:
            return None
        lower = normalized.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return None
        normalized = normalized.replace("\\", "/")

        backend_root = Path(__file__).resolve().parents[2]
        workspace_root = backend_root.parent
        artifacts_root = Path(self._settings.artifacts_dir).resolve()

        candidates: list[Path] = []
        raw = Path(normalized)
        if raw.is_absolute():
            candidates.append(raw)
        else:
            for base in (Path.cwd(), artifacts_root, artifacts_root.parent, backend_root, workspace_root):
                candidates.append((base / raw).resolve(strict=False))

        if normalized.startswith("/artifacts/"):
            rel = normalized[len("/artifacts/") :].lstrip("/")
            candidates.append((artifacts_root / rel).resolve(strict=False))

        marker_match = re.search(r"(?:^|/)(_md2/.+)$", normalized, flags=re.IGNORECASE)
        if marker_match:
            tail = marker_match.group(1)
            candidates.extend(
                [
                    (workspace_root / tail).resolve(strict=False),
                    (backend_root / tail).resolve(strict=False),
                ]
            )
            basename = Path(tail).name
            for folder in (
                workspace_root / "_md2" / "input",
                backend_root / "_md2" / "input",
            ):
                candidates.append((folder / basename).resolve(strict=False))

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists() and candidate.is_file():
                return candidate
        return None
    @staticmethod
    def _track_draft_saves(page) -> dict[str, list[dict]]:
        tracker: dict[str, list[dict]] = {"events": []}

        def extract_article_id(value) -> str | None:
            if isinstance(value, dict):
                preferred_keys = ("id", "article_id", "articleId", "draft_id", "draftId")
                for key in preferred_keys:
                    candidate = value.get(key)
                    if isinstance(candidate, (int, str)) and str(candidate).isdigit() and len(str(candidate)) >= 10:
                        return str(candidate)
                for nested in value.values():
                    found = extract_article_id(nested)
                    if found:
                        return found
            elif isinstance(value, list):
                for nested in value[:20]:
                    found = extract_article_id(nested)
                    if found:
                        return found
            return None

        def on_response(response) -> None:
            try:
                request = response.request
                method = (request.method or "").upper()
                url = response.url or ""
                url_lower = url.lower()
                if method not in {"POST", "PUT", "PATCH"}:
                    return
                if "draft" not in url_lower and "article" not in url_lower:
                    return

                post_data = request.post_data or ""
                article_id = None
                try:
                    article_id = extract_article_id(response.json())
                except Exception:
                    pass
                if not article_id:
                    match = re.search(r"/(?:p|articles?)/(\d{10,})", url)
                    if match:
                        article_id = match.group(1)
                if not article_id:
                    try:
                        headers = response.headers
                        for name in ("location", "x-article-id", "x-draft-id"):
                            value = str(headers.get(name, ""))
                            match = re.search(r"/(?:p|articles?)/(\d{10,})", value) or re.search(r"(?:draft|article)[_-]?(\d{10,})", value, re.I)
                            if match:
                                article_id = match.group(1)
                                break
                    except Exception:
                        pass
                tracker["events"].append(
                    {
                        "status": int(response.status),
                        "url": url,
                        "article_id": article_id,
                        "post_data_length": len(post_data),
                        "contains_content": bool(re.search(r"(?:content|delta|body)", post_data, flags=re.IGNORECASE)),
                    }
                )
            except Exception:
                return

        page.on("response", on_response)
        return tracker

    def _wait_for_draft_saved(self, page, tracker: dict[str, list[dict]], source_body: str) -> str:
        # Keep save responses observed before this helper was called. A newly
        # created draft may already have been persisted while the publish panel
        # was mounting; clearing the tracker would turn that valid save into a
        # false 45-second timeout. Only nudge the editor when no save evidence
        # exists at all.
        events = tracker.setdefault("events", [])
        if not events:
            self._move_cursor_to_end(page)
            try:
                page.keyboard.insert_text(" ")
                page.keyboard.press("Backspace")
            except Exception:
                pass

        deadline = time.time() + 45
        last_status = ""
        while time.time() < deadline:
            self._check_access_block(page)
            try:
                status_nodes = page.locator(".DraftStatusTip")
                if status_nodes.count() > 0:
                    last_status = (status_nodes.first.inner_text(timeout=1_000) or "").strip()
            except Exception:
                last_status = ""

            save_events = [
                event
                for event in tracker.get("events", [])
                if 200 <= int(event.get("status", 0) or 0) < 300
                and (
                    bool(event.get("contains_content"))
                    or int(event.get("post_data_length", 0) or 0) >= 160
                )
            ]
            saved_status = "草稿" in last_status and not any(token in last_status for token in ("保存中", "正在保存"))
            if saved_status:
                latest = save_events[-1] if save_events else None
                article_id = latest.get("article_id") if latest else None
                draft_url = f"https://zhuanlan.zhihu.com/p/{article_id}/edit" if article_id else self._resolve_draft_url(page)
                # Existing edit pages can be verified by the subsequent reload
                # even if this run did not emit a fresh network response.
                has_persisted_identity = self._is_edit_url(draft_url) and (
                    bool(save_events) or self._is_edit_url(page.url)
                )
                if not has_persisted_identity:
                    page.wait_for_timeout(500)
                    continue
                self._save_draft_validation_snapshot(page, draft_url, source_body)
                return draft_url
            page.wait_for_timeout(500)

        event_summary = [
            {
                "status": event.get("status"),
                "url": str(event.get("url", ""))[:180],
                "payload": event.get("post_data_length", 0),
            }
            for event in tracker.get("events", [])[-5:]
        ]
        raise RuntimeError(
            f"知乎草稿保存校验超时：状态={last_status or '未识别'}，保存事件={event_summary}"
        )

    def _save_draft_validation_snapshot(self, page, draft_url: str, source_body: str) -> None:
        debug_dir = Path(self._settings.artifacts_dir) / "zhihu_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        base = debug_dir / f"zhihu_draft_verified_{ts}"
        metrics = self._read_editor_metrics(page)
        metrics.update(
            {
                "draft_url": draft_url,
                "expected_formula_count": self._count_latex_formulas(source_body),
            }
        )
        try:
            (base.with_suffix(".json")).write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            return

    def _resolve_draft_url(self, page) -> str:
        current = page.url
        if self._is_edit_url(current):
            return current

        match = re.search(r"/p/(\d+)", current)
        if match:
            return f"https://zhuanlan.zhihu.com/p/{match.group(1)}/edit"

        # Never select an arbitrary article link from the writer page. The
        # page may contain an older article, and using its ID here can update
        # the wrong draft on retry. A new draft must expose its own ID in the
        # save response or redirect to an edit URL.
        return current

    @staticmethod
    def _is_edit_url(url: str | None) -> bool:
        return bool(url and "/edit" in url)

    def _verify_published_url(self, page, expected_title: str = "", *, source_body: str = "", expected_images: int = 0) -> str:
        current = page.url
        if "zhuanlan.zhihu.com/p/" not in current:
            raise RuntimeError(f"发布结果校验失败，未进入文章相关页面，当前URL: {current}")

        match = re.search(r"/p/(\d+)", current)
        if not match:
            raise RuntimeError(f"发布结果校验失败，未识别文章ID，当前URL: {current}")

        article_id = match.group(1)
        public_url = f"https://zhuanlan.zhihu.com/p/{article_id}"
        if self._load_zhihu_state().get("public_readback_mode") == "published_api":
            return self._verify_published_api(page, article_id, expected_title, source_body, expected_images)
        response = page.goto(public_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        if response is not None and response.status in {403, 429} and source_body:
            # Reading the public HTML is a different operation from publishing.
            # Only a fully verified, owner-authenticated published record can
            # distinguish this restriction from an uncertain submission.
            return self._verify_published_api(page, article_id, expected_title, source_body, expected_images)
        final_url = page.url
        self._check_access_block(page)
        self._check_publish_rate_limit(page, stage="发布结果校验")
        state = page.evaluate("""() => {
            const article = document.querySelector('.Post-RichTextContainer, .Post-RichText, article .RichText');
            const heading = document.querySelector('h1.Post-Title, h1');
            return {title: heading ? heading.innerText : '',
                    text: article ? article.innerText : '',
                    pageText: document.body ? document.body.innerText : '',
                    hasEditor: !!document.querySelector('.WriteIndex [contenteditable=true]')};
        }""")
        self._assert_public_article_state(state, expected_title)
        if response is not None and response.status >= 400:
            raise RuntimeError(f"知乎公开页面返回 HTTP {response.status}，未确认发布成功。")
        if "zhuanlan.zhihu.com/p/" not in final_url or "/edit" in final_url:
            raise RuntimeError(f"发布后仍停留在编辑页或未进入公开页，当前URL: {final_url}")
        if source_body:
            article = page.locator(".Post-RichTextContainer, .Post-RichText, article .RichText").first
            media = article.locator("span[data-tex], img")
            for index in range(media.count()):
                media.nth(index).scroll_into_view_if_needed()
            page.wait_for_function("""() => {
                const root = document.querySelector('.Post-RichTextContainer, .Post-RichText, article .RichText');
                return root && Array.from(root.querySelectorAll('img')).every(n => n.complete && n.naturalWidth > 0)
                    && Array.from(root.querySelectorAll('span[data-tex]')).every(n => n.querySelector('svg, math, mjx-container, .katex'));
            }""", timeout=25000)
            metrics = article.evaluate("""root => {
                const formulas = Array.from(root.querySelectorAll('span[data-tex], img[data-eeimg], img[eeimg], img[src*="equation?tex="]'));
                const images = Array.from(root.querySelectorAll('img')).filter(n => !formulas.includes(n));
                const prose = root.cloneNode(true);
                prose.querySelectorAll('span[data-tex], img').forEach(n => n.remove());
                return {proseText: prose.textContent,
                    formulaTexts: formulas.map(n => n.getAttribute('data-tex') || n.getAttribute('alt') || ''),
                    imageCount: images.length,
                    unrenderedFormulaCount: formulas.filter(n => n.tagName === 'IMG' ? !n.complete || !n.naturalWidth : !n.querySelector('svg, math, mjx-container, .katex') || n.querySelector('.MathJax_Error, .merror, merror, .MJXp-merror, .katex-error, [data-mjx-error]') || Array.from(n.querySelectorAll('mtext[mathcolor="red"]')).some(x => /^\\\\[A-Za-z]/.test(x.textContent || ''))).length,
                    invalidImageCount: images.filter(n => !n.complete || !n.naturalWidth).length};
            }""")
            metrics["topics"] = page.locator("a[href*='/topic/']").all_text_contents()
            metrics["columns"] = page.locator("a").all_text_contents()
            self._assert_public_content(metrics, source_body, expected_images)
        return final_url

    @staticmethod
    def _assert_published_api_identity(payload: dict, article_id: str, expected_title: str) -> None:
        if not isinstance(payload, dict) or str(payload.get("id")) != article_id or payload.get("state") != "published":
            raise RuntimeError("官方文章记录未证明目标文章已经公开发布，禁止推定成功。")
        normalize = lambda value: re.sub(r"\s+", "", str(value or ""))
        if normalize(payload.get("title")) != normalize(expected_title[:100]) or not payload.get("content"):
            raise RuntimeError("官方已发表记录的标题或正文与目标文章不一致。")

    def _verify_published_api(self, page, article_id: str, expected_title: str, source_body: str, expected_images: int) -> str:
        if not source_body:
            raise RuntimeError("缺少已核验原文，无法通过文章接口验收。")
        response = page.context.request.get(f"https://zhuanlan.zhihu.com/api/articles/{article_id}", timeout=30000)
        if response.status != 200:
            raise RuntimeError(f"官方已发表记录返回 HTTP {response.status}，仍不能确认发布验收通过。")
        payload = response.json()
        self._assert_published_api_identity(payload, article_id, expected_title)
        metrics = page.evaluate("""content => {
            const root = new DOMParser().parseFromString(content, 'text/html').body;
            const formulas = Array.from(root.querySelectorAll('span[data-tex], img[data-eeimg], img[eeimg], img[src*=\"equation?tex=\"]'));
            const images = Array.from(root.querySelectorAll('img')).filter(n => !formulas.includes(n));
            const prose = root.cloneNode(true);
            prose.querySelectorAll('span[data-tex], img, script, style').forEach(n => n.remove());
            return {proseText: prose.textContent,
                formulaTexts: formulas.map(n => n.getAttribute('data-tex') || n.getAttribute('alt') || ''),
                imageCount: images.length,
                imageIdentities: images.map(n => n.getAttribute('data-original-token') || (n.src.match(/v2-[a-f0-9]{32}/) || [n.src])[0]),
                imageSources: images.map(n => new URL(n.getAttribute('src'), 'https://zhuanlan.zhihu.com').href)};
        }""", payload["content"])
        self._assert_source_content(metrics, source_body)
        if metrics.get("imageCount") != expected_images:
            raise RuntimeError("官方已发表正文的图片数量与已核验原文不一致。")
        metrics["topics"] = [item.get("name", "") for item in payload.get("topics", []) if isinstance(item, dict)]
        column = payload.get("column") or {}
        metrics["columns"] = [column.get("title") or column.get("name") or ""] if isinstance(column, dict) else []
        draft_url = f"https://zhuanlan.zhihu.com/p/{article_id}/edit"
        # Published editors no longer expose all pre-submit column controls;
        # their actual topics/column are checked against the official record.
        self._validate_saved_draft(page, draft_url, expected_title, source_body, expected_images, verify_settings=False)
        rendered = self._read_editor_metrics(page)
        editor_identities = page.locator('[contenteditable=true] img').evaluate_all("""nodes => nodes
            .filter(n => !n.hasAttribute('eeimg') && !n.hasAttribute('data-eeimg') && !/equation\\?tex=/.test(n.src))
            .map(n => n.getAttribute('data-original-token') || (n.src.match(/v2-[a-f0-9]{32}/) || [n.src])[0])""")
        if metrics["imageIdentities"] != editor_identities:
            raise RuntimeError("官方已发表正文与回读编辑器的图片内容或顺序不一致。")
        for source in metrics["imageSources"]:
            self._assert_published_image_loads(page.context.request, source)
        metrics["invalidImageCount"] = rendered.get("invalidImageCount", 1)
        metrics["unrenderedFormulaCount"] = rendered.get("unrenderedFormulaCount", 1)
        if metrics["unrenderedFormulaCount"]:
            serialized_count, serialized_unrendered = _FormulaRenderSnapshotParser.state(page.content())
            if serialized_count == self._count_latex_formulas(source_body) and serialized_unrendered == 0:
                logger.info("Zhihu published owner DOM confirms %s rendered formulas.", serialized_count)
                metrics["unrenderedFormulaCount"] = 0
        self._assert_public_content(metrics, source_body, expected_images)
        state = self._load_zhihu_state()
        state["public_readback_mode"] = "published_api"
        state["public_readback_reason"] = "公开展示页读取受限；已通过官方已发表记录与编辑器渲染核验。"
        state["public_readback_verified_at"] = datetime.now(timezone.utc).isoformat()
        if not self._save_zhihu_state(state):
            raise RuntimeError("无法保存官方已发表记录的核验结果。")
        logger.info("Verified published article %s through official record and rendered owner view.", article_id)
        return f"https://zhuanlan.zhihu.com/p/{article_id}"

    @staticmethod
    def _assert_published_image_loads(request, source: str) -> None:
        from urllib.parse import urlsplit
        import fitz

        parsed = urlsplit(source)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".zhimg.com"):
            raise RuntimeError("官方已发表图片不是已知的知乎上传资源。")
        response = request.get(source, timeout=30000)
        if response.status != 200 or not response.headers.get("content-type", "").startswith("image/"):
            raise RuntimeError("官方已发表图片未完成加载。")
        pixmap = fitz.Pixmap(response.body())
        if not pixmap.width or not pixmap.height:
            raise RuntimeError("官方已发表图片无法解码。")

    def _assert_public_content(self, metrics: dict, source_body: str, expected_images: int) -> None:
        self._assert_source_content(metrics, source_body)
        if metrics.get("imageCount") != expected_images or metrics.get("invalidImageCount") or metrics.get("unrenderedFormulaCount"):
            raise RuntimeError("知乎公开文章的公式或图片不完整，未确认发布验收通过。")
        tags = {tag.strip() for tag in self._settings.zhihu_default_tags.split(",") if tag.strip()}
        if not tags.issubset({value.strip() for value in metrics.get("topics", [])}):
            raise RuntimeError("知乎公开文章未显示全部配置话题。")
        column = self._settings.zhihu_column_name.strip()
        if column and column not in {value.strip() for value in metrics.get("columns", [])}:
            raise RuntimeError("知乎公开文章未显示配置专栏。")

    @staticmethod
    def _assert_public_article_state(state: dict, expected_title: str = "") -> None:
        text = str(state.get("text") or "").strip()
        title = str(state.get("title") or "").strip()
        page_text = str(state.get("pageText") or "")
        if state.get("hasEditor") or len(text) < 100 or not title:
            reason = "知乎返回空白/不存在的文章页面" if "没有知识存在的荒原" in page_text else "未找到公开文章正文"
            raise RuntimeError(f"知乎发布结果校验失败：{reason}，草稿地址已保留。")
        normalize = lambda value: re.sub(r"[\s\u200b]", "", value)
        if expected_title and normalize(title) != normalize(expected_title[:100]):
            raise RuntimeError("知乎发布结果校验失败：公开文章标题与待发布文章不一致。")

    def _assert_publish_response(self, response) -> None:
        if response.status >= 500:
            raise RuntimeError(f"知乎发布接口返回 HTTP {response.status}，请求是否生效不确定。")
        try:
            payload = response.json()
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if response.status < 400 and not error:
            return
        message = str(error.get("message") or error) if isinstance(error, dict) else str(error or f"HTTP {response.status}")
        if response.status == 429 or error_code == "40362" or any(hint in message for hint in self.RATE_LIMIT_HINTS):
            until = self._mark_rate_limited(message)
            raise RuntimeError(f"知乎发布熔断：平台返回限流或异常访问，请人工检查后再解除，参考冷却至 {self._format_dt(until)}。")
        raise RuntimeError(f"知乎拒绝发布：{message}")

    @classmethod
    def _convert_latex_formulas(cls, markdown_text: str) -> tuple[str, list[str]]:
        formulas: list[str] = []

        def formula_tag(formula: str) -> str:
            normalized = formula.strip()
            normalized = re.sub(r"\\(?:textnormal|textsc)(?=\s*\{)", lambda _: r"\text", normalized)
            normalized = re.sub(r"\\(?:label|tag)\*?\{[^{}]*\}", "", normalized).strip()
            # TeX source files sometimes guard an aligned display with a
            # class/layout conditional such as
            # ``\\ifdefined\\ORLtwocolumn ... \\else ... \\fi``. Zhihu's
            # MathJax does not implement these document-level conditionals
            # and renders the whole equation as an error node. The article
            # already contains the mathematical branches, so keep the first
            # branch and remove only the unsupported control flow.
            normalized = re.sub(
                r"\\ifdefined\s*\\[A-Za-z@]+([\s\S]*?)\\else[\s\S]*?\\fi",
                lambda match: match.group(1),
                normalized,
            )
            normalized = re.sub(
                r"\\ifdefined\s*\\[A-Za-z@]+([\s\S]*?)\\fi",
                lambda match: match.group(1),
                normalized,
            )
            for source, target in (("align", "aligned"), ("alignat", "alignedat"), ("gather", "gathered"), ("multline", "gathered"), ("dcases", "cases")):
                normalized = re.sub(r"\\(begin|end)\{" + source + r"\*?\}", lambda m: "\\" + m.group(1) + "{" + target + "}", normalized)
            normalized = re.sub(r"\\(?:begin|end)\{equation\*?\}", "", normalized).strip()
            formulas.append(normalized)
            encoded = quote(normalized, safe="")
            # A newline followed by `=` is Setext-heading syntax in Markdown.
            # Keeping formula newlines inside an HTML attribute can therefore
            # split the image tag before it reaches Zhihu's rich-text editor.
            alt = html.escape(re.sub(r"\s+", " ", normalized), quote=True)
            return (
                f'<img src="https://www.zhihu.com/equation?tex={encoded}" '
                f'alt="{alt}" class="ee_img tr_noresize" data-eeimg="1" eeimg="1">'
            )

        def replace_math(text: str) -> str:
            code_parts = re.split(r"(`+[^`\n]*`+)", text)
            converted: list[str] = []
            for part in code_parts:
                if part.startswith("`") and part.endswith("`"):
                    converted.append(part)
                    continue
                part = escape_currency_dollars(part)
                math_error = malformed_math_reason(part)
                if math_error:
                    raise RuntimeError(f"公式源码损坏，必须重新通过质量核验：{math_error}")
                # A single pass prevents nested TeX environments from converting
                # to HTML before the enclosing dollar expression is handled.
                pattern = (
                    r"(?<!\\)\$\$(?!\$)(?P<display>[\s\S]+?)(?<!\\)\$\$(?!\$)"
                    r"|(?<!\\)\\\[(?P<bracket>[\s\S]*?)(?<!\\)\\\]"
                    r"|(?<!\\)\\\((?P<paren>[\s\S]*?)(?<!\\)\\\)"
                    r"|(?P<environment>\\begin\{(?P<env>equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|split|aligned(?:at)?|gathered|cases|dcases|array)\}[\s\S]*?\\end\{(?P=env)\})"
                    r"|(?<![\\$])\$(?![$\s])(?P<inline>[^\n$]+?)(?<!\\)\$(?!\$)"
                )
                part = re.sub(pattern, lambda m: formula_tag(next(m.group(name) for name in ("display", "bracket", "paren", "environment", "inline") if m.group(name) is not None)), part)
                converted.append(part)
            return "".join(converted)

        output: list[str] = []
        pending: list[str] = []
        fence_char = ""
        for line in markdown_text.splitlines(keepends=True):
            fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if not fence_char and fence_match:
                if pending:
                    output.append(replace_math("".join(pending)))
                    pending = []
                fence_char = fence_match.group(1)[0]
                output.append(line)
                continue
            if fence_char:
                output.append(line)
                if re.match(rf"^\s{{0,3}}{re.escape(fence_char)}{{3,}}\s*$", line):
                    fence_char = ""
                continue
            pending.append(line)

        if pending:
            output.append(replace_math("".join(pending)))
        return "".join(output), formulas

    @classmethod
    def _count_latex_formulas(cls, markdown_text: str) -> int:
        _, formulas = cls._convert_latex_formulas(markdown_text)
        return len(formulas)

    @staticmethod
    def _formula_image_count(markdown_text: str) -> int:
        return len(re.findall(r"<img\b[^>]+www\.zhihu\.com/equation\?tex=", markdown_text, flags=re.IGNORECASE))

    @staticmethod
    def _strip_leading_h1(markdown_text: str) -> str:
        lines = markdown_text.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            if re.match(r"^\s{0,3}#\s+\S", line):
                del lines[index]
                while index < len(lines) and not lines[index].strip():
                    del lines[index]
            break
        return "\n".join(lines).strip()

    def _convert_markdown_for_zhihu(self, markdown_text: str) -> str:
        prepared_text, formulas = self._convert_latex_formulas(markdown_text)
        expected_formulas = len(formulas)
        cmd_text = (self._settings.zhihu_markdown_converter_cmd or "").strip()
        if not cmd_text:
            return prepared_text

        cmd_parts = self._resolve_converter_cmd_parts(cmd_text)
        if not cmd_parts:
            return prepared_text

        work_dir = Path(self._settings.artifacts_dir) / "zhihu_convert" / f"run_{int(time.time() * 1000)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        input_md = work_dir / "input.md"
        output_md = work_dir / "output.md"
        input_md.write_text(prepared_text, encoding="utf-8")
        run_env = self._build_converter_env()

        attempts = [
            cmd_parts + [str(input_md), "-o", str(output_md)],
            cmd_parts + ["-i", str(input_md), "-o", str(output_md)],
            cmd_parts + [str(input_md)],
        ]

        for cmd in attempts:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=False, timeout=120, env=run_env)
            except Exception as exc:
                logger.info("Markdown converter invocation failed: %s | cmd=%s", exc, cmd)
                continue

            stdout_text = self._decode_output(proc.stdout)
            stderr_text = self._decode_output(proc.stderr)
            if proc.returncode != 0:
                logger.info(
                    "Markdown converter returned non-zero code %s | cmd=%s | stderr=%s",
                    proc.returncode,
                    cmd,
                    stderr_text[-280:],
                )
                continue

            if output_md.exists():
                text = output_md.read_text(encoding="utf-8", errors="ignore").strip()
                if text and self._formula_image_count(text) >= expected_formulas:
                    return text

            generated = sorted(
                [p for p in work_dir.rglob("*.md") if p.name != "input.md"],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if generated:
                text = generated[0].read_text(encoding="utf-8", errors="ignore").strip()
                if text and self._formula_image_count(text) >= expected_formulas:
                    return text

            if (
                stdout_text
                and len(stdout_text.strip()) > 20
                and self._formula_image_count(stdout_text) >= expected_formulas
            ):
                return stdout_text.strip()

            rewritten = input_md.read_text(encoding="utf-8", errors="ignore").strip()
            if (
                rewritten
                and rewritten != prepared_text.strip()
                and self._formula_image_count(rewritten) >= expected_formulas
            ):
                return rewritten

        return prepared_text

    def _resolve_converter_cmd_parts(self, cmd_text: str) -> list[str]:
        try:
            cmd_parts = shlex.split(cmd_text, posix=False)
        except Exception:
            cmd_parts = [cmd_text]
        if not cmd_parts:
            return []

        executable = cmd_parts[0]
        if shutil.which(executable):
            return cmd_parts

        if executable.lower() == "md2zhihu" and importlib.util.find_spec("md2zhihu") is not None:
            fallback = [sys.executable, "-X", "utf8", "-m", "md2zhihu", *cmd_parts[1:]]
            logger.info("Markdown converter command not found: %s, fallback to `%s`", executable, " ".join(fallback))
            return fallback

        logger.info("Markdown converter command not found: %s", executable)
        return []

    def _build_converter_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env["PATH"] = self._augment_path_for_converter(env.get("PATH", ""))
        return env

    def _augment_path_for_converter(self, current_path: str) -> str:
        parts = [p for p in current_path.split(os.pathsep) if p]

        # Ensure user-level Python scripts (e.g., md2zhihu.exe) are reachable.
        py_scripts = Path(sys.executable).resolve().parent / "Scripts"
        if py_scripts.exists() and str(py_scripts) not in parts:
            parts.insert(0, str(py_scripts))

        pandoc_dir = self._detect_pandoc_dir()
        if pandoc_dir and pandoc_dir not in parts:
            parts.insert(0, pandoc_dir)
        return os.pathsep.join(parts)

    def _detect_pandoc_dir(self) -> str | None:
        current = shutil.which("pandoc")
        if current:
            return str(Path(current).resolve().parent)

        appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        if appdata:
            base = appdata / "Microsoft" / "WinGet" / "Packages"
            if base.exists():
                for exe in sorted(base.glob("JohnMacFarlane.Pandoc_*/*/pandoc.exe"), reverse=True):
                    if exe.exists():
                        return str(exe.parent)
        return None

    @staticmethod
    def _decode_output(payload: bytes | None) -> str:
        if not payload:
            return ""
        for encoding in ("utf-8", "gbk"):
            try:
                return payload.decode(encoding)
            except Exception:
                continue
        return payload.decode("utf-8", errors="ignore")

    def _save_debug_snapshot(self, page) -> str:
        debug_dir = Path(self._settings.artifacts_dir) / "zhihu_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        html_path = debug_dir / f"zhihu_publish_{ts}.html"
        png_path = debug_dir / f"zhihu_publish_{ts}.png"
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            pass
        return str(html_path)

    def _check_access_block(self, page) -> None:
        try:
            content = page.content()
        except Exception:
            return
        if "40362" in content or "暂时限制本次访问" in content:
            until = self._mark_rate_limited(
                "知乎页面返回风控 code 40362：当前请求存在异常，暂时限制本次访问",
                manual_clearance_required=True,
            )
            raise RuntimeError(
                f"知乎发布熔断：检测到访问风控 code 40362；自动化已暂停，请确认官方页面恢复正常后在控制台解除熔断。冷却参考时间 {self._format_dt(until)}。"
            )

    def _ensure_not_rate_limited(self) -> None:
        state = self._load_zhihu_state()
        if state.get("manual_clearance_required"):
            raise RuntimeError(
                "知乎发布熔断中：检测到平台风控或发布限制。自动化不会自动探测重试；请在官方页面确认恢复后从控制台解除熔断。"
            )
        text = str(state.get("rate_limit_until") or "").strip()
        if not text:
            return

        until = self._parse_iso_datetime(text)
        if not until:
            raise RuntimeError(
                "知乎发布熔断中：本地冷却状态无法解析。请在官方页面确认账号状态后手动解除熔断。"
            )

        now = datetime.now(timezone.utc)
        if until <= now:
            state.pop("rate_limit_until", None)
            self._save_zhihu_state(state)
            return

        raise RuntimeError(f"知乎发布冷却中：检测到24小时发布频率限制，请在 {self._format_dt(until)} 后重试。")

    def _interaction_interval_seconds(self) -> int:
        try:
            return max(0, int(getattr(self._settings, "zhihu_min_interaction_interval_seconds", 1800)))
        except (TypeError, ValueError):
            return 1800

    def _ensure_interaction_cooldown(self) -> None:
        interval = self._interaction_interval_seconds()
        if interval <= 0:
            return
        state = self._load_zhihu_state()
        last_text = str(state.get("last_interaction_at") or "").strip()
        if not last_text:
            return
        last = self._parse_iso_datetime(last_text)
        if last is None:
            raise RuntimeError("知乎账号交互状态无法解析，已失败关闭；请检查状态文件后再操作。")
        until = last + timedelta(seconds=interval)
        now = datetime.now(timezone.utc)
        if until > now:
            raise ZhihuPublicationWindowClosed(
                f"知乎账号交互冷却中，请在 {self._format_dt(until)} 后再尝试公开发布。"
            )

    def has_interacted_today(self) -> bool:
        state = self._load_zhihu_state()
        last_text = str(state.get("last_interaction_at") or "").strip()
        if not last_text:
            return False
        last = self._parse_iso_datetime(last_text)
        if last is None:
            raise RuntimeError("知乎账号交互状态无法解析，已失败关闭；请检查状态文件后再操作。")
        try:
            tz = ZoneInfo(self._settings.timezone)
        except Exception:
            tz = timezone.utc
        return last.astimezone(tz).date() == datetime.now(tz).date()

    def _ensure_daily_interaction_capacity(self) -> None:
        """Compatibility hook; cooldown and publish quota are the real gates.

        Older deployments treated any failed browser attempt as a full-day
        account interaction. That stranded the queue after a transient draft
        validation failure. The persisted interaction timestamp remains
        observable, but automatic retries are governed by the configured
        interval and the separate daily publication-attempt ledger.
        """
        return

    def _reserve_interaction(self) -> None:
        state = self._load_zhihu_state()
        state["last_interaction_at"] = datetime.now(timezone.utc).isoformat()
        state["interaction_interval_seconds"] = self._interaction_interval_seconds()
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if not self._save_zhihu_state(state):
            raise RuntimeError("无法持久化知乎账号交互冷却状态，已拒绝启动公开发布。")

    def _publication_date(self) -> str:
        try:
            tz = ZoneInfo(self._settings.timezone)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz).date().isoformat()

    def _publication_attempt_count(self, state: dict | None = None) -> int:
        payload = state if state is not None else self._load_zhihu_state()
        day = self._publication_date()
        legacy_counts = payload.get("public_publication_attempts_by_date", {})
        keys_by_date = payload.get("public_publication_keys_by_date", {})
        try:
            legacy_count = max(0, int(legacy_counts.get(day, 0))) if isinstance(legacy_counts, dict) else 0
        except (TypeError, ValueError):
            legacy_count = 0
        day_keys = keys_by_date.get(day, []) if isinstance(keys_by_date, dict) else []
        key_count = len({str(key) for key in day_keys}) if isinstance(day_keys, list) else 0
        return max(legacy_count, key_count)

    def _ensure_publication_capacity(self) -> None:
        limit = max(0, int(getattr(self._settings, "zhihu_max_publications_per_day", 1)))
        if self.remaining_publication_attempts() <= 0:
            raise ZhihuPublicationWindowClosed(
                f"已达到每日知乎公开发布上限 {limit} 篇，任务保留队列等待后续发布窗口。"
            )

    def remaining_publication_attempts(self) -> int:
        limit = max(0, int(getattr(self._settings, "zhihu_max_publications_per_day", 1)))
        return max(0, limit - self._publication_attempt_count())

    def _reserve_publication_attempt(self, publication_key: str) -> None:
        state = self._load_zhihu_state()
        day = self._publication_date()
        keys_by_date = state.get("public_publication_keys_by_date", {})
        keys_by_date = keys_by_date if isinstance(keys_by_date, dict) else {}
        day_keys = keys_by_date.get(day, [])
        day_keys = [str(key) for key in day_keys] if isinstance(day_keys, list) else []
        limit = max(0, int(getattr(self._settings, "zhihu_max_publications_per_day", 1)))
        if self._publication_attempt_count(state) >= limit:
            raise ZhihuPublicationWindowClosed(
                f"已达到每日知乎公开发布上限 {limit} 篇，任务保留队列等待后续发布窗口。"
            )
        if publication_key not in day_keys:
            day_keys.append(publication_key)
        keys_by_date[day] = day_keys
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
        state["public_publication_keys_by_date"] = {
            key: value for key, value in keys_by_date.items() if key >= cutoff
        }
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if not self._save_zhihu_state(state):
            raise RuntimeError("无法持久化知乎发布额度状态，已拒绝启动公开发布以避免重复提交。")

    def release_publication_attempt(self, publication_key: str) -> None:
        state = self._load_zhihu_state()
        keys_by_date = state.get("public_publication_keys_by_date", {})
        if not isinstance(keys_by_date, dict):
            return
        legacy_counts = state.get("public_publication_attempts_by_date", {})
        legacy_counts = legacy_counts if isinstance(legacy_counts, dict) else {}
        updated = False
        for day, values in list(keys_by_date.items()):
            if not isinstance(values, list) or publication_key not in values:
                continue
            keys_by_date[day] = [key for key in values if key != publication_key]
            if not keys_by_date[day]:
                keys_by_date.pop(day, None)
            updated = True
            try:
                legacy_value = max(0, int(legacy_counts.get(day, 0)))
            except (TypeError, ValueError):
                legacy_value = 0
            if legacy_value:
                legacy_counts[day] = legacy_value - 1
                if legacy_counts[day] <= 0:
                    legacy_counts.pop(day, None)
        if not updated:
            return
        state["public_publication_keys_by_date"] = keys_by_date
        if legacy_counts:
            state["public_publication_attempts_by_date"] = legacy_counts
        else:
            state.pop("public_publication_attempts_by_date", None)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_zhihu_state(state)

    @classmethod
    def _is_definite_publish_response(cls, exc: Exception) -> bool:
        message = str(exc)
        return (
            message.startswith("知乎拒绝发布：")
            or message.startswith("知乎发布频率过高，")
            or message.startswith("知乎发布熔断：平台返回限流")
            or "知乎提示“" in message
            or any(hint in message for hint in cls.RATE_LIMIT_HINTS)
        )

    @classmethod
    def _should_release_publication_reservation(
        cls,
        publish_request_started: bool,
        publish_response_confirmed: bool,
        exc: Exception,
    ) -> bool:
        if not publish_request_started:
            return True
        return not publish_response_confirmed and cls._is_definite_publish_response(exc)

    def _check_publish_rate_limit(self, page, stage: str = "") -> None:
        hit = self._detect_rate_limit_hint(page)
        if not hit:
            return
        until = self._mark_rate_limited(hit, manual_clearance_required=True)
        prefix = f"{stage}：" if stage else ""
        raise RuntimeError(f"{prefix}知乎提示“{hit}”，自动化已熔断；请在官方页面确认恢复后手动解除，参考冷却至 {self._format_dt(until)}。")

    def _detect_rate_limit_hint(self, page) -> str | None:
        for attempt in range(4):
            chunks: list[str] = []
            try:
                text = page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
                if isinstance(text, str) and text:
                    chunks.append(text)
            except Exception:
                pass
            try:
                html = page.content()
                if html:
                    chunks.append(html)
            except Exception:
                pass
            all_text = "\n".join(chunks)
            if all_text:
                for hint in self.RATE_LIMIT_HINTS:
                    if hint in all_text:
                        return hint
            if attempt < 3:
                page.wait_for_timeout(300)
        return None

    def _mark_rate_limited(self, reason: str, *, manual_clearance_required: bool = True) -> datetime:
        now = datetime.now(timezone.utc)
        until = now + timedelta(hours=24)
        state = self._load_zhihu_state()
        state["rate_limit_until"] = until.isoformat()
        state["rate_limit_reason"] = reason
        state["manual_clearance_required"] = manual_clearance_required
        state["updated_at"] = now.isoformat()
        if not self._save_zhihu_state(state):
            raise RuntimeError("无法持久化知乎风控熔断状态，已中止本次知乎操作。")
        return until

    def rate_limit_status(self) -> dict:
        state = self._load_zhihu_state()
        until_text = str(state.get("rate_limit_until") or "").strip()
        until = self._parse_iso_datetime(until_text) if until_text else None
        cooldown_active = bool(until_text and (until is None or until > datetime.now(timezone.utc)))
        return {
            "active": bool(state.get("manual_clearance_required")) or cooldown_active,
            "until": state.get("rate_limit_until"),
            "reason": state.get("rate_limit_reason", ""),
            "session_error": state.get("session_error", ""),
            "session_checked_at": state.get("session_checked_at"),
            "public_readback_mode": state.get("public_readback_mode"),
            "public_readback_reason": state.get("public_readback_reason"),
            "interacted_today": self.has_interacted_today(),
            "interaction_until": (
                self._format_dt(
                    self._parse_iso_datetime(str(state.get("last_interaction_at")))
                    + timedelta(seconds=self._interaction_interval_seconds())
                )
                if self._parse_iso_datetime(str(state.get("last_interaction_at") or ""))
                else None
            ),
        }

    def clear_rate_limit_after_manual_check(self) -> dict:
        state = self._load_zhihu_state()
        state.pop("rate_limit_until", None)
        state.pop("rate_limit_reason", None)
        state.pop("manual_clearance_required", None)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if not self._save_zhihu_state(state):
            raise RuntimeError("无法清除知乎熔断状态；本次未修改状态。")
        return self.rate_limit_status()

    def _state_file(self) -> Path:
        return Path(self._settings.artifacts_dir) / "zhihu_state.json"

    def _load_zhihu_state(self) -> dict:
        path = self._state_file()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
            raise ValueError("知乎状态文件不是对象")
        except Exception:
            logger.exception("读取知乎状态失败；按风控熔断处理。")
            return {
                "manual_clearance_required": True,
                "rate_limit_reason": "知乎状态文件损坏，已失败关闭。请检查状态文件并核验知乎官方页面。",
            }

    def _save_zhihu_state(self, payload: dict) -> bool:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            return True
        except Exception as exc:
            logger.warning("保存知乎状态失败: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _format_dt(self, dt: datetime) -> str:
        try:
            tz = ZoneInfo(self._settings.timezone)
        except Exception:
            tz = timezone.utc
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")

    def _record_session_status(self, valid: bool) -> None:
        state = self._load_zhihu_state()
        state["session_error"] = "" if valid else "知乎登录态已失效，需要恢复服务器登录配置。"
        state["session_checked_at"] = datetime.now(timezone.utc).isoformat()
        self._save_zhihu_state(state)

    def _restore_managed_session(self, context, source_profile: Path) -> None:
        path = source_profile / "autosci-zhihu-cookies.json"
        if not path.exists():
            return
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cookies, list) or any(
            not isinstance(cookie, dict) or not self._is_zhihu_cookie(cookie) for cookie in cookies
        ):
            raise RuntimeError("Managed session contains invalid or non-Zhihu cookies")
        context.add_cookies(cookies)

    @staticmethod
    def _is_zhihu_cookie(cookie: dict) -> bool:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        return domain == "zhihu.com" or domain.endswith(".zhihu.com")

    def _save_managed_session(self, context, source_profile: Path) -> bool:
        path = source_profile / "autosci-zhihu-cookies.json"
        temporary = source_profile / "autosci-zhihu-cookies.json.tmp"
        try:
            cookies = [cookie for cookie in context.cookies() if self._is_zhihu_cookie(cookie)]
            if not any(cookie.get("name") == "z_c0" and cookie.get("value") for cookie in cookies):
                return False
            temporary.write_text(json.dumps(cookies), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            self._record_session_status(True)
            return True
        except Exception:
            logger.warning("Could not persist refreshed Zhihu session", exc_info=True)
            return False

    def _prepare_launch_profile(self, source_profile: Path) -> Path:
        runtime_root = Path(tempfile.mkdtemp(prefix="autosci_zhihu_profile_"))
        runtime_profile = runtime_root / "profile"
        try:
            runtime_profile.mkdir(parents=True, exist_ok=False)
            session_files = (
                "cookies.sqlite",
                "cookies.sqlite-shm",
                "cookies.sqlite-wal",
                "permissions.sqlite",
                "content-prefs.sqlite",
                "containers.json",
                "SiteSecurityServiceState.txt",
            )
            copied = 0
            for filename in session_files:
                source = source_profile / filename
                if not source.exists() or not source.is_file():
                    continue
                shutil.copy2(source, runtime_profile / filename)
                copied += 1
            if copied == 0 or not (runtime_profile / "cookies.sqlite").exists():
                raise RuntimeError("Firefox Profile 中没有可复制的 Cookie 数据库。")
            return runtime_profile
        except Exception as exc:
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise RuntimeError(f"复制 Firefox 登录配置失败：{exc}") from exc

    @staticmethod
    def _cleanup_launch_profile(launch_profile: Path, source_profile: Path) -> None:
        if launch_profile.resolve() == source_profile.resolve():
            return
        try:
            shutil.rmtree(launch_profile.parent, ignore_errors=True)
        except Exception:
            return

    def _resolve_firefox_profile_path(self) -> Path | None:
        configured = self._settings.zhihu_firefox_profile_path
        if configured:
            path = Path(configured).expanduser()
            if path.exists():
                return path
            return None

        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        profiles_ini = Path(appdata) / "Mozilla" / "Firefox" / "profiles.ini"
        if not profiles_ini.exists():
            return None

        parser = configparser.ConfigParser()
        parser.read(profiles_ini, encoding="utf-8")
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            is_default = parser.get(section, "Default", fallback="0") == "1"
            if not is_default:
                continue
            profile_path = parser.get(section, "Path", fallback="")
            is_relative = parser.get(section, "IsRelative", fallback="1") == "1"
            if not profile_path:
                continue
            if is_relative:
                candidate = Path(appdata) / "Mozilla" / "Firefox" / profile_path
            else:
                candidate = Path(profile_path)
            if candidate.exists():
                return candidate
        return None
