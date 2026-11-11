"""Tests for environment-based notification delivery."""

import os
import unittest
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import notification_service

SAMPLE_ALERT = {
    "timestamp_utc": "2026-11-10T00:00:00+00:00",
    "severity": "CRITICAL",
    "name": "cpu",
    "value": "95.00 %",
    "threshold": "Warning: 70.00 % | Critical: 90.00 %",
    "message": "cpu entered CRITICAL state",
    "previous_status": "OK",
    "current_status": "CRITICAL",
}


class NotificationDispatchTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "TELEGRAM_ENABLED": "true",
        "EMAIL_ENABLED": "true",
    }, clear=True)
    @patch("notification_service.send_email", return_value=True)
    @patch("notification_service.send_telegram", return_value=True)
    def test_dispatches_to_both_enabled_channels(self, telegram, email):
        result = notification_service.dispatch_alert(SAMPLE_ALERT)
        self.assertEqual(result, {"telegram": True, "email": True})
        telegram.assert_called_once_with(SAMPLE_ALERT)
        email.assert_called_once_with(SAMPLE_ALERT)

    @patch.dict(os.environ, {}, clear=True)
    @patch("notification_service.send_email")
    @patch("notification_service.send_telegram")
    def test_disabled_channels_are_not_called(self, telegram, email):
        self.assertEqual(notification_service.dispatch_alert(SAMPLE_ALERT), {})
        telegram.assert_not_called()
        email.assert_not_called()


class TelegramDeliveryTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "12345",
    }, clear=True)
    @patch("notification_service.urlopen")
    def test_sends_form_encoded_message(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        urlopen.return_value = response
        self.assertTrue(notification_service.send_telegram(SAMPLE_ALERT))
        request = urlopen.call_args.args[0]
        self.assertIn("bottest-token/sendMessage", request.full_url)
        self.assertIn(b"chat_id=12345", request.data)
        self.assertIn(b"CRITICAL", request.data)

    @patch.dict(os.environ, {}, clear=True)
    @patch("notification_service.urlopen")
    def test_missing_credentials_skip_delivery(self, urlopen):
        self.assertFalse(notification_service.send_telegram(SAMPLE_ALERT))
        urlopen.assert_not_called()

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "12345",
    }, clear=True)
    @patch("notification_service.urlopen", side_effect=TimeoutError)
    def test_network_failure_returns_false(self, _urlopen):
        self.assertFalse(notification_service.send_telegram(SAMPLE_ALERT))


class EmailDeliveryTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": "alerts@example.test",
        "SMTP_PASSWORD": "secret-test-value",
        "SMTP_FROM": "alerts@example.test",
        "SMTP_TO": "admin@example.test,ops@example.test",
        "SMTP_USE_TLS": "true",
        "SMTP_USE_SSL": "false",
    }, clear=True)
    @patch("notification_service.smtplib.SMTP")
    def test_sends_email_using_starttls(self, smtp_factory):
        server = smtp_factory.return_value.__enter__.return_value
        self.assertTrue(notification_service.send_email(SAMPLE_ALERT))
        smtp_factory.assert_called_once_with("smtp.example.test", 587, timeout=10)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("alerts@example.test", "secret-test-value")
        sent_message = server.send_message.call_args.args[0]
        self.assertIsInstance(sent_message, EmailMessage)
        self.assertEqual(sent_message["To"], "admin@example.test, ops@example.test")
        self.assertIn("CRITICAL", sent_message.get_content())

    @patch.dict(os.environ, {}, clear=True)
    @patch("notification_service.smtplib.SMTP")
    def test_missing_smtp_settings_skip_delivery(self, smtp_factory):
        self.assertFalse(notification_service.send_email(SAMPLE_ALERT))
        smtp_factory.assert_not_called()

    @patch.dict(os.environ, {
        "SMTP_HOST": "smtp.example.test",
        "SMTP_FROM": "alerts@example.test",
        "SMTP_TO": "admin@example.test",
    }, clear=True)
    @patch("notification_service.smtplib.SMTP", side_effect=OSError("offline"))
    def test_smtp_failure_returns_false(self, _smtp_factory):
        self.assertFalse(notification_service.send_email(SAMPLE_ALERT))


if __name__ == "__main__":
    unittest.main()
