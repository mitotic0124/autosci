import configparser
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import Settings

logger = logging.getLogger(__name__)


class ZhihuService:
    RATE_LIMIT_HINTS = (
        "24小时内发布频率过高",
        "发布频率过高",
        "发布过于频繁",
        "操作过于频繁",
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def publish_article(self, title: str, markdown_content: str) -> str:
        if not self._settings.zhihu_publish_enabled:
            raise RuntimeError("知乎发布功能已禁用。")
        if self._settings.zhihu_auto_publish:
            self._ensure_not_rate_limited()

        profile_path = self._resolve_firefox_profile_path()
        if not profile_path:
            raise RuntimeError("未找到 Firefox 登录配置，请设置 ZHIHU_FIREFOX_PROFILE_PATH。")

        body = markdown_content.strip()
        if len(body) > 18000:
            body = body[:18000] + "\n\n（内容过长，已截断，请在系统中查看完整笔记。）"

        body = self._convert_markdown_for_zhihu(body)
        body_text, image_slots = self._split_body_and_images(body)

        with sync_playwright() as playwright:
            launch_profile = self._prepare_launch_profile(profile_path)
            context = playwright.firefox.launch_persistent_context(
                user_data_dir=str(launch_profile),
                channel="firefox",
                headless=self._settings.zhihu_headless,
                viewport={"width": 1366, "height": 900},
            )
            try:
                page = context.new_page()
                self._ensure_browser_interactable(page)
                page.goto(self._settings.zhihu_column_url or "https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=60_000)
                self._ensure_browser_interactable(page)
                page.wait_for_timeout(2000)
                self._check_access_block(page)
                if self._settings.zhihu_auto_publish:
                    self._check_publish_rate_limit(page, stage="打开写作页")

                current_url = page.url.lower()
                if "signin" in current_url or "login" in current_url:
                    raise RuntimeError(f"检测到知乎未登录或登录态失效，当前URL: {page.url}")

                self._ensure_browser_interactable(page)
                title_filled = self._fill_title(page, title)
                if not title_filled:
                    body_text = f"{title}\n\n{body_text}"

                self._check_access_block(page)
                fill_mode = self._fill_body(page, body_text)
                self._assert_body_written(page, min_chars=30)
                self._assert_draft_rendered(page, body_text, fill_mode)
                self._upload_images(page, image_slots)
                if not self._settings.zhihu_auto_publish:
                    page.wait_for_timeout(1500)
                    return self._resolve_draft_url(page)
                self._ensure_browser_interactable(page)
                self._click_publish(page)
                self._check_publish_rate_limit(page, stage="点击发布后")
                page.wait_for_timeout(2500)
                self._check_publish_rate_limit(page, stage="发布确认后")
                return self._verify_published_url(page)
            except PlaywrightTimeoutError as exc:
                debug_path = self._save_debug_snapshot(page)
                raise RuntimeError(f"知乎发布超时: {exc} | 调试快照: {debug_path}") from exc
            except Exception as exc:
                debug_path = self._save_debug_snapshot(page)
                if "Target page, context or browser has been closed" in str(exc):
                    raise RuntimeError(
                        f"页面在编辑过程中被关闭，可能触发知乎风控或账号状态异常。调试快照: {debug_path}"
                    ) from exc
                raise RuntimeError(f"{exc} | 调试快照: {debug_path}") from exc
            finally:
                context.close()

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
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "$targets = @('Firefox','Mozilla Firefox','火狐','Mozilla'); "
            "foreach($t in $targets){ "
            "  try { if([Microsoft.VisualBasic.Interaction]::AppActivate($t)){ break } } catch {} "
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
    def _click_publish(page) -> None:
        publish_buttons = page.get_by_role("button", name="发布")
        if publish_buttons.count() == 0:
            raise RuntimeError("未找到发布按钮。")

        publish_buttons.last.click(timeout=20_000)
        page.wait_for_timeout(800)

        dialog = page.locator("[role='dialog']").first
        if dialog.count() == 0:
            return

        for label in ("确认发布", "立即发布", "发布文章", "发布", "发表"):
            button = dialog.get_by_role("button", name=label).first
            if button.count() > 0:
                button.click(timeout=15_000)
                page.wait_for_timeout(1500)
                return

    @staticmethod
    def _fill_title(page, title: str) -> bool:
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
                locator.click()
                try:
                    locator.fill(title[:120])
                except Exception:
                    page.keyboard.press("Control+A")
                    page.keyboard.type(title[:120], delay=1)
                return True

        fallback = page.get_by_placeholder("请输入标题").first
        if fallback.count() > 0:
            fallback.click()
            fallback.fill(title[:120])
            return True

        return False

    def _fill_body(self, page, body: str) -> str:
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

        def is_doc_accept(accept: str) -> bool:
            text = (accept or "").lower()
            if not text:
                return False
            tokens = (
                ".md",
                "markdown",
                "text/plain",
                "text/markdown",
                ".doc",
                ".docx",
                "application/msword",
                "officedocument.wordprocessingml.document",
            )
            return any(token in text for token in tokens)

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
                    try:
                        file_inputs.nth(idx).set_input_files(str(md_path))
                        page.wait_for_timeout(3200)
                        return True
                    except Exception:
                        continue
            generic_inputs = host.locator("input[type='file']")
            count = min(generic_inputs.count(), 8)
            for idx in range(count):
                item = generic_inputs.nth(idx)
                try:
                    accept = (item.get_attribute("accept") or "").lower()
                except Exception:
                    accept = ""
                if accept and not is_doc_accept(accept):
                    continue
                try:
                    item.set_input_files(str(md_path))
                    page.wait_for_timeout(3200)
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
                return True
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

        return False

    def _upload_images(self, page, image_slots: list[tuple[str, Path]]) -> None:
        if not image_slots:
            return

        valid_slots = [(marker, path) for marker, path in image_slots if path.exists()]
        if not valid_slots:
            return

        limited_slots = valid_slots[:9]
        total = len(limited_slots)
        for idx, (marker, path) in enumerate(limited_slots, start=1):
            is_last = idx == total
            uploaded = False
            last_error: Exception | None = None
            for attempt in range(1, 4):
                if not self._set_cursor_to_marker(page, marker):
                    self._move_cursor_to_end(page)
                try:
                    self._upload_inline_image_at_cursor(
                        page,
                        path,
                        timeout_ms=22_000 if is_last else 15_000,
                    )
                    uploaded = True
                    break
                except Exception as exc:
                    last_error = exc
                    self._ensure_browser_interactable(page)
                    page.wait_for_timeout(900)
            if not uploaded:
                raise RuntimeError(
                    f"图片插入失败（{idx}/{total}）：{path.name}"
                    + (f"；原因：{last_error}" if last_error else "")
                )
            self._remove_marker_text(page, marker)
            if is_last:
                page.wait_for_timeout(1200)

    def _upload_inline_image_at_cursor(self, page, image_path: Path, timeout_ms: int = 15_000) -> None:
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
                    return
                except RuntimeError:
                    raise
                except Exception:
                    continue
        raise RuntimeError(f"未找到正文图片上传入口，无法插入图片：{image_path.name}")

    @staticmethod
    def _wait_image_upload_stable(page, timeout_ms: int = 15_000) -> None:
        deadline = time.time() + timeout_ms / 1000.0
        stable_rounds = 0
        while time.time() < deadline:
            if page.get_by_text("素材文件加载失败").count() > 0 or page.get_by_text("文件加载失败").count() > 0:
                raise RuntimeError("图片上传失败：知乎提示“素材文件加载失败”。")
            if page.get_by_text("上传失败").count() > 0:
                raise RuntimeError("图片上传失败：知乎提示“上传失败”。")

            pending = (
                page.get_by_text("上传中").count() > 0
                or page.get_by_text("处理中").count() > 0
                or page.get_by_text("加载中").count() > 0
                or page.locator("[role='progressbar']").count() > 0
                or page.locator("[class*='progress']").count() > 0
            )
            if pending:
                stable_rounds = 0
            else:
                stable_rounds += 1
                if stable_rounds >= 3:
                    return
            page.wait_for_timeout(450)

        # Timeout without explicit failure text: continue, but with conservative extra wait.
        page.wait_for_timeout(800)

    @staticmethod
    def _set_cursor_to_marker(page, marker: str) -> bool:
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
                          const range = document.createRange();
                          range.setStart(node, idx);
                          range.setEnd(node, idx);
                          const sel = window.getSelection();
                          if (sel) {
                            sel.removeAllRanges();
                            sel.addRange(range);
                          }
                          const editable = node.parentElement && node.parentElement.closest("[contenteditable='true']");
                          if (editable) {
                            editable.focus();
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
                          const range = document.createRange();
                          range.setStart(node, idx);
                          range.setEnd(node, idx + target.length);
                          const sel = window.getSelection();
                          if (sel) {
                            sel.removeAllRanges();
                            sel.addRange(range);
                          }
                          const editable = node.parentElement && node.parentElement.closest("[contenteditable='true']");
                          if (editable) {
                            editable.focus();
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

    def _remove_marker_text(self, page, marker: str) -> None:
        if not self._select_marker_text(page, marker):
            return
        try:
            page.keyboard.press("Delete")
            page.wait_for_timeout(300)
        except Exception:
            return

    @staticmethod
    def _assert_body_written(page, min_chars: int = 20) -> None:
        length = 0
        try:
            length = page.evaluate(
                """
                () => {
                  const textarea = document.querySelector("textarea[placeholder*='请输入正文'], textarea[placeholder*='正文']");
                  if (textarea && 'value' in textarea) {
                    return (textarea.value || '').trim().length;
                  }
                  const editable = document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                  if (editable) {
                    return (editable.innerText || '').trim().length;
                  }
                  return 0;
                }
                """
            )
        except Exception:
            length = 0
        if int(length) < min_chars:
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
        if not self._looks_like_markdown_body(source_body):
            return
        page.wait_for_timeout(1200)
        editor_text = self._read_editor_text(page).strip()
        if not editor_text:
            return
        if self._looks_like_raw_markdown(editor_text):
            preview = editor_text.replace("\n", " ")[:120]
            raise RuntimeError(f"知乎草稿仍显示 Markdown 源码（mode={fill_mode}，preview={preview}），已中止发布。")

    @staticmethod
    def _read_editor_text(page) -> str:
        try:
            content = page.evaluate(
                """
                () => {
                  const textNodes = Array.from(document.querySelectorAll("[data-contents='true'] [data-text='true']"));
                  if (textNodes.length > 0) {
                    return textNodes.slice(0, 320).map(node => node.textContent || "").join("\\n");
                  }
                  const textarea = document.querySelector("textarea");
                  if (textarea && 'value' in textarea) {
                    return textarea.value || '';
                  }
                  const editable = document.querySelector("div[contenteditable='true']:not([data-placeholder*='标题'])");
                  if (editable) {
                    return editable.innerText || editable.textContent || '';
                  }
                  return '';
                }
                """
            )
            return content if isinstance(content, str) else ""
        except Exception:
            return ""

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

    def _resolve_draft_url(self, page) -> str:
        current = page.url
        if self._is_edit_url(current):
            return current

        match = re.search(r"/p/(\d+)", current)
        if match:
            return f"https://zhuanlan.zhihu.com/p/{match.group(1)}/edit"

        try:
            links = page.evaluate(
                """
                () => Array.from(document.querySelectorAll("a[href]"))
                  .map(item => item.href || "")
                  .filter(Boolean)
                  .slice(0, 400)
                """
            )
            if isinstance(links, list):
                for href in links:
                    if not isinstance(href, str):
                        continue
                    if "/p/" not in href:
                        continue
                    id_match = re.search(r"/p/(\\d+)", href)
                    if id_match:
                        return f"https://zhuanlan.zhihu.com/p/{id_match.group(1)}/edit"
        except Exception:
            pass

        try:
            html = page.content()
        except Exception:
            html = ""
        html_match = re.search(r"https?://zhuanlan\\.zhihu\\.com/p/(\\d+)/edit", html)
        if html_match:
            return html_match.group(0)

        return current

    @staticmethod
    def _is_edit_url(url: str | None) -> bool:
        return bool(url and "/edit" in url)

    def _verify_published_url(self, page) -> str:
        self._check_publish_rate_limit(page, stage="发布结果校验")
        current = page.url
        if "zhuanlan.zhihu.com/p/" not in current:
            raise RuntimeError(f"发布结果校验失败，未进入文章相关页面，当前URL: {current}")

        match = re.search(r"/p/(\d+)", current)
        if not match:
            raise RuntimeError(f"发布结果校验失败，未识别文章ID，当前URL: {current}")

        article_id = match.group(1)
        public_url = f"https://zhuanlan.zhihu.com/p/{article_id}"
        page.goto(public_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1200)
        final_url = page.url
        if "zhuanlan.zhihu.com/p/" in final_url and "/edit" not in final_url:
            return final_url
        self._check_publish_rate_limit(page, stage="发布结果校验")
        raise RuntimeError(f"发布后仍停留在编辑页或未进入公开页，当前URL: {final_url}")

    def _convert_markdown_for_zhihu(self, markdown_text: str) -> str:
        cmd_text = (self._settings.zhihu_markdown_converter_cmd or "").strip()
        if not cmd_text:
            return markdown_text

        cmd_parts = self._resolve_converter_cmd_parts(cmd_text)
        if not cmd_parts:
            return markdown_text

        work_dir = Path(self._settings.artifacts_dir) / "zhihu_convert" / f"run_{int(time.time() * 1000)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        input_md = work_dir / "input.md"
        output_md = work_dir / "output.md"
        input_md.write_text(markdown_text, encoding="utf-8")
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
                if text:
                    return text

            generated = sorted(
                [p for p in work_dir.rglob("*.md") if p.name != "input.md"],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if generated:
                text = generated[0].read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    return text

            if stdout_text and len(stdout_text.strip()) > 20:
                return stdout_text.strip()

            rewritten = input_md.read_text(encoding="utf-8", errors="ignore").strip()
            if rewritten and rewritten != markdown_text.strip():
                return rewritten

        return markdown_text

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

    @staticmethod
    def _check_access_block(page) -> None:
        try:
            content = page.content()
        except Exception:
            return
        if "40362" in content or "暂时限制本次访问" in content:
            raise RuntimeError("知乎返回风控限制（code 40362），当前会话暂不可自动发布。")

    def _ensure_not_rate_limited(self) -> None:
        state = self._load_zhihu_state()
        text = str(state.get("rate_limit_until") or "").strip()
        if not text:
            return

        until = self._parse_iso_datetime(text)
        if not until:
            return

        now = datetime.now(timezone.utc)
        if until <= now:
            state.pop("rate_limit_until", None)
            self._save_zhihu_state(state)
            return

        raise RuntimeError(f"知乎发布冷却中：检测到24小时发布频率限制，请在 {self._format_dt(until)} 后重试。")

    def _check_publish_rate_limit(self, page, stage: str = "") -> None:
        hit = self._detect_rate_limit_hint(page)
        if not hit:
            return
        until = self._mark_rate_limited(hit)
        prefix = f"{stage}：" if stage else ""
        raise RuntimeError(f"{prefix}知乎提示“{hit}”，触发24小时冷却，请在 {self._format_dt(until)} 后重试。")

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

    def _mark_rate_limited(self, reason: str) -> datetime:
        now = datetime.now(timezone.utc)
        until = now + timedelta(hours=24)
        state = self._load_zhihu_state()
        state["rate_limit_until"] = until.isoformat()
        state["rate_limit_reason"] = reason
        state["updated_at"] = now.isoformat()
        self._save_zhihu_state(state)
        return until

    def _state_file(self) -> Path:
        return Path(self._settings.artifacts_dir) / "zhihu_state.json"

    def _load_zhihu_state(self) -> dict:
        path = self._state_file()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _save_zhihu_state(self, payload: dict) -> None:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("保存知乎状态失败: %s", exc)

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

    def _prepare_launch_profile(self, source_profile: Path) -> Path:
        runtime_profile = Path(self._settings.artifacts_dir) / "zhihu_profile_runtime"
        try:
            if runtime_profile.exists():
                shutil.rmtree(runtime_profile, ignore_errors=True)
            shutil.copytree(
                source_profile,
                runtime_profile,
                ignore=shutil.ignore_patterns("parent.lock", "lock", ".parentlock"),
            )
            return runtime_profile
        except Exception:
            return source_profile

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
