"""Turn a qualifier's answers into a tier and score, in code.

The model (or the scripted rules) only answers each criterion with evidence. This module checks
those answers against the enquiry text and applies the business thresholds, so the same answers
always give the same tier.
"""
from __future__ import annotations

import re

from .criteria import Criteria
from .models import CriterionCheck, Qualification, Scored


_SAME = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(text: str) -> str:
    """Compare quotes loosely: case, spacing, curly quotes and dash styles don't matter."""
    text = text.translate(_SAME)
    return re.sub(r"\s+", " ", text).strip().strip("\"'.,").lower()


def validate(q: Qualification, criteria: Criteria, enquiry_text: str) -> tuple[Qualification, list[str]]:
    """Make the answers safe to score.

    - Every criterion gets exactly one answer; missing ones become "unknown", extra ones are dropped.
    - A yes or no whose evidence is not a quote from the enquiry becomes "unknown".
    """
    corrections: list[str] = []
    text = _norm(enquiry_text)
    given = {}
    for check in q.checks:
        if criteria.get(check.id) is None:
            corrections.append(f"dropped unknown criterion '{check.id}'")
        elif check.id in given:
            corrections.append(f"dropped repeated answer for '{check.id}'")
        else:
            given[check.id] = check

    checks = []
    for c in criteria.criteria:
        check = given.get(c.id)
        if check is None:
            corrections.append(f"'{c.id}' was not answered; set to unknown")
            check = CriterionCheck(id=c.id, met="unknown")
        elif check.met != "unknown":
            evidence = _norm(check.evidence or "")
            if not evidence or evidence not in text:
                corrections.append(f"'{c.id}' said {check.met} without a quote from the enquiry; set to unknown")
                check = CriterionCheck(id=c.id, met="unknown")
        checks.append(check)
    return q.model_copy(update={"checks": checks}), corrections


def tier_for(answers: dict[str, str], is_spam: bool, criteria: Criteria) -> tuple[str, int]:
    """The business rules: (tier, score) for a set of yes/no/unknown answers."""
    if is_spam:
        return "spam", 0
    total = sum(c.weight for c in criteria.criteria if answers.get(c.id) == "yes")
    if any(c.required and answers.get(c.id) == "no" for c in criteria.criteria):
        return "poor", total
    if total >= criteria.tiers["strong"]:
        return "strong", total
    if total >= criteria.tiers["possible"]:
        return "possible", total
    return "poor", total


def score(q: Qualification, criteria: Criteria, enquiry_text: str) -> Scored:
    q, corrections = validate(q, criteria, enquiry_text)
    tier, total = tier_for({c.id: c.met for c in q.checks}, q.is_spam, criteria)
    return Scored(tier=tier, score=total, qualification=q, corrections=corrections)
