import logging
import re
import smtplib
import textwrap
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from app.core.config import Settings
from app.db.models import PublicationTask

logger = logging.getLogger(__name__)


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_paper_code_digest(self, run_id: int, tasks: list[PublicationTask]) -> bool:
        if not (self._settings.smtp_host and self._settings.mail_to):
            return False

        username = self._settings.smtp_username or self._settings.mail_from
        password = self._settings.smtp_password or self._settings.imap_password
        mail_from = self._settings.mail_from or username
        if not mail_from:
            return False

        message = MIMEMultipart("mixed")
        message["Subject"] = f"[AutoSci] 今日论文编号 (Run #{run_id})"
        message["From"] = mail_from
        message["To"] = self._settings.mail_to

        card_attachments = self._build_markdown_card_attachments(run_id=run_id, tasks=tasks, max_cards=3)
        html_lines = [
            f"<h2>AutoSci 今日论文编号 (Run #{run_id})</h2>",
            "<p>回复命令示例：<strong>发布R005-01,R005-03</strong></p>",
            (
                f"<p>已附上 {len(card_attachments)} 份论文总结 PNG 卡片（邮件附件）。</p>"
                if card_attachments
                else "<p>未生成 PNG 卡片附件。</p>"
            ),
            "<ul>",
        ]
        for task in tasks:
            html_lines.append(f"<li><strong>{task.paper_code}</strong> | {task.title} | 状态: {task.status}</li>")
        html_lines.append("</ul>")
        message.attach(MIMEText("\n".join(html_lines), "html", "utf-8"))

        for filename, image_bytes in card_attachments:
            image_part = MIMEImage(image_bytes, _subtype="png")
            image_part.add_header("Content-Disposition", "attachment", filename=filename)
            message.attach(image_part)

        raw = message.as_string()
        if self._send_via_starttls(raw, mail_from, username, password):
            return True
        return self._send_via_ssl(raw, mail_from, username, password)

    def _build_markdown_card_attachments(
        self,
        run_id: int,
        tasks: list[PublicationTask],
        max_cards: int = 3,
    ) -> list[tuple[str, bytes]]:
        if not tasks:
            return []

        cards_dir = Path(self._settings.artifacts_dir) / "mail_cards" / f"run_{run_id}"
        cards_dir.mkdir(parents=True, exist_ok=True)
        for stale in cards_dir.glob("*.png"):
            try:
                stale.unlink()
            except Exception:
                pass
        for stale in cards_dir.glob("*.html"):
            try:
                stale.unlink()
            except Exception:
                pass

        ordered_tasks = sorted(tasks, key=lambda t: (t.paper_code or ""))
        attachments: list[tuple[str, bytes]] = []
        for task in ordered_tasks[:max_cards]:
            file_stem = self._safe_filename(f"{task.paper_code}_{task.title}")[:96]
            png_path = cards_dir / f"{file_stem}.png"
            try:
                self._render_markdown_card(task=task, run_id=run_id, output_path=png_path)
                attachments.append((png_path.name, png_path.read_bytes()))
            except Exception as exc:
                logger.warning("Failed to render mail PNG card for %s: %s", task.paper_code, exc)
        return attachments

    def _render_markdown_card(self, task: PublicationTask, run_id: int, output_path: Path) -> None:
        self._render_markdown_card_text(task=task, run_id=run_id, output_path=output_path)

    def _render_markdown_card_text(self, task: PublicationTask, run_id: int, output_path: Path) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch

        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["font.sans-serif"] = [
            "Microsoft YaHei",
            "SimHei",
            "Noto Sans CJK SC",
            "WenQuanYi Zen Hei",
            "Arial Unicode MS",
            "sans-serif",
        ]

        plain_text = self._markdown_to_plain_text(task.note_markdown)
        plain_text = plain_text.replace("$", "＄")
        wrapped_lines = self._wrap_for_card(plain_text, width=52)
        if not wrapped_lines:
            wrapped_lines = ["（无摘要内容）"]

        line_count = len(wrapped_lines) + 6
        figure_height = max(10.0, min(56.0, line_count * 0.34))
        max_visible = max(30, int((figure_height - 4.0) / 0.28))
        if len(wrapped_lines) > max_visible:
            wrapped_lines = wrapped_lines[: max_visible - 1] + ["..."]
        body_text = "\n".join(wrapped_lines)

        fig = plt.figure(figsize=(8.8, figure_height), dpi=170)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")

        card = FancyBboxPatch(
            (0.03, 0.02),
            0.94,
            0.96,
            boxstyle="round,pad=0.02,rounding_size=0.018",
            linewidth=1.2,
            edgecolor="#D0D7DE",
            facecolor="#F8FAFC",
            transform=ax.transAxes,
        )
        ax.add_patch(card)

        display_title = (task.title or "Untitled").strip()
        if len(display_title) > 90:
            display_title = display_title[:87] + "..."
        ax.text(
            0.06,
            0.965,
            display_title,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=16,
            fontweight="bold",
            color="#111827",
        )
        ax.text(
            0.06,
            0.925,
            f"{task.paper_code} | Run #{run_id}",
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=11.5,
            color="#4B5563",
        )
        ax.text(
            0.06,
            0.885,
            body_text,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=10.6,
            color="#1F2937",
            linespacing=1.45,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, format="png", dpi=170, facecolor="white", bbox_inches="tight", pad_inches=0.08)
        plt.close(fig)

    @staticmethod
    def _markdown_to_plain_text(markdown_text: str) -> str:
        text = markdown_text or ""
        text = re.sub(r"```[\s\S]*?```", " ", text)
        text = re.sub(r"\$\$([\s\S]*?)\$\$", lambda m: f"公式: {m.group(1).strip()}", text)
        text = re.sub(r"\$([^$]+)\$", lambda m: m.group(1).strip(), text)
        text = re.sub(r"`([^`]+)`", r"\1", text)

        lines: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("![") or line.lower().startswith("<img"):
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)
            line = re.sub(r"^>\s*", "", line)
            line = re.sub(r"^[-*+]\s+", "", line)
            line = re.sub(r"^\d+\.\s+", "", line)
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", line)
            line = re.sub(r"<[^>]+>", "", line)
            line = line.replace("**", "").replace("__", "").replace("*", "")
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)

        plain_text = "\n".join(lines)
        if len(plain_text) > 14000:
            return plain_text[:14000] + "..."
        return plain_text

    @staticmethod
    def _wrap_for_card(text: str, width: int = 52) -> list[str]:
        wrapped: list[str] = []
        for paragraph in text.splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                if wrapped and wrapped[-1] != "":
                    wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(paragraph, width=width, break_long_words=True, break_on_hyphens=False))
        return wrapped

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
        cleaned = re.sub(r"\s+", "_", cleaned)
        return cleaned or "summary_card"

    def _send_via_starttls(
        self,
        raw_message: str,
        mail_from: str,
        username: str | None,
        password: str | None,
    ) -> bool:
        try:
            server = smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=30)
            try:
                if self._settings.smtp_use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.sendmail(mail_from, [self._settings.mail_to], raw_message)
                return True
            finally:
                server.quit()
        except Exception:
            return False

    def _send_via_ssl(
        self,
        raw_message: str,
        mail_from: str,
        username: str | None,
        password: str | None,
    ) -> bool:
        try:
            server = smtplib.SMTP_SSL(self._settings.smtp_host, 465, timeout=30)
            try:
                if username and password:
                    server.login(username, password)
                server.sendmail(mail_from, [self._settings.mail_to], raw_message)
                return True
            finally:
                server.quit()
        except Exception:
            return False
