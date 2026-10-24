"""SMTP email delivery."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from eventscout.config import EmailConfig


def send_email(cfg: EmailConfig, subject: str, html: str, text: str, attachments: list[tuple[str, bytes, str]]) -> None:
    if not (cfg.smtp_host and cfg.sender and cfg.recipients):
        raise ValueError("email.smtp_host, email.sender and email.recipients must be set")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for filename, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    context = ssl.create_default_context()
    if cfg.use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    with server:
        if cfg.starttls and not cfg.use_ssl:
            server.starttls(context=context)
        if cfg.username and cfg.password:
            server.login(cfg.username, cfg.password)
        server.send_message(msg)
