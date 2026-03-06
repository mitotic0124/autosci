import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import Settings
from app.db.models import PublicationTask


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

        message = MIMEMultipart("alternative")
        message["Subject"] = f"[AutoSci] 今日论文编号 (Run #{run_id})"
        message["From"] = mail_from
        message["To"] = self._settings.mail_to

        html_lines = [
            f"<h2>AutoSci 今日论文编号 (Run #{run_id})</h2>",
            "<p>回复命令示例：<strong>发布R005-01,R005-03</strong></p>",
            "<ul>",
        ]
        for task in tasks:
            html_lines.append(
                f"<li><strong>{task.paper_code}</strong> | {task.title} | 状态: {task.status}</li>"
            )
        html_lines.append("</ul>")
        message.attach(MIMEText("\n".join(html_lines), "html", "utf-8"))

        raw = message.as_string()
        if self._send_via_starttls(raw, mail_from, username, password):
            return True
        return self._send_via_ssl(raw, mail_from, username, password)

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
