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
        self._ensure_not_rate_limited()

        profile_path = self._resolve_firefox_profile_path()
        if not profile_path:
            raise RuntimeError("未找到 Firefox 登录配置，请设置 ZHIHU_FIREFOX_PROFILE_PATH。")

        body = markdown_content.strip()
        if len(body) > 18000:
            body = body[:18000] + "\n\n（内容过长，已截断，请在系统中查看完整笔记。）"

        body = self._convert_markdown_for_zhihu(body)
        body_text, image_files = self._split_body_and_images(body)

        with sync_playwright() as playwright:
            launch_profile = self._prepare_launch_profile(profile_path)
            context = playwright.firefox.launch_persistent_context(
                user_data_dir=str(launch_profile),
                channel="firefox",
                headless=self._settings.zhihu_headless,
                viewport={"width": 1366, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self._settings.zhihu_column_url or "https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2000)
                self._check_access_block(page)
                self._check_publish_rate_limit(page, stage="打开写作页")

                current_url = page.url.lower()
                if "signin" in current_url or "login" in current_url:
                    raise RuntimeError(f"检测到知乎未登录或登录态失效，当前URL: {page.url}")

                title_filled = self._fill_title(page, title)
                if not title_filled:
                    body_text = f"{title}\n\n{body_text}"

                self._check_access_block(page)
                self._fill_body(page, body_text)
                self._assert_body_written(page, min_chars=30)
                self._upload_images(page, image_files)
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

    def _fill_body(self, page, body: str) -> None:
        if self._fill_markdown_textarea(page, body):
            return
        if self._import_markdown_file(page, body):
            return

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
                return
        raise RuntimeError("未找到知乎正文编辑器，页面结构可能已变化。")

    @staticmethod
    def _fill_markdown_textarea(page, body: str) -> bool:
        selectors = [
            "textarea[placeholder*='请输入正文']",
            "textarea[placeholder*='正文']",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.click()
            locator.fill(body)
            return True
        return False

    def _import_markdown_file(self, page, body: str) -> bool:
        temp_dir = Path(self._settings.artifacts_dir) / "zhihu_debug"
        temp_dir.mkdir(parents=True, exist_ok=True)
        md_path = temp_dir / f"zhihu_import_{int(time.time() * 1000)}.md"
        md_path.write_text(body, encoding="utf-8")

        def try_set_file() -> bool:
            file_inputs = page.locator(
                "input[type='file'][accept*='.md'], "
                "input[type='file'][accept*='markdown'], "
                "input[type='file'][accept*='text/plain']"
            )
            if file_inputs.count() == 0:
                return False
            try:
                file_inputs.first.set_input_files(str(md_path))
                page.wait_for_timeout(2200)
                return True
            except Exception:
                return False

        for label in ("导入 Markdown", "导入markdown", "导入文档", "导入", "Markdown"):
            button = page.get_by_role("button", name=label).first
            if button.count() == 0:
                continue
            try:
                button.click(timeout=8_000)
                page.wait_for_timeout(800)
                if try_set_file():
                    return True
            except Exception:
                continue

        return False

    @staticmethod
    def _upload_images(page, image_files: list[Path]) -> None:
        if not image_files:
            return

        valid = [path for path in image_files if path.exists()]
        if not valid:
            return

        file_inputs = page.locator("input[type='file'][accept*='image']")
        if file_inputs.count() == 0:
            return

        file_inputs.first.set_input_files([str(path) for path in valid[:9]])
        page.wait_for_timeout(2600)
        if page.get_by_text("素材文件加载失败").count() > 0:
            raise RuntimeError("图片上传失败：知乎提示“素材文件加载失败”。")

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
            raise RuntimeError(f"正文写入校验失败，当前正文长度仅 {length} 字符。")

    def _split_body_and_images(self, body: str) -> tuple[str, list[Path]]:
        image_paths: list[Path] = []

        def replace_image(match: re.Match) -> str:
            alt = (match.group(1) or "图").strip()
            url = (match.group(2) or "").strip()
            file_path = self._resolve_local_image_path(url)
            if file_path:
                image_paths.append(file_path)
                return f"\n[{alt}]\n"
            return match.group(0)

        body_text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace_image, body)
        return body_text, image_paths

    def _resolve_local_image_path(self, url: str) -> Path | None:
        if not url:
            return None
        if url.startswith("http://") or url.startswith("https://"):
            return None
        if url.startswith("/artifacts/"):
            rel = url[len("/artifacts/") :]
            path = Path(self._settings.artifacts_dir) / rel
        else:
            path = Path(url)
            if not path.is_absolute():
                path = Path(self._settings.artifacts_dir) / url.lstrip("/")
        return path if path.exists() else None

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
