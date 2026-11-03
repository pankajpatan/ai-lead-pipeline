import json
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from pipeline.crm import CRMError, FakeCRM, HubSpotCRM, SyncState, note_html, split_name
from pipeline.criteria import load_criteria
from pipeline.drafts import ClaudeDrafter, Draft, DraftError, TemplateDrafter, _DraftOut, check_draft
from pipeline.models import Enquiry
from pipeline.qualifier import ScriptedQualifier
from pipeline.service import Pipeline
from pipeline.store import Store

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = load_criteria(ROOT / "criteria.yaml")
STRONG = ("Hi, I'm the owner of a dental clinic. We need a new website with online booking. "
          "Budget is around $6,000 and we'd like to start next month.")
VAGUE = "Hello, can you help us with a website? Let me know what you charge."
SPAM = "We offer SEO services and high quality backlinks to rank your site on page one."


def lead(tier="strong", **kw):
    base = {
        "id": 7, "name": "Priya Shah", "email": "priya@example.com", "company": "Bright Smiles",
        "message": STRONG, "tier": tier, "score": 100, "qualifier": "scripted",
        "qualification": {
            "checks": [{"id": "budget", "met": "yes", "evidence": "<b>$6,000</b>"}],
            "extracted": {"service": "website", "budget_usd": 6000},
            "missing_info": ["When would you like to start?"],
            "summary": "Clinic wants a site & booking", "next_action": "Reply today.",
            "meta": {},
        },
    }
    base.update(kw)
    return base


class FakeHubSpot:
    """Records requests and answers like HubSpot."""

    def __init__(self, existing_props=False, fail=None):
        self.requests = []
        self.existing_props = existing_props
        self.fail = dict(fail or {})  # path fragment -> list of status codes to return first

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        for fragment, codes in self.fail.items():
            if fragment in request.url.path and codes:
                code = codes.pop(0)
                headers = {"Retry-After": "2"} if code == 429 else {}
                return httpx.Response(code, json={"message": f"fake {code}"}, headers=headers)
        path = request.url.path
        if "/crm/properties/" in path and request.method == "GET":
            return httpx.Response(200 if self.existing_props else 404, json={"name": path.rsplit("/", 1)[-1]})
        if "/crm/properties/" in path:
            return httpx.Response(201, json=body)
        if path.endswith("/contacts/batch/upsert"):
            return httpx.Response(200, json={"status": "COMPLETE", "results": [{"id": "501", "new": True}]})
        if path.endswith("/notes"):
            return httpx.Response(201, json={"id": "901"})
        if path.endswith("/deals"):
            return httpx.Response(201, json={"id": "301"})
        if path == "/account-info/v3/details":
            return httpx.Response(200, json={"portalId": 123, "uiDomain": "app-na2.hubspot.com"})
        return httpx.Response(404, json={"message": "unexpected " + path})


def hubspot(fake, **kw):
    client = httpx.Client(base_url="https://api.hubapi.com", transport=httpx.MockTransport(fake.handler))
    sleeps = []
    crm = HubSpotCRM("pat-test", client=client, sleep=sleeps.append, **kw)
    return crm, sleeps


# ---------- HubSpot adapter ----------

def test_strong_lead_creates_properties_contact_note_and_deal():
    fake = FakeHubSpot()
    crm, _ = hubspot(fake)
    saved = []
    state = crm.sync(lead(), SyncState(), lambda s: saved.append(list(s.steps)))
    assert (state.contact_id, state.note_id, state.deal_id) == ("501", "901", "301")
    assert state.created_contact is True
    assert state.contact_url == "https://app-na2.hubspot.com/contacts/123/record/0-1/501"
    assert saved == [["contact"], ["contact", "note"], ["contact", "note", "deal"]]

    created = [b["name"] for m, p, b in fake.requests if m == "POST" and "/crm/properties/" in p]
    assert created == ["lead_fit_tier", "lead_fit_score"]
    calls = {(m, p): b for m, p, b in fake.requests}
    upsert = calls[("POST", "/crm/objects/2026-09/contacts/batch/upsert")]["inputs"][0]
    assert upsert["idProperty"] == "email" and upsert["id"] == "priya@example.com"
    assert upsert["properties"] == {"email": "priya@example.com", "firstname": "Priya", "lastname": "Shah",
                                    "lead_fit_tier": "strong", "lead_fit_score": "100",
                                    "hs_lead_status": "NEW", "company": "Bright Smiles"}
    note = calls[("POST", "/crm/objects/2026-09/notes")]
    assert note["associations"][0]["types"][0]["associationTypeId"] == 202
    assert note["associations"][0]["to"]["id"] == "501"
    deal = calls[("POST", "/crm/objects/2026-09/deals")]
    assert deal["associations"][0]["types"][0]["associationTypeId"] == 3
    assert deal["properties"]["amount"] == "6000" and deal["properties"]["dealstage"] == "appointmentscheduled"


