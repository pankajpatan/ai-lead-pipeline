"""Sending approved replies. Only the approve endpoint leads here: nothing is sent automatically."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

log = logging.getLogger("pipeline")


class MailError(RuntimeError):
    pass


@dataclass
class Sent:
    message_id: str
    delivered: bool      # False means recorded in the outbox only (no SMTP configured)
    via: str


class Mailer(Protocol):
    name: str

    def send(self, to: str, subject: str, body: str, from_name: str) -> Sent: ...


def build_message(sender: str, to: str, subject: str, body: str, from_name: str,
                  reply_to: str | None = None) -> EmailMessage:
    # Header values come from the lead and the editor, so strip line breaks (header injection).
    clean = lambda s: " ".join(s.split())  # noqa: E731
    msg = EmailMessage()
    msg["From"] = formataddr((clean(from_name), sender))
    msg["To"] = clean(to)
    msg["Subject"] = clean(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg


class SMTPMailer:
    name = "smtp"

    def __init__(self, host: str, port: int, sender: str, user: str = "", password: str = "",
                 starttls: bool = True, reply_to: str | None = None, timeout: float = 20.0):
        self.host, self.port, self.sender = host, port, sender
        self.user, self.password, self.starttls = user, password, starttls
        self.reply_to, self.timeout = reply_to, timeout

    def send(self, to: str, subject: str, body: str, from_name: str) -> Sent:
        msg = build_message(self.sender, to, subject, body, from_name, self.reply_to)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                refused = smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP {self.host}:{self.port}: {type(exc).__name__}: {exc}"[:300]) from exc
        if refused:
            raise MailError(f"Recipient refused: {refused}"[:300])
        log.info("email sent message_id=%s", msg["Message-ID"])
        return Sent(message_id=msg["Message-ID"], delivered=True, via=f"smtp:{self.host}")


class OutboxMailer:
    """No SMTP configured: keep the message for the record and say plainly that it was not emailed."""

    name = "outbox"

    def __init__(self, sender: str = "demo@localhost"):
        self.sender = sender
        self.messages: list[EmailMessage] = []

    def send(self, to: str, subject: str, body: str, from_name: str) -> Sent:
        msg = build_message(self.sender, to, subject, body, from_name)
        self.messages.append(msg)
        return Sent(message_id=msg["Message-ID"], delivered=False, via="outbox")
