"""Email delivery for the daily report.

Provider is selected by config.EMAIL_PROVIDER ("gmail" | "sendgrid"). Gmail uses
stdlib smtplib + an app password; SendGrid uses its HTTP API. Recipients come
from REPORT_EMAIL_TO (comma-separated).
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

log = logging.getLogger(__name__)


def _recipients() -> list[str]:
    raw = config.REPORT_EMAIL_TO or ""
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_report(html_content: str, subject: str) -> None:
    """Dispatch the report via the configured provider."""
    recipients = _recipients()
    if not recipients:
        raise RuntimeError("REPORT_EMAIL_TO is not set; nowhere to send the report.")

    provider = config.EMAIL_PROVIDER
    log.info("Sending report to %d recipient(s) via %s", len(recipients), provider)
    if provider == "sendgrid":
        send_via_sendgrid(html_content, subject, recipients)
    elif provider == "gmail":
        send_via_gmail(html_content, subject, recipients)
    else:
        raise RuntimeError(f"Unknown EMAIL_PROVIDER: {provider!r}")


def send_via_gmail(html_content: str, subject: str, recipients: list[str]) -> None:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError("Gmail credentials (GMAIL_ADDRESS/GMAIL_APP_PASSWORD) missing.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("Your email client does not support HTML.", "plain"))
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.sendmail(config.GMAIL_ADDRESS, recipients, msg.as_string())
    log.info("Report sent via Gmail")


def send_via_sendgrid(html_content: str, subject: str, recipients: list[str]) -> None:
    if not config.SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY missing.")
    from_email = config.SENDGRID_FROM_EMAIL or config.GMAIL_ADDRESS
    if not from_email:
        raise RuntimeError("SENDGRID_FROM_EMAIL (verified sender) missing.")

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=from_email,
        to_emails=recipients,
        subject=subject,
        html_content=html_content,
    )
    resp = SendGridAPIClient(config.SENDGRID_API_KEY).send(message)
    log.info("Report sent via SendGrid (status %s)", resp.status_code)
