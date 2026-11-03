from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.config import Settings
from pipeline.criteria import load_criteria
from pipeline.main import create_app
from pipeline.models import CriterionCheck, Enquiry, Qualification
from pipeline.qualifier import ScriptedQualifier
from pipeline.scoring import score
from pipeline.service import Pipeline, idempotency_key
from pipeline.store import InvalidTransition, Store

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = load_criteria(ROOT / "criteria.yaml")

STRONG = ("Hi, I'm the owner of a dental clinic. We need a new website with online booking. "
          "Budget is around $6,000 and we'd like to start next month.")


def enquiry(message=STRONG, **kw):
    return Enquiry(name=kw.pop("name", "Priya"), email=kw.pop("email", "priya@example.com"), message=message, **kw)


def checks(**answers):
    return [CriterionCheck(id=k, met=v[0], evidence=v[1]) for k, v in answers.items()]


class FixedQualifier:
    name = "fixed"

    def __init__(self, result):
        self.result, self.calls = result, 0

    def qualify(self, enquiry, criteria):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "app.db"), criteria_path=str(ROOT / "criteria.yaml"))
    with TestClient(create_app(settings)) as c:
        yield c


# ---------- scripted qualifier + scoring ----------

def test_strong_enquiry_scores_strong():
    s = score(ScriptedQualifier().qualify(enquiry(), CRITERIA), CRITERIA, STRONG)
    assert s.tier == "strong" and s.score == 100
    q = s.qualification
    assert q.extracted.budget_usd == 6000 and q.extracted.service == "website"
    assert all(c.met == "yes" and c.evidence for c in q.checks)
    assert q.missing_info == [] and s.corrections == []


def test_vague_enquiry_is_possible_with_questions():
    text = "Hello, can you help us with a website? Let me know what you charge."
    s = score(ScriptedQualifier().qualify(enquiry(text), CRITERIA), CRITERIA, text)
    assert s.tier == "possible" and s.score == 30
    assert len(s.qualification.missing_info) == 3
    assert s.qualification.extracted.budget_usd is None  # never guessed


def test_low_budget_and_late_start_is_poor():
    text = "Looking for a mobile app like Uber for my startup. Budget $800, no rush, maybe next year."
    s = score(ScriptedQualifier().qualify(enquiry(text), CRITERIA), CRITERIA, text)
    answers = {c.id: c.met for c in s.qualification.checks}
    assert answers == {"service_in_scope": "yes", "budget": "no", "timeline": "no", "decision_maker": "yes"}
    assert s.score == 50 and s.tier == "poor"  # budget is required, and it is a clear no


def test_spam_is_flagged():
    text = "We offer SEO services and high quality backlinks to rank your site on page one."
    s = score(ScriptedQualifier().qualify(enquiry(text), CRITERIA), CRITERIA, text)
    assert s.tier == "spam" and s.score == 0


@pytest.mark.parametrize("text,usd", [
    ("Budget is $5k for a website", 5000),
    ("we can spend 4,500 USD on the site", 4500),
    ("between $2,000 and $3,500 for a website", 3500),
    ("Budget $800, no rush", 800),
    ("about $1,250,000 total", 1250000),
])
def test_budget_parsing(text, usd):
    q = ScriptedQualifier().qualify(enquiry(text), CRITERIA)
    assert q.extracted.budget_usd == usd
    assert not q.extracted.budget_text.endswith(",")


def test_other_currency_is_kept_but_not_judged():
    text = "Need a website for my shop, budget ₹2 lakh, start within 2 months"
    q = ScriptedQualifier().qualify(enquiry(text), CRITERIA)
    budget = next(c for c in q.checks if c.id == "budget")
    assert budget.met == "unknown" and "₹2 lakh" in q.extracted.budget_text


def test_not_decision_maker():
    text = "Getting quotes for my manager for a new website, budget $4000, start in 6 weeks"
    q = ScriptedQualifier().qualify(enquiry(text), CRITERIA)
    assert next(c for c in q.checks if c.id == "decision_maker").met == "no"


# ---------- validation: answers must be backed by the enquiry text ----------

def test_yes_without_quote_from_enquiry_becomes_unknown():
    q = Qualification(checks=checks(
        service_in_scope=("yes", "new website"),
        budget=("yes", "budget of $50,000"),       # invented: not in the text
        timeline=("yes", None),                     # no evidence at all
        decision_maker=("yes", "i'm the owner"),
    ))
    s = score(q, CRITERIA, STRONG)
    answers = {c.id: c.met for c in s.qualification.checks}
    assert answers == {"service_in_scope": "yes", "budget": "unknown", "timeline": "unknown", "decision_maker": "yes"}
    assert s.score == 50 and s.tier == "possible" and len(s.corrections) == 2


def test_missing_extra_and_repeated_answers_are_fixed():
    q = Qualification(checks=checks(service_in_scope=("yes", "website"), made_up=("yes", "website"))
                      + checks(service_in_scope=("no", "website")))
    s = score(q, CRITERIA, STRONG)
    assert [c.id for c in s.qualification.checks] == [c.id for c in CRITERIA.criteria]
    assert s.qualification.checks[0].met == "yes"
    assert len(s.corrections) == 5  # 1 unknown id, 1 repeat, 3 unanswered


def test_required_no_makes_lead_poor_whatever_the_score():
    text = "I'm the owner. We need a logo, budget $9000, start next month."
    q = Qualification(checks=checks(
        service_in_scope=("no", "we need a logo"), budget=("yes", "budget $9000"),
        timeline=("yes", "start next month"), decision_maker=("yes", "i'm the owner"),
    ))
    s = score(q, CRITERIA, text)
    assert s.score == 70 and s.tier == "poor"