def test_poor_lead_has_no_deal_and_is_unqualified():
    fake = FakeHubSpot(existing_props=True)
    crm, _ = hubspot(fake)
    state = crm.sync(lead("poor", score=50), SyncState(), lambda s: None)
    assert state.deal_id is None
    paths = [p for _, p, _ in fake.requests]
    assert not any(p.endswith("/deals") for p in paths)
    assert not any(m == "POST" and "/properties/" in p for m, p, _ in fake.requests)  # already existed
    upsert = next(b for _, p, b in fake.requests if p.endswith("/batch/upsert"))
    assert upsert["inputs"][0]["properties"]["hs_lead_status"] == "UNQUALIFIED"


def test_429_waits_for_retry_after_then_succeeds():
    fake = FakeHubSpot(existing_props=True, fail={"/batch/upsert": [429, 503]})
    crm, sleeps = hubspot(fake)
    state = crm.sync(lead(), SyncState(), lambda s: None)
    assert state.contact_id == "501" and sleeps == [2.0, 4]


def test_400_is_not_retried():
    fake = FakeHubSpot(existing_props=True, fail={"/notes": [400]})
    crm, sleeps = hubspot(fake)
    saved = []
    with pytest.raises(CRMError, match="400") as err:
        crm.sync(lead(), SyncState(), lambda s: saved.append(s.contact_id))
    assert err.value.retryable is False and sleeps == []
    assert saved == ["501"]  # the contact step was saved before the note failed


def test_resume_skips_finished_steps():
    fake = FakeHubSpot(existing_props=True)
    crm, _ = hubspot(fake)
    state = crm.sync(lead(), SyncState(contact_id="501", note_id="901"), lambda s: None)
    assert state.deal_id == "301"
    assert [p for m, p, _ in fake.requests if m == "POST"] == ["/crm/objects/2026-09/deals"]


def test_gives_up_after_max_attempts():
    fake = FakeHubSpot(existing_props=True, fail={"/batch/upsert": [500] * 10})
    crm, sleeps = hubspot(fake, max_attempts=3)
    with pytest.raises(CRMError) as err:
        crm.sync(lead(), SyncState(), lambda s: None)
    assert err.value.retryable and len(sleeps) == 2


def test_parallel_leads_set_up_properties_once_and_409_counts_as_done():
    """Live run 2026-11-03: three leads at once all tried to create the properties."""
    from concurrent.futures import ThreadPoolExecutor
    fake = FakeHubSpot(fail={"/crm/properties/2026-09/contacts": []})
    real = fake.handler

    def handler(request):  # second property: another server created it first
        if (request.method == "POST" and request.url.path == "/crm/properties/2026-09/contacts"
                and b'"lead_fit_score"' in request.content):
            fake.requests.append((request.method, request.url.path, None))
            return httpx.Response(409, json={"message": "A property named 'lead_fit_score' already exists."})
        return real(request)

    client = httpx.Client(base_url="https://api.hubapi.com", transport=httpx.MockTransport(handler))
    crm = HubSpotCRM("pat-test", client=client, sleep=lambda s: None)
    with ThreadPoolExecutor(5) as pool:
        states = list(pool.map(lambda i: crm.sync(lead(id=i, email=f"p{i}@x.com"), SyncState(), lambda s: None),
                               range(5)))
    assert all(s.note_id for s in states)
    property_posts = [p for m, p, _ in fake.requests if m == "POST" and "/crm/properties/" in p]
    assert len(property_posts) == 2  # one attempt per property, not one per lead


def test_note_escapes_enquiry_text():
    h = note_html(lead(message="<script>alert(1)</script>"))
    assert "<script>" not in h and "&lt;script&gt;" in h and "&lt;b&gt;$6,000" in h


def test_split_name():
    assert split_name("Priya Shah") == ("Priya", "Shah")
    assert split_name("  Cher ") == ("Cher", "")
    assert split_name("Ana María López") == ("Ana", "María López")


# ---------- drafts ----------

