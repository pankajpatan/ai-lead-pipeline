"""Qualifiers answer each criterion for an enquiry.

ScriptedQualifier uses fixed rules so the demo runs with no API key. The Claude qualifier
(added in step 2) returns the same Qualification shape, and both go through scoring.validate().
"""
from __future__ import annotations

import re
from typing import Protocol

from .criteria import Criteria
from .models import CriterionCheck, Enquiry, Extracted, Qualification


class QualifierError(RuntimeError):
    """The qualifier could not produce an answer. The lead goes to needs_attention."""


class Qualifier(Protocol):
    name: str

    def qualify(self, enquiry: Enquiry, criteria: Criteria) -> Qualification: ...


# 1,234,567 or 1234 (a trailing comma, as in "$800, no rush", is not part of the number)
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_USD = re.compile(
    rf"(?:\$\s?|usd\s?)({_NUM})\s?(k\b)?|({_NUM})\s?(k\b)?\s?(?:usd|dollars)\b",
    re.IGNORECASE,
)
_OTHER_CURRENCY = re.compile(rf"(?:₹|rs\.?\s?|inr\s?|€|£|eur\s?|gbp\s?)\s?{_NUM}\s?(?:k|lakh|lakhs)?", re.IGNORECASE)
_SOON = re.compile(r"\b(asap|as soon as possible|immediately|urgent(?:ly)?|right away|this month|next month)\b", re.IGNORECASE)
_WITHIN = re.compile(r"\b(?:within|in|next|start in|over the next)\s+(\d+|a|one|two|three|four|six)\s+(weeks?|months?)\b", re.IGNORECASE)
_LATER = re.compile(r"\b(next year|no rush|no hurry|sometime later|in a year|later this year|just exploring)\b", re.IGNORECASE)
_OWNER = re.compile(
    r"\b(?:i am|i'm|im|as)\s+(?:the\s+|a\s+)?(owner|founder|co-founder|cofounder|ceo|director|managing director|partner)\b"
    r"|\b(?:my|our)\s+(?:own\s+)?(business|company|startup|shop|store|clinic|restaurant|agency|firm)\b",
    re.IGNORECASE,
)
_NOT_OWNER = re.compile(
    r"\b(my boss|my manager|on behalf of|for my client|need to check with|get approval|getting quotes for)\b",
    re.IGNORECASE,
)
_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "six": 6}

QUESTIONS = {
    "service_in_scope": "What would you like us to build (website, web app, mobile app or online store)?",
    "budget": "Do you have a budget range in mind for this project?",
    "timeline": "When would you like to start, and is there a deadline?",
    "decision_maker": "Who else is involved in choosing the agency for this project?",
}


def _amount(number: str, k: str | None) -> int:
    value = float(number.replace(",", ""))
    return int(value * 1000 if k else value)


def _find_budget(text: str) -> tuple[int | None, str | None, str | None]:
    """Return (largest USD amount, the matched text, other-currency text)."""
    best, quote = None, None
    for m in _USD.finditer(text):
        value = _amount(m.group(1), m.group(2)) if m.group(1) else _amount(m.group(3), m.group(4))
        if best is None or value > best:
            best, quote = value, m.group(0).strip()
    other = _OTHER_CURRENCY.search(text)
    return best, quote, other.group(0).strip() if other else None


def _find_timeline(text: str) -> tuple[float | None, str | None]:
    """Return (months until start, matched text). 12 means 'later than we care about'."""
    if m := _SOON.search(text):
        return 0.5, m.group(0)
    if m := _WITHIN.search(text):
        count = _WORDS.get(m.group(1).lower()) or int(m.group(1))
        months = count / 4 if m.group(2).lower().startswith("week") else count
        return months, m.group(0)
    if m := _LATER.search(text):
        return 12, m.group(0)
    return None, None


class ScriptedQualifier:
    """Keyword rules. Good enough for a demo; misses anything phrased differently."""

    name = "scripted"

    def qualify(self, enquiry: Enquiry, criteria: Criteria) -> Qualification:
        text = enquiry.message
        lower = text.lower()
        extracted = Extracted(company=enquiry.company)

        if any(phrase in lower for phrase in criteria.spam_phrases):
            return Qualification(
                checks=[CriterionCheck(id=c.id, met="unknown") for c in criteria.criteria],
                extracted=extracted,
                is_spam=True,
                summary="Looks like a sales pitch or spam, not a project enquiry.",
            )

        checks: dict[str, CriterionCheck] = {}

        service_hit = next(
            ((service, word) for service, words in criteria.services.items() for word in words
             if re.search(rf"\b{re.escape(word)}\b", lower)),
            None,
        )
        if service_hit:
            extracted.service = service_hit[0]
            checks["service_in_scope"] = CriterionCheck(id="service_in_scope", met="yes", evidence=service_hit[1])

        budget_rule = criteria.get("budget")
        usd, quote, other = _find_budget(text)
        if usd is not None:
            extracted.budget_usd, extracted.budget_text = usd, quote
            minimum = budget_rule.min_budget_usd if budget_rule and budget_rule.min_budget_usd else 0
            checks["budget"] = CriterionCheck(id="budget", met="yes" if usd >= minimum else "no", evidence=quote)
        elif other:
            extracted.budget_text = other  # not converted; a person or Claude decides

        timeline_rule = criteria.get("timeline")
        months, phrase = _find_timeline(text)
        if months is not None:
            extracted.timeline = phrase
            limit = timeline_rule.max_start_months if timeline_rule and timeline_rule.max_start_months else 3
            checks["timeline"] = CriterionCheck(id="timeline", met="yes" if months <= limit else "no", evidence=phrase)

        if m := _NOT_OWNER.search(text):
            checks["decision_maker"] = CriterionCheck(id="decision_maker", met="no", evidence=m.group(0))
        elif m := _OWNER.search(text):
            extracted.role = m.group(1) or "owner"
            checks["decision_maker"] = CriterionCheck(id="decision_maker", met="yes", evidence=m.group(0))

        answers = [checks.get(c.id, CriterionCheck(id=c.id, met="unknown")) for c in criteria.criteria]
        missing = [QUESTIONS.get(a.id, f"Can you tell us more about: {a.id}?") for a in answers if a.met == "unknown"]
        budget_part = f"budget {extracted.budget_text}" if extracted.budget_text else "no budget given"
        summary = (
            f"{enquiry.name} asks about {extracted.service or 'unclear work'}; {budget_part}; "
            f"{'timeline: ' + extracted.timeline if extracted.timeline else 'no timeline given'}."
        )
        return Qualification(checks=answers, extracted=extracted, missing_info=missing, summary=summary)
