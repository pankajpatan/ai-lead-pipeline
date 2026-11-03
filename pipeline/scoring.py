"""Turn a qualifier's answers into a tier and score, in code.

The model (or the scripted rules) only answers each criterion with evidence. This module checks
those answers against the enquiry text and applies the business thresholds, so the same answers
always give the same tier.
"""
from __future__ import annotations

import re

from .criteria import Criteria
from .models import CriterionCheck, Qualification, Scored


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


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


def score(q: Qualification, criteria: Criteria, enquiry_text: str) -> Scored:
    q, corrections = validate(q, criteria, enquiry_text)
    if q.is_spam:
        return Scored(tier="spam", score=0, qualification=q, corrections=corrections)

    answers = {c.id: c.met for c in q.checks}
    total = sum(c.weight for c in criteria.criteria if answers[c.id] == "yes")
    if any(c.required and answers[c.id] == "no" for c in criteria.criteria):
        tier = "poor"
    elif total >= criteria.tiers["strong"]:
        tier = "strong"
    elif total >= criteria.tiers["possible"]:
        tier = "possible"
    else:
        tier = "poor"
    return Scored(tier=tier, score=total, qualification=q, corrections=corrections)