def test_template_drafts_per_tier():
    d = TemplateDrafter()
    strong = check_draft(d.draft(lead(), CRITERIA), lead(), CRITERIA)
    assert "cal.example.com" in strong.body and strong.warnings == []
    possible = check_draft(d.draft(lead("possible"), CRITERIA), lead("possible"), CRITERIA)
    assert "When would you like to start?" in possible.body and "cal.example.com" not in possible.body
    poor = d.draft(lead("poor"), CRITERIA)
    assert "right fit" in poor.body and poor.warnings == []


def test_check_draft_flags_problems():
    bad = Draft(subject="Quote", body="Hi [Name], our sites start at $2,500. Book: https://cal.example.com/northwind/intro-call")
    w = check_draft(bad, lead("possible"), CRITERIA).warnings
    assert any("price" in x and "$2,500" in x for x in w)
    assert any("placeholder" in x for x in w)
    assert any("Booking link offered" in x for x in w)
    missing = check_draft(Draft(subject="Hi", body="Thanks!"), lead(), CRITERIA).warnings
    assert missing == ["Strong lead, but the booking link is missing."]
    # a price the client gave is fine to repeat
    assert check_draft(Draft(subject="Re", body="Thanks, $6,000 works. https://cal.example.com/northwind/intro-call"),
                       lead(), CRITERIA).warnings == []


class FakeMessages:
    def __init__(self, out=None, stop="end_turn"):
        self.out, self.stop, self.calls = out, stop, []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self.out, stop_reason=self.stop)


def test_claude_drafter_request_and_result():
    fake = FakeMessages(_DraftOut(subject=" Next step ", body=" Hi Priya, book here: x "))
    d = ClaudeDrafter(model="claude-opus-5-5", client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    draft = d.draft(lead(), CRITERIA)
    assert draft.subject == "Next step" and draft.drafter == "claude:claude-opus-5-5"
    call = fake.calls[0]
    assert "cal.example.com" in call["messages"][0]["content"] and "<enquiry>" in call["messages"][0]["content"]
    assert "Never mention prices" in call["system"] and call["fallbacks"] == "default"
    with pytest.raises(DraftError):
        ClaudeDrafter(client=SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(None, "refusal")))).draft(lead(), CRITERIA)


# ---------- pipeline with CRM and drafts ----------

@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def run(store, message, crm=None, email="priya@example.com"):
    p = Pipeline(store, ScriptedQualifier(), CRITERIA, crm or FakeCRM(), TemplateDrafter())
    lead_id, _ = p.intake(Enquiry(name="Priya", email=email, message=message))
    p.process(lead_id)
    return p, lead_id


def test_full_flow_to_draft_ready(store):
    _, lead_id = run(store, STRONG)
    l = store.get(lead_id)
    assert l["state"] == "draft_ready" and l["crm"]["deal_id"] == "1"
    assert [e["kind"] for e in store.events(lead_id)] == ["received", "qualified", "crm_synced", "draft_ready"]


def test_spam_is_rejected_without_crm(store):
    crm = FakeCRM()
    _, lead_id = run(store, SPAM, crm)
    assert store.get(lead_id)["state"] == "rejected" and crm.contacts == {}


def test_crm_failure_then_retry_resumes_without_requalifying(store):
    crm = FakeCRM(fail_on="note")
    p, lead_id = run(store, STRONG, crm)
    l = store.get(lead_id)
    assert l["state"] == "needs_attention" and l["resume_state"] == "qualified"
    assert l["crm"]["contact_id"] == "1" and l["crm"]["note_id"] is None  # progress kept
    assert "note" in l["error"]

    crm.fail_on = None
    assert p.retry(lead_id)
    l = store.get(lead_id)
    assert l["state"] == "draft_ready" and l["attempts"] == 1  # qualified once only
    assert len(crm.contacts) == 1 and len(crm.notes) == 1 and len(crm.deals) == 1


def test_same_email_twice_updates_one_contact(store):
    crm = FakeCRM()
    run(store, STRONG, crm)
    run(store, VAGUE, crm)
    assert len(crm.contacts) == 1 and len(crm.notes) == 2 and len(crm.deals) == 1


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE leads (id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, source TEXT, name TEXT, "
                     "email TEXT, company TEXT, message TEXT, state TEXT DEFAULT 'received', tier TEXT, score INTEGER, "
                     "qualification TEXT, qualifier TEXT, error TEXT, attempts INTEGER DEFAULT 0, "
                     "created_at REAL, updated_at REAL);")
    db.close()
    s = Store(str(path))
    lead_id, _ = s.add_lead("k", "web_form", "P", "p@x.com", None, STRONG)
    s.update(lead_id, crm={"contact_id": "9"})
    assert s.get(lead_id)["crm"] == {"contact_id": "9"}
