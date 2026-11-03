import socket
from email import message_from_bytes
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient

from pipeline.alerts import LogNotifier, attention_text, strong_lead_text
from pipeline.config import Settings
from pipeline.crm import FakeCRM
from pipeline.criteria import load_criteria
from pipeline.drafts import TemplateDrafter
from pipeline.mailer import MailError, OutboxMailer, SMTPMailer, build_message
from pipeline.main import create_app
from pipeline.models import Enquiry
from pipeline.qualifier import ScriptedQualifier
from pipeline.service import Pipeline
from pipeline.store import InvalidTransition, Store

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = load_criteria(ROOT / "criteria.yaml")
STRONG = ("Hi, I'm the owner of a dental clinic. We need a new website with online booking. "
          "Budget is around $6,000 and we'd like to start next month.")
VAGUE = "Hello, can you help us with a website? Let me know what you charge."
LINK = "https://cal.example.com/northwind/intro-call"


class FlakyMailer(OutboxMailer):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    def send(self, *a, **kw):
        if self.failures:
            self.failures -= 1
            raise MailError("SMTP down")
        return super().send(*a, **kw)


class BrokenNotifier:
    name = "broken"

    def notify(self, text):
        raise RuntimeError("webhook exploded")


def make(tmp_path, mailer=None, crm=None, notifier=None):
    store = Store(str(tmp_path / "t.db"))
    p = Pipeline(store, ScriptedQualifier(), CRITERIA, crm or FakeCRM(), TemplateDrafter(),
                 mailer or OutboxMailer(), notifier or LogNotifier(), "http://127.0.0.1:8020")
    return p, store


def ready(p, message=STRONG, email="priya@example.com"):
    lead_id, _ = p.intake(Enquiry(name="Priya Shah", email=email, message=message))
    p.process(lead_id)
    assert p.store.get(lead_id)["state"] == "draft_ready"
    return lead_id


# ---------- approval and sending ----------

def test_pipeline_stops_at_draft_ready_until_approved(tmp_path):
    p, store = make(tmp_path)
    lead_id = ready(p)
    p.process(lead_id)  # running again must not send anything
    assert p.mailer.messages == [] and store.get(lead_id)["state"] == "draft_ready"


def test_approve_unchanged_sends_and_logs_to_crm(tmp_path):
    crm = FakeCRM()
    p, store = make(tmp_path, crm=crm)
    lead_id = ready(p)
    d = store.get(lead_id)["draft"]
    assert p.approve(lead_id, d["subject"], d["body"]) == {"ok": True, "warnings": []}
    p.process(lead_id)
    lead = store.get(lead_id)
    assert lead["state"] == "sent" and lead["draft"]["edited"] is False
    assert lead["draft"]["delivered"] is False and lead["draft"]["via"] == "outbox"
    msg = p.mailer.messages[0]
    assert msg["To"] == "priya@example.com" and LINK in msg.get_content()
    assert "Reply approved (recorded only, not emailed)" in crm.notes[-1]["html"]
    kinds = [e["kind"] for e in store.events(lead_id)]
    assert kinds[-3:] == ["approved", "crm_reply_logged", "sent"]


def test_edited_reply_with_warning_needs_confirmation(tmp_path):
    p, store = make(tmp_path)
    lead_id = ready(p, VAGUE, "sam@example.com")
    body = "Hi Sam, our websites start at $2,500. What is your budget?"
    first = p.approve(lead_id, "Re: website", body)
    assert first["ok"] is False and any("$2,500" in w for w in first["warnings"])
    assert store.get(lead_id)["state"] == "draft_ready"
    second = p.approve(lead_id, "Re: website", body, confirm_warnings=True)
    assert second["ok"] is True
    lead = store.get(lead_id)
    assert lead["draft"]["edited"] is True and lead["draft"]["body"] == body
    approved = next(e for e in store.events(lead_id) if e["kind"] == "approved")
    assert "$2,500" in approved["detail"]  # what was confirmed is on the record


def test_reject_stops_the_lead(tmp_path):
    p, store = make(tmp_path)
    lead_id = ready(p)
    p.reject(lead_id, "Already talking to them by phone")
    assert store.get(lead_id)["state"] == "rejected"
    with pytest.raises(InvalidTransition):
        p.approve(lead_id, "x", "y")


def test_send_failure_then_retry_sends_exactly_once(tmp_path):
    mailer = FlakyMailer(failures=1)
    notifier = LogNotifier()
    p, store = make(tmp_path, mailer=mailer, notifier=notifier)
    lead_id = ready(p)
    d = store.get(lead_id)["draft"]
    p.approve(lead_id, d["subject"], d["body"])
    p.process(lead_id)
    lead = store.get(lead_id)
    assert lead["state"] == "needs_attention" and lead["resume_state"] == "approved"
    assert "SMTP down" in lead["error"]
    assert any("needs attention" in t for t in notifier.sent)
    assert p.retry(lead_id)
    assert store.get(lead_id)["state"] == "sent" and len(mailer.messages) == 1


