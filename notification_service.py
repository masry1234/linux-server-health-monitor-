"""Environment-driven Telegram and email alert delivery for ServerPulse."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("serverpulse.notifications")


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _alert_text(event: dict) -> str:
    severity = str(event.get("severity", "ALERT")).upper()
    return "\\n".join((
        f"ServerPulse {severity}",
        f"Resource: {event.get('name', 'unknown')}",
        f"Value: {event.get('value', 'N/A')}",
        f"Threshold: {event.get('threshold', 'N/A')}",
        f"Status: {event.get('previous_status', 'UNKNOWN')} -> {event.get('current_status', 'UNKNOWN')}",
        f"Message: {event.get('message', 'No details')}",
        f"Time (UTC): {event.get('timestamp_utc', 'unknown')}",
    ))

def send_telegram(event: dict) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram notification skipped: credentials are not configured")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({"chat_id": chat_id, "text": _alert_text(event)}).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            logger.error("Telegram API rejected notification")
            return False
        return True
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        # Do not log the request URL: Telegram bot tokens are part of that URL.
        logger.error("Telegram notification delivery failed (%s)", type(error).__name__)
        return False


def send_email(event: dict) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", os.getenv("SMTP_USERNAME", "")).strip()
    recipients = [item.strip() for item in os.getenv("SMTP_TO", "").split(",") if item.strip()]
    if not host or not sender or not recipients:
        logger.warning("Email notification skipped: SMTP host, sender, or recipient is missing")
        return False

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "")
        use_ssl = _enabled("SMTP_USE_SSL", port == 465)
        use_tls = _enabled("SMTP_USE_TLS", not use_ssl)
        message = EmailMessage()
        message["Subject"] = (
            f"[ServerPulse] {event.get('severity', 'ALERT')} - "
            f"{event.get('name', 'unknown')}"
        )
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(_alert_text(event))

        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        context = ssl.create_default_context()
        with smtp_class(host, port, timeout=10, **({"context": context} if use_ssl else {})) as server:
            if use_tls and not use_ssl:
                server.starttls(context=context)
            if username:
                server.login(username, password)
            server.send_message(message)
        return True
    except (OSError, ValueError, smtplib.SMTPException) as error:
        logger.error("Email notification delivery failed (%s)", type(error).__name__)
        return False


def dispatch_alert(event: dict) -> dict[str, bool]:
    """Deliver an alert to enabled channels; never raise into the collector."""
    results: dict[str, bool] = {}
    if _enabled("TELEGRAM_ENABLED"):
        results["telegram"] = send_telegram(event)
    if _enabled("EMAIL_ENABLED"):
        results["email"] = send_email(event)
    return results


def dispatch_alert_async(event: dict) -> None:
    """Dispatch without blocking the monitoring collection loop."""
    worker = threading.Thread(
        target=dispatch_alert, args=(event.copy(),), daemon=True,
        name="serverpulse-notification",
    )
    worker.start()
