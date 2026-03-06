from dataclasses import dataclass
from typing import Callable

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
        for idx, paper in enumerate(papers, start=1):
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
        failed_count = 0
        ignored_count = 0

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

                try:
                    publish_title = self._pick_publish_title(task.note_markdown, task.title)
                    url = self._zhihu_service.publish_article(publish_title, task.note_markdown)
                    crud.update_publication_task(
                        db,
                        task.id,
                        status="PUBLISHED",
                        zhihu_url=url,
                        command_text=cmd.command_text,
                        requested_by=cmd.sender,
                    )
                    published_count += 1
                    cmd_success.append(f"{code}: published")
                except Exception as exc:
                    failed_status = self._status_from_exception(exc)
                    crud.update_publication_task(
                        db,
                        task.id,
                        status=failed_status,
                        error_message=str(exc),
                        command_text=cmd.command_text,
                        requested_by=cmd.sender,
                    )
                    failed_count += 1
                    cmd_failed.append(f"{code}: {exc}")

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

        return PublicationSummary(
            received_commands=len(commands),
            published_count=published_count,
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
            try:
                publish_title = self._pick_publish_title(task.note_markdown, task.title)
                url = self._zhihu_service.publish_article(publish_title, task.note_markdown)
                crud.update_publication_task(
                    db,
                    task.id,
                    status="PUBLISHED",
                    zhihu_url=url,
                    command_text=command_text or "manual_publish",
                    requested_by=sender,
                )
                results.append({"code": code, "status": "PUBLISHED", "zhihu_url": url})
            except Exception as exc:
                failed_status = self._status_from_exception(exc)
                crud.update_publication_task(
                    db,
                    task.id,
                    status=failed_status,
                    error_message=str(exc),
                    command_text=command_text or "manual_publish",
                    requested_by=sender,
                )
                results.append({"code": code, "status": failed_status, "error": str(exc)})

        stats = {
            "PUBLISHED": 0,
            "ALREADY_PUBLISHED": 0,
            "FAILED": 0,
            "RATE_LIMITED": 0,
            "NOT_FOUND": 0,
        }
        for item in results:
            status = item.get("status", "")
            if status in stats:
                stats[status] += 1

        return {"requested": normalized, "results": results, "stats": stats}

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

    @staticmethod
    def _status_from_exception(exc: Exception) -> str:
        text = str(exc)
        if "发布频率过高" in text or "知乎发布冷却中" in text or "触发24小时冷却" in text:
            return "RATE_LIMITED"
        return "FAILED"