def test_no_second_email_if_crash_happened_after_sending(tmp_path):
    p, store = make(tmp_path)
    lead_id = ready(p)
    d = store.get(lead_id)["draft"]
    p.approve(lead_id, d["subject"], d["body"])
    # simulate: the email went out and its ID was saved, then the process died
    store.update(lead_id, draft={**store.get(lead_id)["draft"], "message_id": "<x@y>", "delivered": True,
                                 "via": "smtp:test"})
    p.process(lead_id)
    assert p.mailer.messages == [] and store.get(lead_id)["state"] == "sent"


def test_crm_log_failure_does_not_block_sending(tmp_path):
    p, store = make(tmp_path, crm=FakeCRM(fail_on="log_reply"))
    lead_id = ready(p)
    d = store.get(lead_id)["draft"]
    p.approve(lead_id, d["subject"], d["body"])
    p.process(lead_id)
    assert store.get(lead_id)["state"] == "sent"
    assert "crm_reply_log_failed" in [e["kind"] for e in store.events(lead_id)]


def test_status_updated_after_real_delivery_but_not_for_declines(tmp_path):
    crm = FakeCRM()
    lead = {"tier": "strong", "crm": {"contact_id": "1"}}
    crm.contacts["a@x.com"] = {"id": "1"}
    crm.log_reply(lead, "s", "b", delivered=True)
    assert crm.contacts["a@x.com"]["status"] == "ATTEMPTED_TO_CONTACT"
    crm.contacts["a@x.com"] = {"id": "1"}
    crm.log_reply({**lead, "tier": "poor"}, "s", "b", delivered=True)
    assert "status" not in crm.contacts["a@x.com"]


def test_restart_resumes_unfinished_leads_but_not_ones_waiting_for_a_person(tmp_path):
    p, store = make(tmp_path)
    waiting = ready(p)                                   # draft_ready: needs a person
    stuck, _ = p.intake(Enquiry(name="Sam", email="sam@example.com", message=VAGUE))  # never processed
    approved = ready(p, VAGUE, "lee@example.com")
    d = store.get(approved)["draft"]
    p.approve(approved, d["subject"], d["body"])         # approved, then the process "died"

    restarted = Pipeline(store, ScriptedQualifier(), CRITERIA, FakeCRM(), TemplateDrafter(), OutboxMailer(),
                         LogNotifier())
    assert restarted.resume_pending() == [stuck, approved]
    assert store.get(stuck)["state"] == "draft_ready"
    assert store.get(approved)["state"] == "sent" and len(restarted.mailer.messages) == 1
    assert store.get(waiting)["state"] == "draft_ready"
    assert restarted.resume_pending() == []              # nothing left; running again is harmless
    assert len(restarted.mailer.messages) == 1


def test_same_lead_processed_from_two_threads_runs_each_step_once(tmp_path):
    import threading
    import time

    class SlowQualifier(ScriptedQualifier):
        calls = 0

        def qualify(self, enquiry, criteria):
            SlowQualifier.calls += 1
            time.sleep(0.2)
            return super().qualify(enquiry, criteria)

    store = Store(str(tmp_path / "t.db"))
    p = Pipeline(store, SlowQualifier(), CRITERIA, FakeCRM(), TemplateDrafter(), OutboxMailer(), LogNotifier())
    lead_id, _ = p.intake(Enquiry(name="Priya", email="p@example.com", message=STRONG))
    threads = [threading.Thread(target=p.process, args=(lead_id,)) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert SlowQualifier.calls == 1 and len(p.crm.contacts) == 1 and len(p.crm.notes) == 1
    assert store.get(lead_id)["state"] == "draft_ready"


# ---------- alerts ----------

def test_strong_lead_alerts_owner_others_do_not(tmp_path):
    notifier = LogNotifier()
    p, store = make(tmp_path, notifier=notifier)
    strong = ready(p)
    ready(p, VAGUE, "sam@example.com")
    assert len(notifier.sent) == 1 and f"/dashboard#lead-{strong}" in notifier.sent[0]
    assert "alert_sent" in [e["kind"] for e in store.events(strong)]


def test_broken_alerts_never_break_the_pipeline(tmp_path):
    p, store = make(tmp_path, notifier=BrokenNotifier())
    lead_id = ready(p)
    assert "alert_failed" in [e["kind"] for e in store.events(lead_id)]


def test_alert_text_is_defanged():
    lead = {"id": 3, "name": "<!channel> @here", "score": 90, "error": None,
            "qualification": {"summary": "Visit <https://evil.example|click> " + "x" * 400}}
    text = strong_lead_text(lead, "http://h")
    assert "<!channel>" not in text and "@here" not in text and "<https" not in text
    assert len(text.splitlines()[1]) <= 160
    assert "unknown error" in attention_text(lead, "http://h")


# ---------- email building and SMTP ----------

def test_header_injection_is_stripped():
    msg = build_message("team@northwind.example", "victim@x.com\nBcc: everyone@x.com",
                        "Hello\r\nBcc: spam@x.com", "Body", "Alex\nBcc: a@b.c")
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"] and "\n" not in msg["To"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.rcpt_tos, message_from_bytes(envelope.content)))
        return "250 OK"


def test_real_smtp_delivery():
    inbox, port = Inbox(), free_port()
    server = Controller(inbox, hostname="127.0.0.1", port=port)
    server.start()
    try:
        mailer = SMTPMailer("127.0.0.1", port, "team@northwind.example", starttls=False,
                            reply_to="hello@northwind.example")
        sent = mailer.send("priya@example.com", "Next step", "Hi Priya,\n\nBook here.", "Alex at Northwind")
    finally:
        server.stop()
    assert sent.delivered and sent.via == "smtp:127.0.0.1"
    rcpts, msg = inbox.messages[0]
    assert rcpts == ["priya@example.com"]
    assert msg["Subject"] == "Next step" and msg["Reply-To"] == "hello@northwind.example"
    assert msg["From"] == "Alex at Northwind <team@northwind.example>"
    assert msg["Message-ID"] == sent.message_id


def test_smtp_unreachable_raises_mail_error():
    with pytest.raises(MailError, match="SMTP 127.0.0.1"):
        SMTPMailer("127.0.0.1", free_port(), "a@b.example", starttls=False, timeout=2).send("x@y.com", "s", "b", "n")


# ---------- HTTP ----------

@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "app.db"), criteria_path=str(ROOT / "criteria.yaml"))
    with TestClient(create_app(settings)) as c:
        yield c


