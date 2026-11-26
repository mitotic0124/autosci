from dataclasses import dataclass
from typing import Callable
import json
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import crud
from app.db.models import Paper, PublicationTask
from app.services.mail_command_service import MailCommandService
from app.services.zhihu_service import ZhihuService


@dataclass
class PublicationSummary:
    received_commands: int
    published_count: int
    drafted_count: int
    failed_count: int
    ignored_count: int


class PublicationService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mail_command_service = MailCommandService(settings)
        self._zhihu_service = ZhihuService(settings)

    def create_tasks_for_run(
        self,
        db: Session,
        run_id: int,
        papers: list[Paper],
        notes_by_paper_id: dict[int, str],
    ) -> list[PublicationTask]:
        tasks: list[PublicationTask] = []
        existing_tasks = crud.list_publication_tasks(db, run_id=run_id)
        next_index = max((int(task.paper_code.rsplit("-", 1)[-1]) for task in existing_tasks), default=0) + 1
        for idx, paper in enumerate(papers, start=next_index):
            code = f"R{run_id:03d}-{idx:02d}"
            note = notes_by_paper_id.get(paper.id, "")
            publish_title = self._pick_publish_title(note, paper.title)
            task = crud.create_publication_task(
                db=db,
                run_id=run_id,
                paper_id=paper.id,
                paper_code=code,
                title=publish_title,
                note_markdown=note,
            )
            tasks.append(task)
            previous_tasks = db.query(PublicationTask).join(Paper).filter(
                Paper.arxiv_id == paper.arxiv_id,
                PublicationTask.id != task.id,
                PublicationTask.status != "PUBLISHED",
            ).all()
            for previous in previous_tasks:
                previous.status = "SUPERSEDED"
                previous.error_message = f"已由同一论文的新版本 {code} 替代，历史草稿仅保留供审计。"
            if previous_tasks:
                db.commit()
        return tasks

    def process_mail_commands(
        self,
        db: Session,
        step_callback: Callable[[str, str, str], None] | None = None,
    ) -> PublicationSummary:
        processed = crud.get_processed_email_uids(db)
        commands = self._mail_command_service.poll_publish_commands(processed_uids=processed)

        if step_callback:
            step_callback("reply", "active", f"Received {len(commands)} command email(s).")

        published_count = 0
        drafted_count = 0
        failed_count = 0
        ignored_count = 0
        daily_remaining = self.remaining_daily_publications(db) if self._settings.zhihu_auto_publish else len(commands)

        authorized_sender = (self._settings.command_authorized_sender or self._settings.mail_to or "").strip().lower()
        for cmd in commands:
            if authorized_sender and cmd.sender != authorized_sender:
                ignored_count += 1
                crud.save_email_command_log(
                    db=db,
                    message_uid=cmd.message_uid,
                    sender=cmd.sender,
                    subject=cmd.subject,
                    body=cmd.body,
                    command_text=cmd.command_text,
                    status="IGNORED",
                    result=f"Unauthorized sender: {cmd.sender}",
                )
                continue

            cmd_success: list[str] = []
            cmd_failed: list[str] = []
            stop_all_publication = False
            request_owner = self._automatic_queue_owner(cmd.sender)
            total_codes = len(cmd.codes)
            for index, code in enumerate(cmd.codes, start=1):
                if step_callback:
                    step_callback("zhihu", "active", f"Publishing {code} ({index}/{total_codes}).")

                task = crud.get_publication_task_by_code(db, code)
                if not task:
                    cmd_failed.append(f"{code}: not found")
                    continue
                if task.status == "PUBLISHED" and not self._is_edit_url(task.zhihu_url):
                    cmd_success.append(f"{code}: already published")
                    continue
                if task.status in {"PUBLISHING", "PUBLISH_UNCERTAIN"}:
                    cmd_failed.append(f"{code}: publish result must be checked in Zhihu before retry")
                    stop_all_publication = True
                    for queued_code in cmd.codes[index:]:
                        queued_task = crud.get_publication_task_by_code(db, queued_code)
                        if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                            self._mark_queued(
                                db,
                                queued_task,
                                cmd.command_text,
                                self._automatic_queue_owner(cmd.sender),
                                reason="前一篇发布结果待核实，已暂停后续公开发布。",
                            )
                    break
                if task.status == "RATE_LIMITED":
                    if self._zhihu_service.rate_limit_status().get("active"):
                        cmd_failed.append(f"{code}: Zhihu breaker is active; manually verify recovery before clearing it")
                        for queued_code in cmd.codes[index:]:
                            queued_task = crud.get_publication_task_by_code(db, queued_code)
                            if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                                self._mark_queued(
                                    db,
                                    queued_task,
                                    cmd.command_text,
                                    self._automatic_queue_owner(cmd.sender),
                                    reason="知乎触发风控熔断，后续文章已停止并等待人工检查。",
                                )
                        stop_all_publication = True
                        break
                    self._mark_ready_after_manual_clear(db, task)
                if self._settings.zhihu_auto_publish and daily_remaining <= 0:
                    queue_owner = self._automatic_queue_owner(cmd.sender)
                    self._mark_queued(db, task, cmd.command_text, queue_owner)
                    cmd_success.append(f"{code}: queued for the next daily publication window")
                    continue

                try:
                    self._assert_publishable_quality(db, task)
                    publish_title = self._pick_publish_title(task.note_markdown, task.title)
                    replace_content = self._should_replace_failed_draft(task)
                    if self._settings.zhihu_auto_publish:
                        self._mark_publishing(db, task, cmd.command_text, request_owner)
                    url = self._zhihu_service.publish_article(
                        publish_title,
                        task.note_markdown,
                        draft_url=task.zhihu_url if self._is_edit_url(task.zhihu_url) else None,
                        replace_content=replace_content,
                        publication_key=task.paper_code,
                    )
                    final_status = self._status_from_result_url(url)
                    crud.update_publication_task(
                        db,
                        task.id,
                        status=final_status,
                        zhihu_url=url,
                        command_text=cmd.command_text,
                        requested_by=request_owner,
                    )
                    if final_status == "PUBLISHED":
                        daily_remaining -= 1
                        published_count += 1
                        cmd_success.append(f"{code}: published")
                    else:
                        drafted_count += 1
                        cmd_success.append(f"{code}: draft saved")
                except Exception as exc:
                    failed_status = self._status_from_exception(exc)
                    crud.update_publication_task(
                        db,
                        task.id,
                        status=failed_status,
                        zhihu_url=getattr(exc, "draft_url", None) or task.zhihu_url,
                        error_message=str(exc),
                        command_text=cmd.command_text,
                        requested_by=request_owner,
                    )
                    failed_count += 1
                    cmd_failed.append(f"{code}: {exc}")
                    if failed_status in {"RATE_LIMITED", "PUBLISH_UNCERTAIN"}:
                        for queued_code in cmd.codes[index:]:
                            queued_task = crud.get_publication_task_by_code(db, queued_code)
                            if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                                self._mark_queued(
                                    db,
                                    queued_task,
                                    cmd.command_text,
                                    self._automatic_queue_owner(cmd.sender),
                                    reason=(
                                        "前一篇发布结果待核实，已暂停后续公开发布。"
                                        if failed_status == "PUBLISH_UNCERTAIN"
                                        else "知乎触发风控熔断，后续文章已停止并等待人工检查。"
                                    ),
                                )
                        stop_all_publication = True
                        break

            status = "SUCCESS" if not cmd_failed else ("FAILED" if not cmd_success else "PARTIAL")
            result = "; ".join([*cmd_success, *cmd_failed])[:4000]
            crud.save_email_command_log(
                db=db,
                message_uid=cmd.message_uid,
                sender=cmd.sender,
                subject=cmd.subject,
                body=cmd.body,
                command_text=cmd.command_text,
                status=status,
                result=result,
            )
            if stop_all_publication:
                break

        return PublicationSummary(
            received_commands=len(commands),
            published_count=published_count,
            drafted_count=drafted_count,
            failed_count=failed_count,
            ignored_count=ignored_count,
        )

    def publish_codes(
        self,
        db: Session,
        codes: list[str],
        sender: str = "manual",
        command_text: str = "",
        step_callback: Callable[[str, str, str], None] | None = None,
    ) -> dict:
        normalized = [code.strip().upper() for code in codes if code.strip()]
        results: list[dict] = []

        total = len(normalized)
        daily_remaining = self.remaining_daily_publications(db) if self._settings.zhihu_auto_publish else total
        for index, code in enumerate(normalized, start=1):
            if step_callback:
                step_callback("zhihu", "active", f"Publishing {code} ({index}/{total}).")

            task = crud.get_publication_task_by_code(db, code)
            if not task:
                results.append({"code": code, "status": "NOT_FOUND"})
                continue
            if task.status == "PUBLISHED" and not self._is_edit_url(task.zhihu_url):
                results.append({"code": code, "status": "ALREADY_PUBLISHED", "zhihu_url": task.zhihu_url})
                continue
            if task.status in {"PUBLISHING", "PUBLISH_UNCERTAIN"}:
                results.append({
                    "code": code,
                    "status": "PUBLISH_UNCERTAIN",
                    "zhihu_url": task.zhihu_url,
                    "error": "发布请求可能已成功。请先在知乎创作中心核对，确认未发布前不要重试。",
                })
                for queued_code in normalized[index:]:
                    queued_task = crud.get_publication_task_by_code(db, queued_code)
                    if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                        self._mark_queued(
                            db,
                            queued_task,
                            command_text or "publish_result_check_required",
                            self._automatic_queue_owner(sender),
                            reason="前一篇发布结果待核实，已暂停后续公开发布。",
                        )
                        results.append({"code": queued_code, "status": "PENDING", "error": queued_task.error_message})
                break
            if task.status == "RATE_LIMITED":
                if self._zhihu_service.rate_limit_status().get("active"):
                    results.append({
                        "code": code,
                        "status": "RATE_LIMITED",
                        "zhihu_url": task.zhihu_url,
                        "error": task.error_message or "知乎发布熔断中，请先确认平台恢复并解除熔断。",
                    })
                    for queued_code in normalized[index:]:
                        queued_task = crud.get_publication_task_by_code(db, queued_code)
                        if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                            self._mark_queued(
                                db,
                                queued_task,
                                command_text or "daily_publication_queue",
                                self._automatic_queue_owner(sender),
                                reason="知乎触发风控熔断，后续文章已停止并等待人工检查。",
                            )
                            results.append({"code": queued_code, "status": "PENDING", "error": queued_task.error_message})
                    break
                self._mark_ready_after_manual_clear(db, task)
            if self._settings.zhihu_auto_publish and daily_remaining <= 0:
                queue_owner = self._automatic_queue_owner(sender)
                self._mark_queued(db, task, command_text or "daily_publication_queue", queue_owner)
                results.append({"code": code, "status": "PENDING", "error": task.error_message})
                continue
            try:
                self._assert_publishable_quality(db, task)
                publish_title = self._pick_publish_title(task.note_markdown, task.title)
                replace_content = self._should_replace_failed_draft(task)
                if self._settings.zhihu_auto_publish:
                    self._mark_publishing(db, task, command_text or "manual_publish", sender)
                url = self._zhihu_service.publish_article(
                    publish_title,
                    task.note_markdown,
                    draft_url=task.zhihu_url if self._is_edit_url(task.zhihu_url) else None,
                    replace_content=replace_content,
                    publication_key=task.paper_code,
                )
                final_status = self._status_from_result_url(url)
                crud.update_publication_task(
                    db,
                    task.id,
                    status=final_status,
                    zhihu_url=url,
                    command_text=command_text or "manual_publish",
                    requested_by=sender,
                )
                results.append({"code": code, "status": final_status, "zhihu_url": url})
                if final_status == "PUBLISHED":
                    daily_remaining -= 1
            except Exception as exc:
                failed_status = self._status_from_exception(exc)
                crud.update_publication_task(
                    db,
                    task.id,
                    status=failed_status,
                    zhihu_url=getattr(exc, "draft_url", None) or task.zhihu_url,
                    error_message=str(exc),
                    command_text=command_text or "manual_publish",
                    requested_by=sender,
                )
                results.append({"code": code, "status": failed_status, "error": str(exc)})
                if failed_status in {"RATE_LIMITED", "PUBLISH_UNCERTAIN"}:
                    for queued_code in normalized[index:]:
                        queued_task = crud.get_publication_task_by_code(db, queued_code)
                        if queued_task and queued_task.status not in {"PUBLISHED", "PUBLISHING", "PUBLISH_UNCERTAIN"}:
                            self._mark_queued(
                                db,
                                queued_task,
                                command_text or "daily_publication_queue",
                                self._automatic_queue_owner(sender),
                                reason="知乎触发风控熔断，后续文章已停止并等待人工检查。",
                            )
                            results.append({"code": queued_code, "status": "PENDING", "error": queued_task.error_message})
                    break

        stats = {
            "PUBLISHED": 0,
            "DRAFT": 0,
            "PENDING": 0,
            "PUBLISH_UNCERTAIN": 0,
            "ALREADY_PUBLISHED": 0,
            "FAILED": 0,
            "RATE_LIMITED": 0,
            "NOT_FOUND": 0,
            "SUPERSEDED": 0,
            "QUALITY_BLOCKED": 0,
        }
        for item in results:
            status = item.get("status", "")
            if status in stats:
                stats[status] += 1

        return {"requested": normalized, "results": results, "stats": stats}

    def _assert_publishable_quality(self, db: Session, task: PublicationTask) -> None:
        if getattr(task, "status", "") == "QUALITY_BLOCKED":
            raise RuntimeError("该草稿的后续质量复核未通过，必须使用新的完整核验版本。")
        if getattr(task, "status", "") == "SUPERSEDED":
            raise RuntimeError("该历史草稿已由新版本替代，禁止重复发布，请选择最新核验版本。")
        if not self._settings.article_require_verified:
            return
        run = crud.get_run(db, task.run_id)
        summary = json.loads(run.summary or "{}") if run else {}
        quality = next((item for item in summary.get("article_quality", []) if item.get("arxiv_id") == task.paper.arxiv_id), None)
        if not quality or not quality.get("passed") or quality.get("mode") != "verified" or not quality.get("metrics", {}).get("article_context_verified") or not quality.get("metrics", {}).get("article_global_verified"):
            raise RuntimeError("该文章缺少已通过的独立质量核验，必须重新生成后才能保存或发布到知乎。")
        if quality.get("metrics", {}).get("figure_count", 0) and not quality["metrics"].get("figure_captions_verified"):
            raise RuntimeError("该文章的图片说明尚未完成独立核验，必须复验后才能保存或发布到知乎。")

    def remaining_daily_publications(self, db: Session | None) -> int:
        limit = max(0, int(getattr(self._settings, "zhihu_max_publications_per_day", 1)))
        if db is None:
            published_remaining = limit
        else:
            try:
                tz = ZoneInfo(self._settings.timezone)
            except Exception:
                tz = timezone.utc
            now = datetime.now(tz)
            local_start = datetime.combine(now.date(), time.min, tzinfo=tz)
            utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
            published_today = db.query(PublicationTask).filter(
                PublicationTask.status == "PUBLISHED",
                PublicationTask.published_at >= utc_start,
            ).count()
            published_remaining = max(0, limit - published_today)
        reserved_remaining = self._zhihu_service.remaining_publication_attempts()
        return min(published_remaining, reserved_remaining)

    def select_auto_publication_window(self, db: Session, new_tasks: list[PublicationTask]) -> tuple[list[str], int]:
        for task in new_tasks:
            task.status = "PENDING"
            task.error_message = "等待按创建时间排序的自动发布窗口。"
            task.command_text = "auto_publish_after_run"
            task.requested_by = "auto_workflow"
        if new_tasks:
            db.commit()
        tasks = self._list_automatic_publication_queue(db)
        if not self._settings.zhihu_auto_publish:
            return [task.paper_code for task in new_tasks], len(tasks)
        available = self.remaining_daily_publications(db)
        return [task.paper_code for task in tasks[:available]], max(0, len(tasks) - available)

    @staticmethod
    def _list_automatic_publication_queue(db: Session) -> list[PublicationTask]:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        candidates = db.query(PublicationTask).filter(
            PublicationTask.status.in_(["PENDING", "FAILED"]),
            PublicationTask.requested_by == "auto_workflow",
            PublicationTask.created_at >= cutoff,
        ).order_by(PublicationTask.created_at, PublicationTask.id).all()
        queue = []
        for task in candidates:
            if task.status == "FAILED" and not PublicationService.is_retryable_automatic_failure(task.error_message):
                continue
            if not task.note_markdown:
                continue
            queue.append(task)
        return queue

    @staticmethod
    def is_retryable_automatic_failure(error_message: str | None) -> bool:
        """Return whether a failed task can be retried without manual reconciliation.

        These failures happen before a confirmed publish response. Keeping the
        classifier in one place prevents the run-time worker and the newly
        created-task window from silently disagreeing about which articles are
        eligible for the next daily slot.
        """
        return bool(
            re.search(
                r"pre-publish.*timeout|draft.*(?:save|validation).*timeout|草稿.*(?:保存|校验).*超时|"
                r"草稿有\s*\d+\s*个公式未生成可视图形|公式(?:转换不完整|内容或顺序|源码损坏)|"
                r"知乎发布超时|图片(?:插入|上传|数量校验)失败|未找到知乎(?:正文编辑器|发布按钮)|"
                r"知乎发布设置中未找到|专栏选择器未打开|知乎话题搜索未找到|Locator\.(?:evaluate|click).*Timeout",
                str(error_message or ""),
                re.I,
            )
        )

    def daily_publication_window_open(self) -> bool:
        try:
            tz = ZoneInfo(self._settings.timezone)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        return (now.hour, now.minute) >= (
            int(self._settings.schedule_hour),
            int(self._settings.schedule_minute),
        )

    @staticmethod
    def _mark_queued(
        db: Session,
        task: PublicationTask,
        command_text: str,
        sender: str,
        *,
        reason: str = "已达到每日知乎公开发布上限，等待后续发布窗口。",
    ) -> None:
        crud.update_publication_task(
            db,
            task.id,
            status="PENDING",
            zhihu_url=task.zhihu_url,
            error_message=reason,
            command_text=command_text,
            requested_by=sender,
        )

    def _automatic_queue_owner(self, sender: str) -> str:
        """Keep capped automatic work in the automatic queue regardless of trigger source."""
        if self._settings.zhihu_auto_publish and self._settings.workflow_auto_publish_after_run:
            return "auto_workflow"
        return sender

    def _mark_ready_after_manual_clear(self, db: Session, task: PublicationTask) -> None:
        """Make a rate-limited task retryable only after the platform breaker is cleared."""
        owner = task.requested_by or (
            "auto_workflow" if self._settings.workflow_auto_publish_after_run else "manual_reconcile"
        )
        owner = self._automatic_queue_owner(owner)
        crud.update_publication_task(
            db,
            task.id,
            status="PENDING",
            zhihu_url=task.zhihu_url,
            error_message="知乎熔断已由人工确认解除；等待下一个自动发布窗口。",
            command_text=task.command_text or "manual_rate_limit_clear",
            requested_by=owner,
        )
        task.status = "PENDING"
        task.requested_by = owner
        task.error_message = "知乎熔断已由人工确认解除；等待下一个自动发布窗口。"

    @staticmethod
    def _mark_publishing(db: Session, task: PublicationTask, command_text: str, sender: str) -> None:
        crud.update_publication_task(
            db,
            task.id,
            status="PUBLISHING",
            zhihu_url=task.zhihu_url,
            error_message="知乎公开发布操作已开始；若服务中断，请先核实创作中心和公开页面再决定是否重试。",
            command_text=command_text,
            requested_by=sender,
        )

    def resolve_uncertain_publication(self, db: Session, code: str, *, published: bool, public_url: str | None = None) -> dict:
        task = crud.get_publication_task_by_code(db, code)
        if not task:
            raise ValueError(f"找不到发布任务 {code}。")
        if task.status not in {"PUBLISHING", "PUBLISH_UNCERTAIN"}:
            raise ValueError(f"任务 {code} 当前状态为 {task.status}，不需要核实。")

        if published:
            normalized_url = self._normalize_public_url(public_url)
            crud.update_publication_task(
                db,
                task.id,
                status="PUBLISHED",
                zhihu_url=normalized_url,
                error_message=None,
                command_text="manual_reconcile_published",
                requested_by=task.requested_by or "manual_reconcile",
            )
            return {"code": code, "status": "PUBLISHED", "zhihu_url": normalized_url}

        self._zhihu_service.release_publication_attempt(task.paper_code)
        crud.update_publication_task(
            db,
            task.id,
            status="PENDING",
            zhihu_url=task.zhihu_url,
            error_message="用户已核实知乎创作中心与公开页面均未发布；等待自动发布窗口。",
            command_text="manual_reconcile_not_published",
            requested_by=task.requested_by or "auto_workflow",
        )
        return {"code": code, "status": "PENDING", "zhihu_url": task.zhihu_url}

    def reset_rate_limited_retry_budget(self, db: Session) -> int:
        """Give rate-limited tasks a fresh bounded retry budget after manual clearance."""
        tasks = db.query(PublicationTask).filter(PublicationTask.status == "RATE_LIMITED").all()
        reset = 0
        changed = False
        runs: dict[int, object] = {}
        for task in tasks:
            # RATE_LIMITED is only used for an explicit platform rejection or
            # cooldown before a confirmed publish response. A confirmed
            # response is stored as PUBLISH_UNCERTAIN, so this reservation is
            # safe to release when an operator clears the breaker.
            self._zhihu_service.release_publication_attempt(task.paper_code)
            owner = task.requested_by
            if self._settings.zhihu_auto_publish and self._settings.workflow_auto_publish_after_run:
                owner = "auto_workflow"
            task.status = "PENDING"
            task.error_message = "知乎熔断已由人工确认解除；等待下一个自动发布窗口。"
            task.command_text = task.command_text or "manual_rate_limit_clear"
            changed = True
            if owner and owner != task.requested_by:
                task.requested_by = owner
                changed = True
            run = runs.get(task.run_id)
            if run is None:
                run = crud.get_run(db, task.run_id)
                runs[task.run_id] = run
            if not run:
                continue
            try:
                summary = json.loads(run.summary or "{}")
            except (TypeError, ValueError):
                summary = {}
            retries = summary.get("publication_retries")
            if isinstance(retries, dict) and task.paper_code in retries:
                retries.pop(task.paper_code, None)
                run.summary = json.dumps(summary, ensure_ascii=False)
                reset += 1
                changed = True
        if changed:
            db.commit()
        return reset

    @staticmethod
    def _normalize_public_url(value: str | None) -> str:
        candidate = (value or "").strip().rstrip("/")
        if not re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/\d+", candidate):
            raise ValueError("已公开的文章需要填写 https://zhuanlan.zhihu.com/p/{文章ID} 链接。")
        return candidate

    @staticmethod
    def _pick_publish_title(note_markdown: str, fallback_title: str) -> str:
        for line in note_markdown.splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("# "):
                title = text[2:].strip()
                return title[:120] if title else fallback_title[:120]
            break
        return fallback_title[:120]

    @staticmethod
    def _is_edit_url(url: str | None) -> bool:
        if not url:
            return False
        return "/edit" in url

    @classmethod
    def _should_replace_failed_draft(cls, task: PublicationTask) -> bool:
        """Refresh only drafts that failed before an unambiguous publish result."""
        if getattr(task, "status", "") != "FAILED" or not cls._is_edit_url(getattr(task, "zhihu_url", None)):
            return False
        error = str(getattr(task, "error_message", "") or "")
        # An edit URL paired with a pre-publish failure may contain partially
        # uploaded media from the previous attempt. Reusing it is unsafe: the
        # next marker can be interpreted against stale images. Full rewrite is
        # deterministic and does not risk a duplicate public article because
        # no publish request was confirmed for this task.
        if cls.is_retryable_automatic_failure(error):
            return True
        draft_validation_failures = (
            "公式未生成可视图形",
            "公式源码损坏",
            "公式转换不完整",
            "公式内容或顺序",
            "未解析的公式 HTML",
            "仍显示 Markdown 源码",
            "草稿格式校验失败",
            "草稿正文缺失",
            "正文缺失或顺序不一致",
            "正文写入校验失败",
            "图片插入失败",
            "图片上传失败",
            "图片数量校验失败",
            "知乎发布设置中未找到",
            "专栏选择器未打开",
            "知乎话题搜索未找到",
            "知乎发布超时",
            "Locator.",
        )
        return any(marker in error for marker in draft_validation_failures)

    @staticmethod
    def _status_from_exception(exc: Exception) -> str:
        text = str(exc)
        if "知乎发布请求结果不确定" in text:
            return "PUBLISH_UNCERTAIN"
        if (
            "已达到每日知乎公开发布上限" in text
            or "知乎账号交互冷却中" in text
            or "本日知乎账号交互次数已达上限" in text
        ):
            return "PENDING"
        if "后续质量复核未通过" in text:
            return "QUALITY_BLOCKED"
        if "已由新版本替代" in text:
            return "SUPERSEDED"
        if "发布频率过高" in text or "知乎发布冷却中" in text or "熔断" in text or "风控" in text or "40362" in text or "触发24小时冷却" in text:
            return "RATE_LIMITED"
        if "每日知乎公开发布上限" in text:
            return "PENDING"
        return "FAILED"

    def _status_from_result_url(self, url: str | None) -> str:
        if not self._settings.zhihu_auto_publish:
            return "DRAFT"
        if self._is_edit_url(url):
            return "DRAFT"
        if re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/\d+/?", str(url or "")):
            return "PUBLISHED"
        raise RuntimeError("知乎发布请求结果不确定：未返回有效的公开文章链接，禁止自动重复提交。")
