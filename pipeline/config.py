"""Settings from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    demo_mode: bool = True
    database_path: str = "leads.db"
    criteria_path: str = "criteria.yaml"
    admin_password: str = ""
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "low"
    hubspot_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = ""
    reply_to: str = ""
    alert_webhook_url: str = ""
    public_url: str = "http://127.0.0.1:8020"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            demo_mode=_bool(os.getenv("DEMO_MODE"), True),
            database_path=os.getenv("DATABASE_PATH", "leads.db"),
            criteria_path=os.getenv("CRITERIA_PATH", "criteria.yaml"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=os.getenv("LEAD_MODEL", "claude-opus-5-5"),
            effort=os.getenv("LEAD_EFFORT", "low"),
            hubspot_token=os.getenv("HUBSPOT_TOKEN", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587") or 587),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_starttls=_bool(os.getenv("SMTP_STARTTLS"), True),
            mail_from=os.getenv("MAIL_FROM", ""),
            reply_to=os.getenv("REPLY_TO", ""),
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
            public_url=os.getenv("PUBLIC_URL", "http://127.0.0.1:8020"),
        )

    def validate(self) -> None:
        if not self.demo_mode and len(self.admin_password) < 12:
            raise RuntimeError("Set ADMIN_PASSWORD (12+ characters) when DEMO_MODE=false.")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise RuntimeError("LEAD_EFFORT must be low, medium, high, xhigh or max.")
        if self.smtp_host and "@" not in self.mail_from:
            raise RuntimeError("Set MAIL_FROM (the sender address) when SMTP_HOST is set.")
        if self.alert_webhook_url and not self.alert_webhook_url.startswith("https://"):
            raise RuntimeError("ALERT_WEBHOOK_URL must start with https://")
