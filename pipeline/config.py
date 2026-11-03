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
        )

    def validate(self) -> None:
        if not self.demo_mode and len(self.admin_password) < 12:
            raise RuntimeError("Set ADMIN_PASSWORD (12+ characters) when DEMO_MODE=false.")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise RuntimeError("LEAD_EFFORT must be low, medium, high, xhigh or max.")
