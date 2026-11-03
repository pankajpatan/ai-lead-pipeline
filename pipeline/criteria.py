"""Load the business rules from criteria.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class Criterion(BaseModel):
    id: str = Field(pattern=r"^[a-z_]+$")
    question: str
    weight: int = Field(ge=0, le=100)
    required: bool = False
    min_budget_usd: int | None = None
    max_start_months: int | None = None


class Criteria(BaseModel):
    business: str
    description: str = ""
    services: dict[str, list[str]] = {}
    criteria: list[Criterion]
    tiers: dict[str, int]
    spam_phrases: list[str] = []

    @model_validator(mode="after")
    def check(self) -> "Criteria":
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion ids must be unique")
        if not {"strong", "possible"} <= set(self.tiers):
            raise ValueError("tiers needs 'strong' and 'possible' thresholds")
        if self.tiers["strong"] < self.tiers["possible"]:
            raise ValueError("strong threshold must be at least the possible threshold")
        return self

    def get(self, criterion_id: str) -> Criterion | None:
        return next((c for c in self.criteria if c.id == criterion_id), None)


def load_criteria(path: str | Path) -> Criteria:
    with open(path, encoding="utf-8") as f:
        return Criteria.model_validate(yaml.safe_load(f))
