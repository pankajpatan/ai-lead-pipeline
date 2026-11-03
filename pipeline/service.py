"""The pipeline steps. Each step moves a lead to its next state or to needs_attention."""
from __future__ import annotations

import hashlib
import logging
import re

from .criteria import Criteria
from .models import Enquiry
from .qualifier import Qualifier
from .scoring import score
from .store import Store

log = logging.getLogger("pipeline")

NEXT_ACTION = {
    "strong": "Reply today with a booking link for a call.",
    "possible": "Reply with the missing questions before booking a call.",
    "poor": "Send a polite decline, or a referral if one fits.",
    "spam": "No reply.",
}


def idempotency_key(enquiry: Enquiry) -> str:
    """Same submission ID, or same email and message, means the same enquiry."""
    if enquiry.submission_id:
        basis = f"{enquiry.source}:{enquiry.submission_id}"
    else:
        message = re.sub(r"\s+", " ", enquiry.message).strip().lower()
        basis = f"{enquiry.email.lower()}:{message}"
    return hashlib.sha256(basis.encode()).hexdigest()


class Pipeline:
    def __init__(self, store: Store, qualifier: Qualifier, criteria: Criteria):
        self.store = store
        self.qualifier = qualifier
        self.criteria = criteria

    def intake(self, enquiry: Enquiry) -> tuple[int, str]:
        """Save the enquiry. Returns (lead_id, outcome): 'new', 'duplicate' or 'rejected'."""
        lead_id, created = self.store.add_lead(
            idempotency_key(enquiry), enquiry.source, enquiry.name, enquiry.email.lower(),
            enquiry.company, enquiry.message,
        )
        if not created:
            log.info("duplicate delivery lead_id=%s", lead_id)
            return lead_id, "duplicate"
        if enquiry.website:
            self.store.transition(lead_id, "rejected", "honeypot field was filled", tier="spam", score=0)
            return lead_id, "rejected"
        return lead_id, "new"

    def qualify(self, lead_id: int) -> None:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "received":
            return
        attempt = self.store.bump_attempts(lead_id)
        enquiry = Enquiry(name=lead["name"], email=lead["email"], company=lead["company"],
                          message=lead["message"], source=lead["source"])
        try:
            answer = self.qualifier.qualify(enquiry, self.criteria)
            scored = score(answer, self.criteria, enquiry.message)
        except Exception as exc:  # any failure must leave the lead visible, never lost
            log.exception("qualification failed lead_id=%s attempt=%s", lead_id, attempt)
            self.store.transition(lead_id, "needs_attention", {"step": "qualify", "attempt": attempt},
                                  error=f"{type(exc).__name__}: {exc}"[:500])
            return

        q = scored.qualification
        if not q.next_action:
            q = q.model_copy(update={"next_action": NEXT_ACTION[scored.tier]})
        detail = {"tier": scored.tier, "score": scored.score}
        if scored.corrections:
            detail["corrections"] = scored.corrections
        self.store.transition(
            lead_id, "qualified", detail,
            tier=scored.tier, score=scored.score, qualifier=self.qualifier.name,
            qualification=q.model_dump(), error=None,
        )
        log.info("qualified lead_id=%s tier=%s score=%s", lead_id, scored.tier, scored.score)

    def retry(self, lead_id: int) -> bool:
        """Send a needs_attention lead back through the step that failed. Step 1 only has qualify."""
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "needs_attention":
            return False
        self.store.transition(lead_id, "received", "manual retry", error=None)
        self.qualify(lead_id)
        return True
