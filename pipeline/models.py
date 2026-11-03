"""Data shapes shared by intake, qualifiers and storage."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Answer = Literal["yes", "no", "unknown"]
Tier = Literal["strong", "possible", "poor", "spam"]


class Enquiry(BaseModel):
    """What a contact form (or another source) sends to POST /intake."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: str | None = Field(default=None, max_length=160)
    message: str = Field(min_length=10, max_length=5000)
    source: str = Field(default="web_form", max_length=40)
    # Set by form tools that retry deliveries (Typeform, Tally, n8n). Used to drop repeats.
    submission_id: str | None = Field(default=None, max_length=120)
    # Honeypot: hidden on the form, so only bots fill it.
    website: str | None = Field(default=None, max_length=200)


class CriterionCheck(BaseModel):
    id: str
    met: Answer
    # A short quote from the enquiry that supports the answer. Required for yes or no.
    evidence: str | None = None


class Extracted(BaseModel):
    service: str | None = None
    budget_usd: int | None = None
    budget_text: str | None = None
    timeline: str | None = None
    company: str | None = None
    role: str | None = None


class Qualification(BaseModel):
    """What a qualifier returns. Tier and score are computed from it in code."""

    checks: list[CriterionCheck]
    extracted: Extracted = Extracted()
    is_spam: bool = False
    missing_info: list[str] = []
    summary: str = ""
    next_action: str = ""
    # Model, request ID, tokens and time, when an API produced the answer.
    meta: dict[str, Any] = {}


class Scored(BaseModel):
    tier: Tier
    score: int
    qualification: Qualification
    # Answers the code changed, for example a "yes" whose evidence is not in the enquiry.
    corrections: list[str] = []