def submit(client, message=STRONG, email="priya@example.com"):
    return client.post("/intake", json={"name": "Priya Shah", "email": email, "message": message}).json()["lead_id"]


def test_http_approve_flow(client):
    lead_id = submit(client)
    d = client.get(f"/api/leads/{lead_id}").json()["draft"]
    r = client.post(f"/api/leads/{lead_id}/approve", json={"subject": d["subject"], "body": d["body"]})
    assert r.status_code == 200 and r.json() == {"ok": True, "warnings": []}
    lead = client.get(f"/api/leads/{lead_id}").json()
    assert lead["state"] == "sent" and lead["draft"]["via"] == "outbox"
    assert client.post(f"/api/leads/{lead_id}/approve", json={"subject": "a", "body": "b"}).status_code == 409


def test_http_warning_round_trip(client):
    lead_id = submit(client, VAGUE, "sam@example.com")
    body = {"subject": "Re", "body": f"Hi Sam, book here: {LINK}"}
    r = client.post(f"/api/leads/{lead_id}/approve", json=body)
    assert r.status_code == 409 and any("Booking link offered" in w for w in r.json()["warnings"])
    r = client.post(f"/api/leads/{lead_id}/approve", json={**body, "confirm_warnings": True})
    assert r.status_code == 200


def test_http_reject_and_validation(client):
    lead_id = submit(client)
    assert client.post(f"/api/leads/{lead_id}/approve", json={"subject": "", "body": "x"}).status_code == 422
    assert client.post(f"/api/leads/{lead_id}/reject", json={"reason": "duplicate"}).json()["state"] == "rejected"


def test_cross_site_actions_are_blocked(client):
    lead_id = submit(client)
    d = client.get(f"/api/leads/{lead_id}").json()["draft"]
    body = {"subject": d["subject"], "body": d["body"]}
    assert client.post(f"/api/leads/{lead_id}/approve", json=body,
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(f"/api/leads/{lead_id}/approve", json=body,
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/demo/reset", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(f"/api/leads/{lead_id}").json()["state"] == "draft_ready"
    ok = client.post(f"/api/leads/{lead_id}/approve", json=body, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_dashboard_and_health(client):
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Lead review" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    h = client.get("/health").json()
    assert h["mailer"] == "outbox" and h["alerts"] == "log"


def test_live_mode_dashboard_needs_password(tmp_path):
    s = Settings(demo_mode=False, admin_password="correct-horse-battery", database_path=str(tmp_path / "x.db"),
                 criteria_path=str(ROOT / "criteria.yaml"))
    with TestClient(create_app(s)) as c:
        assert c.get("/dashboard").status_code == 401
        assert c.post("/api/leads/1/approve", json={"subject": "a", "body": "b"}).status_code == 401
        assert c.get("/dashboard", auth=("admin", "correct-horse-battery")).status_code == 200


def test_settings_validation(tmp_path):
    base = dict(database_path=str(tmp_path / "y.db"), criteria_path=str(ROOT / "criteria.yaml"))
    with pytest.raises(RuntimeError, match="MAIL_FROM"):
        create_app(Settings(smtp_host="smtp.example.com", **base))
    with pytest.raises(RuntimeError, match="https"):
        create_app(Settings(alert_webhook_url="http://hooks.example.com/x", **base))
