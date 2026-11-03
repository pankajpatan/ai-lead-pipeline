"""Owner alerts: a strong lead is waiting for approval, or a lead needs attention.

Alerts are best effort. A failed alert is logged and never stops the pipeline.
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx2 as httpx

log = logging.getLogger("pipeline")


class Notifier(Protocol):
    name: str

    def notify(self, text: str) -> bool: ...


class LogNotifier:
    name = "log"

    def __init__(self):
        self.sent: list[str] = []

    def notify(self, text: str) -> bool:
        self.sent.append(text)
        log.info("ALERT %s", text.replace("\n", " | "))
        return True


class SlackNotifier:
    """Slack incoming webhook (also works with tools that accept the same {"text": ...} body)."""

    name = "slack"

    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        if not webhook_url.startswith("https://"):
            raise ValueError("Alert webhook URL must use https")
        self.url = webhook_url
        self.client = client or httpx.Client(timeout=10.0)

    def notify(self, text: str) -> bool:
        try:
            r = self.client.post(self.url, json={"text": text})
            if r.status_code >= 300:
                log.warning("alert webhook returned %s", r.status_code)
                return False
            return True
        except httpx.HTTPError as exc:
            log.warning("alert webhook failed: %s", exc)
            return False


def _plain(text: str, limit: int = 160) -> str:
    """Enquiry text in an alert: one line, short, and no Slack mention or link syntax."""
    text = " ".join(str(text).split())
    text = text.replace("<", "‹").replace(">", "›").replace("@", "(at)")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strong_lead_text(lead: dict, base_url: str) -> str:
    q = lead.get("qualification") or {}
    return (f"New strong lead #{lead['id']}: {_plain(lead['name'], 60)} (score {lead['score']})\n"
            f"{_plain(q.get('summary', ''))}\nReview the draft reply: {base_url}/dashboard#lead-{lead['id']}")


def attention_text(lead: dict, base_url: str) -> str:
    return (f"Lead #{lead['id']} needs attention: {_plain(lead.get('error') or 'unknown error')}\n"
            f"Retry from the dashboard: {base_url}/dashboard#lead-{lead['id']}")
