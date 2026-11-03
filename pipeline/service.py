"""The pipeline steps. Each step moves a lead to its next state or to needs_attention.

received --qualify--> qualified --crm--> crm_synced --draft--> draft_ready
Spam stops after qualify (rejected: no CRM record, no reply).
A failed step records where to resume, so a retry never repeats finished work.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re

from .crm import CRM, SyncState
from .criteria import Criteria
from .drafts import Drafter, check_draft
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
    def __init__(self, store: Store, qualifier: Qualifier, criteria: Criteria,
                 crm: CRM | None = None, drafter: Drafter | None = None):
        self.store = store
        self.qualifier = qualifier
        self.criteria = criteria
        self.crm = crm
        self.drafter = drafter

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

    def process(self, lead_id: int) -> None:
        """Run every remaining step for a lead, stopping at the first failure."""
        steps = {"received": self.qualify, "qualified": self.sync_crm, "crm_synced": self.draft}
        while True:
            lead = self.store.get(lead_id)
            step = steps.get(lead["state"]) if lead else None
            if step is None or (step is not self.qualify and self._missing(step)):
                return
            if not step(lead_id):
                return

    def _missing(self, step) -> bool:
        return (step == self.sync_crm and self.crm is None) or (step == self.draft and self.drafter is None)

    def _fail(self, lead_id: int, step: str, resume: str, exc: Exception, attempt: int | None = None) -> bool:
        log.exception("%s failed lead_id=%s", step, lead_id)
        self.store.transition(lead_id, "needs_attention", {"step": step, "attempt": attempt},
                              error=f"{step}: {type(exc).__name__}: {exc}"[:500], resume_state=resume)
        return False

    # ---------- steps: each returns True when the lead moved on ----------

    def qualify(self, lead_id: int) -> bool:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "received":
            return False
        attempt = self.store.bump_attempts(lead_id)
        enquiry = Enquiry(name=lead["name"], email=lead["email"], company=lead["company"],
                          message=lead["message"], source=lead["source"])
        try:
            answer = self.qualifier.qualify(enquiry, self.criteria)
            scored = score(answer, self.criteria, enquiry.message)
        except Exception as exc:  # any failure must leave the lead visible, never lost
            return self._fail(lead_id, "qualify", "received", exc, attempt)

        q = scored.qualification
        if not q.next_action:
            q = q.model_copy(update={"next_action": NEXT_ACTION[scored.tier]})
        detail = {"tier": scored.tier, "score": scored.score}
        if scored.corrections:
            detail["corrections"] = scored.corrections
        self.store.transition(
            lead_id, "qualified", detail,
            tier=scored.tier, score=scored.score, qualifier=self.qualifier.name,
            qualification=q.model_dump(), error=None, resume_state=None,
        )
        log.info("qualified lead_id=%s tier=%s score=%s", lead_id, scored.tier, scored.score)
        if scored.tier == "spam":
            self.store.transition(lead_id, "rejected", "spam: no CRM record and no reply")
            return False
        return True

    def sync_crm(self, lead_id: int) -> bool:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "qualified" or self.crm is None:
            return False
        state = SyncState(**(lead.get("crm") or {}))

        def save(s: SyncState) -> None:  # progress survives a failure halfway through
            self.store.update(lead_id, crm=dataclasses.asdict(s))

        try:
            state = self.crm.sync(lead, state, save)
        except Exception as exc:
            return self._fail(lead_id, "crm", "qualified", exc)
        self.store.transition(lead_id, "crm_synced",
                              {"crm": self.crm.name, "contact_id": state.contact_id,
                               "new_contact": state.created_contact, "deal_id": state.deal_id},
                              crm=dataclasses.asdict(state), error=None, resume_state=None)
        return True

    def draft(self, lead_id: int) -> bool:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "crm_synced" or self.drafter is None:
            return False
        try:
            draft = check_draft(self.drafter.draft(lead, self.criteria), lead, self.criteria)
        except Exception as exc:
            return self._fail(lead_id, "draft", "crm_synced", exc)
        self.store.transition(lead_id, "draft_ready", {"drafter": draft.drafter, "warnings": draft.warnings},
                              draft=draft.model_dump(), error=None, resume_state=None)
        return True

    def retry(self, lead_id: int) -> bool:
        """Resume a needs_attention lead from the step that failed."""
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "needs_attention":
            return False
        resume = lead.get("resume_state") or "received"
        self.store.transition(lead_id, resume, "manual retry", error=None)
        self.process(lead_id)
        return True
