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
import threading

from .alerts import Notifier, attention_text, strong_lead_text
from .crm import CRM, SyncState
from .criteria import Criteria
from .drafts import Draft, Drafter, check_draft
from .mailer import Mailer
from .models import Enquiry
from .qualifier import Qualifier
from .scoring import score
from .store import InvalidTransition, Store

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
                 crm: CRM | None = None, drafter: Drafter | None = None,
                 mailer: Mailer | None = None, notifier: Notifier | None = None,
                 base_url: str = "http://127.0.0.1:8020"):
        self.store = store
        self.qualifier = qualifier
        self.criteria = criteria
        self.crm = crm
        self.drafter = drafter
        self.mailer = mailer
        self.notifier = notifier
        self.base_url = base_url.rstrip("/")
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _alert(self, lead_id: int, text_for) -> None:
        if self.notifier is None:
            return
        lead = self.store.get(lead_id)
        try:
            ok = self.notifier.notify(text_for(lead, self.base_url))
        except Exception:  # alerts must never break the pipeline
            log.exception("alert failed lead_id=%s", lead_id)
            ok = False
        self.store.log(lead_id, "alert_sent" if ok else "alert_failed", self.notifier.name)

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
        """Run every remaining step for a lead, stopping at the first failure.

        It stops at draft_ready: sending happens only after a person approves.
        """
        with self._locks_guard:
            lock = self._locks.setdefault(lead_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return  # another thread is already moving this lead along
        try:
            self._process(lead_id)
        finally:
            lock.release()

    def _process(self, lead_id: int) -> None:
        steps = {"received": self.qualify, "qualified": self.sync_crm, "crm_synced": self.draft,
                 "approved": self.send}
        while True:
            lead = self.store.get(lead_id)
            step = steps.get(lead["state"]) if lead else None
            if step is None or (step is not self.qualify and self._missing(step)):
                return
            if not step(lead_id):
                return

    def _missing(self, step) -> bool:
        return ((step == self.sync_crm and self.crm is None) or (step == self.draft and self.drafter is None)
                or (step == self.send and self.mailer is None))

    def _fail(self, lead_id: int, step: str, resume: str, exc: Exception, attempt: int | None = None) -> bool:
        log.exception("%s failed lead_id=%s", step, lead_id)
        self.store.transition(lead_id, "needs_attention", {"step": step, "attempt": attempt},
                              error=f"{step}: {type(exc).__name__}: {exc}"[:500], resume_state=resume)
        self._alert(lead_id, attention_text)
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
        if lead["tier"] == "strong":
            self._alert(lead_id, strong_lead_text)
        return False  # wait here for a person

    # ---------- human decisions ----------

    def approve(self, lead_id: int, subject: str, body: str, confirm_warnings: bool = False,
                approver: str = "admin") -> dict:
        """Approve the (possibly edited) draft. Returns {"ok", "warnings"}.

        Edited text is checked again. If it has warnings, approval needs confirm_warnings=True,
        so nobody sends a flagged reply by accident.
        """
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "draft_ready":
            raise InvalidTransition("Only a lead with a draft waiting can be approved")
        original = lead["draft"] or {}
        final = check_draft(Draft(subject=subject.strip(), body=body.strip(), drafter=original.get("drafter", "")),
                            lead, self.criteria)
        if final.warnings and not confirm_warnings:
            return {"ok": False, "warnings": final.warnings}
        edited = (final.subject, final.body) != (original.get("subject"), original.get("body"))
        draft = {**original, "subject": final.subject, "body": final.body, "warnings": final.warnings,
                 "edited": edited, "approved_by": approver}
        self.store.transition(lead_id, "approved",
                              {"by": approver, "edited": edited, "warnings_confirmed": final.warnings},
                              draft=draft)
        return {"ok": True, "warnings": final.warnings}

    def reject(self, lead_id: int, reason: str, approver: str = "admin") -> None:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] not in ("draft_ready", "needs_attention", "qualified"):
            raise InvalidTransition("This lead can't be rejected now")
        self.store.transition(lead_id, "rejected", {"by": approver, "reason": reason[:300]}, resume_state=None)

    def send(self, lead_id: int) -> bool:
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "approved" or self.mailer is None:
            return False
        draft = dict(lead["draft"])
        if not draft.get("message_id"):  # not sent yet; a retry after a crash must not send twice
            try:
                sent = self.mailer.send(lead["email"], draft["subject"], draft["body"],
                                        self.criteria.reply.sender_name)
            except Exception as exc:
                return self._fail(lead_id, "send", "approved", exc)
            draft.update(message_id=sent.message_id, delivered=sent.delivered, via=sent.via)
            self.store.update(lead_id, draft=draft)
        if self.crm is not None:
            try:  # the email is what matters; a failed CRM log is recorded, not fatal
                self.crm.log_reply(lead, draft["subject"], draft["body"], draft["delivered"])
                self.store.log(lead_id, "crm_reply_logged")
            except Exception as exc:
                log.warning("CRM reply log failed lead_id=%s: %s", lead_id, exc)
                self.store.log(lead_id, "crm_reply_log_failed", str(exc)[:300])
        self.store.transition(lead_id, "sent", {"via": draft["via"], "delivered": draft["delivered"],
                                                "message_id": draft["message_id"]}, error=None, resume_state=None)
        return False

    AUTOMATIC_STATES = ("received", "qualified", "crm_synced", "approved")

    def resume_pending(self) -> list[int]:
        """After a restart, finish leads that were mid-pipeline when the process stopped.

        Safe to repeat: each step checks its state, CRM IDs are saved as they are created,
        and a reply whose message ID is saved is never sent again. Leads waiting for a person
        (draft_ready, needs_attention) are left alone.
        """
        pending = [l["id"] for state in self.AUTOMATIC_STATES for l in self.store.leads(state)]
        for lead_id in sorted(pending):
            self.store.log(lead_id, "resumed_after_restart")
            self.process(lead_id)
        return sorted(pending)

    def retry(self, lead_id: int) -> bool:
        """Resume a needs_attention lead from the step that failed."""
        lead = self.store.get(lead_id)
        if lead is None or lead["state"] != "needs_attention":
            return False
        resume = lead.get("resume_state") or "received"
        self.store.transition(lead_id, resume, "manual retry", error=None)
        self.process(lead_id)
        return True
