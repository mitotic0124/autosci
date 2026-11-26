import unittest
from email import message_from_string
from email.header import decode_header, make_header
from types import SimpleNamespace
from unittest.mock import patch

from app.services.email_service import EmailService
from app.core.config import Settings


class EmailTransportTests(unittest.TestCase):
    def test_port_465_uses_implicit_ssl_without_a_plaintext_timeout(self) -> None:
        service = EmailService(Settings(_env_file=None, smtp_port=465))
        with patch.object(service, "_send_via_starttls") as plain, \
             patch.object(service, "_send_via_ssl", return_value=True) as ssl:
            self.assertTrue(service._deliver("message", "from@example.test", "user", "secret"))
        plain.assert_not_called()
        ssl.assert_called_once()

    def test_automatic_digest_requires_no_reply_and_links_the_public_article(self) -> None:
        service = EmailService(Settings(_env_file=None, smtp_host="smtp.example.test", mail_to="to@example.test", mail_from="from@example.test", workflow_auto_publish_after_run=True, zhihu_auto_publish=True))
        task = SimpleNamespace(paper_code="R001-01", title="Title <unsafe>", status="PUBLISHED", zhihu_url="https://zhuanlan.zhihu.com/p/123")
        with patch.object(service, "_build_markdown_card_attachments", return_value=[]), \
             patch.object(service, "_send_via_starttls", return_value=True) as send:
            self.assertTrue(service.send_paper_code_digest(1, [task]))
        message = message_from_string(send.call_args.args[0])
        body = message.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("官方记录核验并确认公开", body)
        self.assertIn("https://zhuanlan.zhihu.com/p/123", body)
        self.assertIn("&lt;unsafe&gt;", body)

    def test_automatic_digest_does_not_claim_pending_task_is_published(self) -> None:
        service = EmailService(Settings(_env_file=None, smtp_host="smtp.example.test", mail_to="to@example.test", mail_from="from@example.test", workflow_auto_publish_after_run=True, zhihu_auto_publish=True))
        task = SimpleNamespace(paper_code="R001-01", title="Pending", status="PENDING", zhihu_url=None)
        with patch.object(service, "_build_markdown_card_attachments", return_value=[]), \
             patch.object(service, "_send_via_starttls", return_value=True) as send:
            self.assertTrue(service.send_paper_code_digest(1, [task]))
        message = message_from_string(send.call_args.args[0])
        self.assertIn("尚未确认公开", str(make_header(decode_header(message["Subject"]))))
        body = message.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("本邮件不表示已经公开", body)
        self.assertNotIn("阅读已发表文章", body)

    def test_workflow_failure_alert_does_not_require_article_or_browser(self) -> None:
        service = EmailService(Settings(_env_file=None, smtp_host="smtp.example.test", mail_to="to@example.test", mail_from="from@example.test"))
        with patch.object(service, "_send_via_starttls", return_value=False) as starttls, \
             patch.object(service, "_send_via_ssl", return_value=True) as ssl:
            self.assertTrue(service.send_workflow_alert(123, "Model unavailable"))
        starttls.assert_called_once()
        ssl.assert_called_once()
        message = message_from_string(ssl.call_args.args[0])
        self.assertIn("Run #123", message.get_payload(decode=True).decode("utf-8"))

    @staticmethod
    def _service() -> EmailService:
        return EmailService(
            SimpleNamespace(
                smtp_host="smtp.example.test",
                smtp_port=587,
                smtp_use_tls=True,
                mail_to="to@example.test",
            )
        )

    @patch("app.services.email_service.smtplib.SMTP")
    def test_starttls_reports_success_when_only_quit_fails(self, smtp_class) -> None:
        smtp_class.return_value.quit.side_effect = RuntimeError("connection already closed")

        sent = self._service()._send_via_starttls(
            "message",
            "from@example.test",
            "from@example.test",
            "secret",
        )

        self.assertTrue(sent)
        smtp_class.return_value.starttls.assert_called_once_with()
        smtp_class.return_value.login.assert_called_once_with("from@example.test", "secret")
        smtp_class.return_value.sendmail.assert_called_once()
        smtp_class.return_value.close.assert_called_once_with()

    @patch("app.services.email_service.smtplib.SMTP_SSL")
    def test_ssl_reports_transport_failure(self, smtp_class) -> None:
        smtp_class.return_value.login.side_effect = RuntimeError("authentication rejected")

        sent = self._service()._send_via_ssl(
            "message",
            "from@example.test",
            "from@example.test",
            "secret",
        )

        self.assertFalse(sent)
        smtp_class.return_value.sendmail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
