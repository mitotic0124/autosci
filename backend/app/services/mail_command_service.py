import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class PublishCommand:
    message_uid: str
    sender: str
    subject: str
    body: str
    command_text: str
    codes: list[str]


class MailCommandService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._command_pattern = re.compile(r"发布\s*([A-Za-z0-9\-_，,\s]+)")
        self._paper_code_pattern = re.compile(r"^R\d{1,6}-\d{2}$", re.IGNORECASE)

    def poll_publish_commands(self, processed_uids: set[str]) -> list[PublishCommand]:
        host = self._settings.imap_host
        username = self._settings.imap_username or self._settings.smtp_username or self._settings.mail_from
        password = self._settings.imap_password or self._settings.smtp_password
        if not (host and username and password):
            return []

        client: imaplib.IMAP4 | imaplib.IMAP4_SSL
        if self._settings.imap_use_ssl:
            client = imaplib.IMAP4_SSL(host, self._settings.imap_port)
        else:
            client = imaplib.IMAP4(host, self._settings.imap_port)

        commands: list[PublishCommand] = []
        try:
            try:
                client.login(username, password)
            except imaplib.IMAP4.error as exc:
                detail = self._decode_imap_error(exc)
                raise RuntimeError(f"IMAP 登录失败：{detail}（账号: {username}）") from exc
            self._send_imap_id(client)
            select_status, select_data = client.select(self._settings.imap_folder)
            if select_status != "OK":
                detail = self._decode_imap_payload(select_data)
                if "Unsafe Login" in detail:
                    raise RuntimeError(
                        "IMAP 已登录但被邮箱风控拦截（Unsafe Login）。请先在 163 邮箱网页端完成一次安全验证，"
                        "并在“设置-POP3/SMTP/IMAP”中确认 IMAP 已开启后重试。"
                    )
                raise RuntimeError(f"IMAP 无法选择邮箱目录 `{self._settings.imap_folder}`：{detail}")
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                return []
            raw_uids = data[0].decode("utf-8").split() if data and data[0] else []
            for uid in raw_uids:
                if uid in processed_uids:
                    continue
                fetch_status, fetch_data = client.uid("fetch", uid, "(RFC822)")
                if fetch_status != "OK" or not fetch_data:
                    continue

                payload = fetch_data[0][1] if isinstance(fetch_data[0], tuple) else b""
                if not payload:
                    continue

                msg = email.message_from_bytes(payload)
                sender = parseaddr(msg.get("From", ""))[1].strip().lower()
                subject = self._decode_mime_text(msg.get("Subject", ""))
                body = self._extract_text_body(msg)
                command_text, codes = self._parse_command(body)
                if not codes:
                    continue

                commands.append(
                    PublishCommand(
                        message_uid=uid,
                        sender=sender,
                        subject=subject,
                        body=body[:4000],
                        command_text=command_text,
                        codes=codes,
                    )
                )
        finally:
            try:
                client.close()
            except Exception:
                pass
            client.logout()

        return commands

    def _send_imap_id(self, client: imaplib.IMAP4 | imaplib.IMAP4_SSL) -> None:
        # 163 要求部分客户端在 LOGIN 后带 IMAP ID，否则 SELECT 可能返回 Unsafe Login。
        if "ID" not in imaplib.Commands:
            imaplib.Commands["ID"] = ("AUTH", "SELECTED")

        support_email = (self._settings.mail_from or self._settings.imap_username or "noreply@autosci.local").strip()
        id_args = (
            f'("name" "AutoSci" "version" "1.0.0" '
            f'"vendor" "autosci" "support-email" "{support_email}")'
        )
        try:
            typ, data = client._simple_command("ID", id_args)
            typ, _ = client._untagged_response(typ, data, "ID")
            if typ != "OK":
                raise RuntimeError(f"ID 响应类型={typ}")
        except Exception as exc:
            logger.warning("IMAP ID command failed: %s", exc)
            raise RuntimeError(f"IMAP ID 发送失败：{exc}") from exc

    @staticmethod
    def _decode_imap_error(exc: Exception) -> str:
        if not exc.args:
            return str(exc) or "unknown error"
        head = exc.args[0]
        if isinstance(head, (bytes, bytearray)):
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    return head.decode(enc, errors="ignore")
                except Exception:
                    continue
            return str(head)
        return str(head)

    @staticmethod
    def _decode_imap_payload(payload) -> str:
        if payload is None:
            return ""
        if isinstance(payload, (bytes, bytearray)):
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    return payload.decode(enc, errors="ignore")
                except Exception:
                    continue
            return str(payload)
        if isinstance(payload, (list, tuple)):
            parts = [MailCommandService._decode_imap_payload(item) for item in payload]
            return " | ".join([p for p in parts if p])
        return str(payload)

    def _parse_command(self, body: str) -> tuple[str, list[str]]:
        match = self._command_pattern.search(body)
        if not match:
            return "", []
        command_text = match.group(0).strip()
        raw_codes = match.group(1)
        parts = re.split(r"[，,\s]+", raw_codes)
        codes: list[str] = []
        for part in parts:
            token = part.strip().upper()
            if not token:
                continue
            if not self._paper_code_pattern.match(token):
                continue
            if token not in codes:
                codes.append(token)
        return command_text, codes

    @staticmethod
    def _extract_text_body(msg: Message) -> str:
        if msg.is_multipart():
            candidates: list[str] = []
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type not in {"text/plain", "text/html"}:
                    continue
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="ignore")
                except Exception:
                    text = payload.decode("utf-8", errors="ignore")
                candidates.append(text)
            return "\n".join(candidates)
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except Exception:
            return payload.decode("utf-8", errors="ignore")

    @staticmethod
    def _decode_mime_text(value: str) -> str:
        chunks = decode_header(value)
        parts: list[str] = []
        for chunk, charset in chunks:
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="ignore"))
            else:
                parts.append(chunk)
        return "".join(parts)
