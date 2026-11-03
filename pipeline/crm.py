"""CRM sync. HubSpot first, behind a small interface so other CRMs can be added.

Each sync step stores the ID it created (contact, note, deal) on the lead before moving on,
so a retry after a failure continues where it stopped instead of creating duplicates.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx2 as httpx

log = logging.getLogger("pipeline")

API = "https://api.hubapi.com"
VERSION = "2026-09"            # HubSpot date-based API version, checked 2026-11-03
NOTE_TO_CONTACT = 202          # HUBSPOT_DEFINED association type IDs, checked live
DEAL_TO_CONTACT = 3
LEAD_STATUS = {"strong": "NEW", "possible": "NEW", "poor": "UNQUALIFIED"}
CUSTOM_PROPERTIES = [
    {
        "name": "lead_fit_tier", "label": "Lead fit tier", "type": "enumeration", "fieldType": "select",
        "groupName": "contactinformation",
        "description": "Set by the AI lead pipeline from the business's qualification rules.",
        "options": [{"label": t.title(), "value": t, "displayOrder": i}
                    for i, t in enumerate(("strong", "possible", "poor"))],
    },
    {
        "name": "lead_fit_score", "label": "Lead fit score", "type": "number", "fieldType": "number",
        "groupName": "contactinformation",
        "description": "0-100: sum of the weights of criteria answered yes.",
    },
]


class CRMError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class SyncState:
    """IDs already created for this lead. Filled in as each step succeeds."""
    contact_id: str | None = None
    note_id: str | None = None
    deal_id: str | None = None
    contact_url: str | None = None
    created_contact: bool | None = None
    steps: list[str] = field(default_factory=list)


class CRM(Protocol):
    name: str

    def sync(self, lead: dict, state: SyncState, save: Callable[[SyncState], None]) -> SyncState: ...

    def log_reply(self, lead: dict, subject: str, body: str, delivered: bool) -> None: ...


def reply_note_html(subject: str, body: str, delivered: bool) -> str:
    e = html.escape
    head = "Reply sent" if delivered else "Reply approved (recorded only, not emailed)"
    return (f"<p><b>{head}: {e(subject)}</b></p>"
            f"<p>{e(body).replace(chr(10), '<br>')}</p><p><i>Approved in the lead pipeline.</i></p>")


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full.strip(), "")


def note_html(lead: dict) -> str:
    """Note body for the contact timeline. Every value from the enquiry is escaped."""
    q = lead["qualification"]
    e = html.escape
    rows = "".join(
        f"<li><b>{e(c['id'])}</b>: {e(c['met'])}"
        + (f' (&ldquo;{e(c["evidence"])}&rdquo;)' if c.get("evidence") else "") + "</li>"
        for c in q["checks"]
    )
    missing = "".join(f"<li>{e(m)}</li>" for m in q.get("missing_info", []))
    model = q.get("meta", {}).get("model")
    return (
        f"<p><b>Website enquiry: {e(lead['tier'] or '')} fit, score {lead['score']}/100</b></p>"
        f"<p>{e(q.get('summary', ''))}</p>"
        f"<p><b>Criteria</b></p><ul>{rows}</ul>"
        + (f"<p><b>Still to ask</b></p><ul>{missing}</ul>" if missing else "")
        + f"<p><b>Next step:</b> {e(q.get('next_action', ''))}</p>"
        f"<p><b>Original message</b></p><blockquote>{e(lead['message'])}</blockquote>"
        f"<p><i>Qualified by {e(lead.get('qualifier') or 'unknown')}"
        + (f" ({e(model)})" if model else "") + f", pipeline lead #{lead['id']}.</i></p>"
    )


class HubSpotCRM:
    name = "hubspot"

    def __init__(self, token: str, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = 4, create_deals: bool = True):
        self.client = client or httpx.Client(base_url=API, timeout=20.0)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.create_deals = create_deals
        self._properties_ready = False
        self._setup_lock = threading.Lock()
        self._ui: tuple[str, int] | None = None

    # ---------- HTTP with retries ----------

    def _request(self, method: str, path: str, json: Any = None, ok_missing: bool = False) -> dict | None:
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.client.request(method, path, json=json, headers=self.headers)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise CRMError(f"HubSpot unreachable: {exc}", retryable=True) from exc
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 404 and ok_missing:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_attempts:
                    raise CRMError(f"HubSpot {r.status_code} after {attempt} attempts", retryable=True)
                wait = r.headers.get("Retry-After")
                self.sleep(min(float(wait), 60) if wait and wait.replace(".", "").isdigit() else min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                try:
                    message = r.json().get("message", r.text)
                except ValueError:
                    message = r.text
                raise CRMError(f"HubSpot {r.status_code} on {method} {path.split('?')[0]}: {message}"[:400],
                               status=r.status_code)
            return r.json() if r.content else {}
        raise AssertionError("unreachable")

    # ---------- setup ----------

    def ensure_properties(self) -> None:
        """Create the fit tier and score properties once, if the account doesn't have them.

        Leads are processed in parallel, so only one thread checks; and a 409 "already exists"
        (another server got there first) counts as success.
        """
        with self._setup_lock:
            if self._properties_ready:
                return
            for prop in CUSTOM_PROPERTIES:
                found = self._request("GET", f"/crm/properties/{VERSION}/contacts/{prop['name']}", ok_missing=True)
                if found is None:
                    try:
                        self._request("POST", f"/crm/properties/{VERSION}/contacts", json=prop)
                        log.info("created HubSpot contact property %s", prop["name"])
                    except CRMError as exc:
                        if exc.status != 409:
                            raise
            self._properties_ready = True

    def _contact_url(self, contact_id: str) -> str | None:
        try:
            if self._ui is None:
                d = self._request("GET", "/account-info/v3/details") or {}
                self._ui = (d["uiDomain"], d["portalId"])
            return f"https://{self._ui[0]}/contacts/{self._ui[1]}/record/0-1/{contact_id}"
        except (CRMError, KeyError):
            return None  # a link is nice to have; never fail the sync for it

    # ---------- sync ----------

    def sync(self, lead: dict, state: SyncState, save: Callable[[SyncState], None]) -> SyncState:
        self.ensure_properties()
        q = lead["qualification"]

        if state.contact_id is None:  # upsert is idempotent, but skip it if already done
            first, last = split_name(lead["name"])
            props = {
                "email": lead["email"],
                "firstname": first,
                "lastname": last,
                "lead_fit_tier": lead["tier"],
                "lead_fit_score": str(lead["score"]),
                "hs_lead_status": LEAD_STATUS.get(lead["tier"], "NEW"),
            }
            company = lead.get("company") or q.get("extracted", {}).get("company")
            if company:
                props["company"] = company
            body = {"inputs": [{"idProperty": "email", "id": lead["email"], "properties": props,
                                "objectWriteTraceId": f"lead-{lead['id']}"}]}
            res = self._request("POST", f"/crm/objects/{VERSION}/contacts/batch/upsert", json=body) or {}
            if res.get("errors") or not res.get("results"):
                raise CRMError(f"HubSpot contact upsert failed: {res.get('errors') or res}"[:400])
            result = res["results"][0]
            state.contact_id = str(result["id"])
            state.created_contact = bool(result.get("new"))
            state.contact_url = self._contact_url(state.contact_id)
            state.steps.append("contact")
            save(state)

        if state.note_id is None:
            note = {
                "properties": {"hs_timestamp": str(int(time.time() * 1000)), "hs_note_body": note_html(lead)},
                "associations": [{"to": {"id": state.contact_id},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                             "associationTypeId": NOTE_TO_CONTACT}]}],
            }
            res = self._request("POST", f"/crm/objects/{VERSION}/notes", json=note) or {}
            state.note_id = str(res["id"])
            state.steps.append("note")
            save(state)

        if self.create_deals and lead["tier"] == "strong" and state.deal_id is None:
            x = q.get("extracted", {})
            label = lead.get("company") or x.get("company") or lead["name"]
            props = {
                "dealname": f"{label}: {x.get('service') or 'project'} (website enquiry)"[:200],
                "pipeline": "default",
                "dealstage": "appointmentscheduled",
            }
            if x.get("budget_usd"):
                props["amount"] = str(x["budget_usd"])
            deal = {
                "properties": props,
                "associations": [{"to": {"id": state.contact_id},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                             "associationTypeId": DEAL_TO_CONTACT}]}],
            }
            res = self._request("POST", f"/crm/objects/{VERSION}/deals", json=deal) or {}
            state.deal_id = str(res["id"])
            state.steps.append("deal")
            save(state)
        return state

    def log_reply(self, lead: dict, subject: str, body: str, delivered: bool) -> None:
        contact_id = (lead.get("crm") or {}).get("contact_id")
        if not contact_id:
            return
        note = {
            "properties": {"hs_timestamp": str(int(time.time() * 1000)),
                           "hs_note_body": reply_note_html(subject, body, delivered)},
            "associations": [{"to": {"id": contact_id},
                              "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                         "associationTypeId": NOTE_TO_CONTACT}]}],
        }
        self._request("POST", f"/crm/objects/{VERSION}/notes", json=note)
        if delivered and lead["tier"] != "poor":  # a decline keeps UNQUALIFIED
            self._request("PATCH", f"/crm/objects/{VERSION}/contacts/{contact_id}",
                          json={"properties": {"hs_lead_status": "ATTEMPTED_TO_CONTACT"}})


class FakeCRM:
    """In-memory CRM for the demo without a HubSpot token, and for tests."""

    name = "fake"

    def __init__(self, fail_on: str | None = None):
        self.contacts: dict[str, dict] = {}
        self.notes: list[dict] = []
        self.deals: list[dict] = []
        self.fail_on = fail_on

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            raise CRMError(f"fake CRM failure at {step}", retryable=True)

    def sync(self, lead: dict, state: SyncState, save: Callable[[SyncState], None]) -> SyncState:
        if state.contact_id is None:
            self._maybe_fail("contact")
            existing = self.contacts.get(lead["email"])
            state.created_contact = existing is None
            contact = existing or {"id": str(len(self.contacts) + 1)}
            contact.update(email=lead["email"], tier=lead["tier"], score=lead["score"])
            self.contacts[lead["email"]] = contact
            state.contact_id = contact["id"]
            state.steps.append("contact")
            save(state)
        if state.note_id is None:
            self._maybe_fail("note")
            self.notes.append({"contact_id": state.contact_id, "html": note_html(lead)})
            state.note_id = str(len(self.notes))
            state.steps.append("note")
            save(state)
        if lead["tier"] == "strong" and state.deal_id is None:
            self._maybe_fail("deal")
            self.deals.append({"contact_id": state.contact_id, "lead_id": lead["id"]})
            state.deal_id = str(len(self.deals))
            state.steps.append("deal")
            save(state)
        return state

    def log_reply(self, lead: dict, subject: str, body: str, delivered: bool) -> None:
        self._maybe_fail("log_reply")
        contact_id = (lead.get("crm") or {}).get("contact_id")
        self.notes.append({"contact_id": contact_id, "html": reply_note_html(subject, body, delivered)})
        if delivered and lead["tier"] != "poor":
            for c in self.contacts.values():
                if c["id"] == contact_id:
                    c["status"] = "ATTEMPTED_TO_CONTACT"