# ---------- store ----------

def test_same_enquiry_twice_is_one_lead(store):
    a = idempotency_key(enquiry())
    assert a == idempotency_key(enquiry(STRONG.upper().replace(" ", "  "), email="PRIYA@example.com"))
    assert a != idempotency_key(enquiry(submission_id="tally-1"))
    first = store.add_lead(a, "web_form", "P", "p@x.com", None, STRONG)
    second = store.add_lead(a, "web_form", "P", "p@x.com", None, STRONG)
    assert first == (1, True) and second == (1, False)
    assert [e["kind"] for e in store.events(1)] == ["received", "duplicate_delivery"]


def test_invalid_transition_is_refused(store):
    lead_id, _ = store.add_lead("k", "web_form", "P", "p@x.com", None, STRONG)
    with pytest.raises(InvalidTransition):
        store.transition(lead_id, "sent")
    with pytest.raises(ValueError):
        store.transition(lead_id, "qualified", password="x")


# ---------- pipeline ----------

def test_qualifier_failure_goes_to_needs_attention_and_retry_recovers(store):
    broken = FixedQualifier(RuntimeError("model timed out"))
    p = Pipeline(store, broken, CRITERIA)
    lead_id, outcome = p.intake(enquiry())
    p.qualify(lead_id)
    lead = store.get(lead_id)
    assert outcome == "new" and lead["state"] == "needs_attention"
    assert "model timed out" in lead["error"] and lead["attempts"] == 1

    p.qualifier = ScriptedQualifier()
    assert p.retry(lead_id)
    lead = store.get(lead_id)
    assert lead["state"] == "qualified" and lead["tier"] == "strong" and lead["error"] is None
    assert lead["attempts"] == 2
    assert [e["kind"] for e in store.events(lead_id)] == ["received", "needs_attention", "received", "qualified"]


def test_qualify_runs_once(store):
    fixed = FixedQualifier(ScriptedQualifier().qualify(enquiry(), CRITERIA))
    p = Pipeline(store, fixed, CRITERIA)
    lead_id, _ = p.intake(enquiry())
    p.qualify(lead_id)
    p.qualify(lead_id)  # already qualified: nothing happens
    assert fixed.calls == 1
    assert store.get(lead_id)["qualification"]["next_action"].startswith("Reply today")


def test_honeypot_is_rejected_without_qualifying(store):
    fixed = FixedQualifier(None)
    p = Pipeline(store, fixed, CRITERIA)
    lead_id, outcome = p.intake(enquiry(website="http://spam.example"))
    assert outcome == "rejected" and store.get(lead_id)["state"] == "rejected" and fixed.calls == 0


# ---------- HTTP ----------

def test_intake_json_runs_every_step_in_background(client):
    r = client.post("/intake", json={"name": "Priya", "email": "priya@example.com", "message": STRONG})
    assert r.status_code == 202 and r.json() == {"ok": True, "lead_id": 1, "duplicate": False}
    lead = client.get("/api/leads/1").json()
    assert lead["state"] == "draft_ready" and lead["tier"] == "strong"
    assert [e["kind"] for e in lead["events"]] == ["received", "qualified", "crm_synced", "draft_ready"]
    assert lead["crm"]["contact_id"] == "1" and lead["crm"]["deal_id"] == "1"
    assert "cal.example.com" in lead["draft"]["body"] and lead["draft"]["warnings"] == []
    assert client.get("/health").json()["crm"] == "fake"


def test_intake_form_post_and_duplicate(client):
    form = {"name": "Priya", "email": "priya@example.com", "message": STRONG, "company": ""}
    assert client.post("/intake", data=form).json()["duplicate"] is False
    assert client.post("/intake", data=form).json()["duplicate"] is True
    assert len(client.get("/api/leads").json()) == 1


def test_intake_validation_errors(client):
    r = client.post("/intake", json={"name": "", "email": "not-an-email", "message": "short"})
    assert r.status_code == 422
    assert {e["field"] for e in r.json()["errors"]} == {"name", "email", "message"}
    assert client.post("/intake", content="{bad", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/intake", json=["list"]).status_code == 422


def test_honeypot_looks_like_success_to_the_bot(client):
    r = client.post("/intake", json={"name": "Bot", "email": "b@x.com", "message": STRONG, "website": "x"})
    assert r.status_code == 202 and r.json()["duplicate"] is False
    assert client.get("/api/leads/1").json()["state"] == "rejected"


def test_retry_only_for_needs_attention(client):
    client.post("/intake", json={"name": "Priya", "email": "priya@example.com", "message": STRONG})
    assert client.post("/api/leads/1/retry").status_code == 409
    assert client.get("/api/leads?state=nope").status_code == 422
    assert client.get("/api/leads/99").status_code == 404


def test_live_mode_needs_password_and_hides_demo(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(demo_mode=False, database_path=str(tmp_path / "x.db"), criteria_path=str(ROOT / "criteria.yaml")))
    settings = Settings(demo_mode=False, admin_password="correct-horse-battery",
                        database_path=str(tmp_path / "x.db"), criteria_path=str(ROOT / "criteria.yaml"))
    with TestClient(create_app(settings)) as c:
        assert c.get("/").status_code == 404
        assert c.post("/api/demo/reset").status_code == 404
        assert c.get("/api/leads").status_code == 401
        assert c.get("/api/leads", auth=("admin", "wrong")).status_code == 401
        assert c.get("/api/leads", auth=("admin", "correct-horse-battery")).status_code == 200
        assert c.post("/intake", json={"name": "P", "email": "p@x.com", "message": STRONG}).status_code == 202
