"""The n8n workflow must apply the same rules as the Python app.

Runs n8n/src/logic.js with plain Node on the same inputs as pipeline/scoring.py and
pipeline/drafts.py and compares the results. Skipped if Node is not installed.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.criteria import load_criteria
from pipeline.drafts import Draft, check_draft
from pipeline.models import CriterionCheck, Qualification
from pipeline.scoring import score

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "n8n"))
import build  # noqa: E402

CRITERIA = load_criteria(ROOT / "criteria.yaml")
CFG = build.config(ROOT / "criteria.yaml", "claude-opus-5-5", "low")
NODE = shutil.which("node")
STRONG = ("Hi, I’m the owner of a dental clinic. We need a new website with online booking. "
          "Budget is around $6,000 and we'd like to start next month.")


def run_js(calls: list[dict]) -> list:
    logic = (ROOT / "n8n" / "src" / "logic.js").read_text(encoding="utf-8").replace(
        "/*CONFIG*/ {}", json.dumps(CFG, ensure_ascii=False))
    script = logic + """
const calls = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = calls.map(c => { try { return module.exports[c.fn](...c.args); } catch (e) { return {error: e.message}; } });
process.stdout.write(JSON.stringify(out));
"""
    r = subprocess.run([NODE, "-e", script], input=json.dumps(calls), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


SCORE_CASES = [
    ("all yes with curly apostrophe quote", STRONG, False, [
        ("service_in_scope", "yes", "need a new website"), ("budget", "yes", "Budget is around $6,000"),
        ("timeline", "yes", "we'd like to start next month"), ("decision_maker", "yes", "I'm the owner")]),
    ("invented quote", STRONG, False, [
        ("service_in_scope", "yes", "need a new website"), ("budget", "yes", "budget of $50,000"),
        ("timeline", "unknown", ""), ("decision_maker", "yes", "i’m the owner")]),
    ("required no", "I'm the owner. We need a logo, budget $9000, start next month.", False, [
        ("service_in_scope", "no", "We need a logo"), ("budget", "yes", "budget $9000"),
        ("timeline", "yes", "start next month"), ("decision_maker", "yes", "I'm the owner")]),
    ("missing and extra answers", STRONG, False, [("service_in_scope", "yes", "website"), ("bogus", "yes", "x"),
                                                  ("service_in_scope", "no", "website")]),
    ("spam", "We sell backlinks and SEO services.", True, []),
]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("label,message,spam,checks", SCORE_CASES, ids=[c[0] for c in SCORE_CASES])
def test_scoring_matches_python(label, message, spam, checks):
    py = score(Qualification(checks=[CriterionCheck(id=i, met=m, evidence=e or None) for i, m, e in checks],
                             is_spam=spam), CRITERIA, message)
    answer = {"is_spam": spam, "checks": [{"id": i, "met": m, "evidence": e} for i, m, e in checks],
              "extracted": {}, "missing_info": [], "summary": ""}
    js = run_js([{"fn": "scoreAnswer", "args": [answer, message]}])[0]
    assert js["tier"] == py.tier and js["score"] == py.score
    assert [(c["id"], c["met"]) for c in js["checks"]] == [(c.id, c.met) for c in py.qualification.checks]
    assert js["corrections"] == py.corrections


DRAFT_CASES = [
    ("strong", "Next step", f"Hi, book here: {CRITERIA.reply.booking_link}"),
    ("strong", "Next step", "Hi, let's talk soon."),
    ("possible", "Quote", "Hi [Name], sites start at $2,500. Book: " + CRITERIA.reply.booking_link),
    ("possible", "Re", "Ours start at $4,000. Thanks"),
    ("strong", "Re", "Thanks, $6,000 works. " + CRITERIA.reply.booking_link),
    ("poor", "Thanks", "word " * 230),
]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("tier,subject,body", DRAFT_CASES)
def test_draft_checks_match_python(tier, subject, body):
    lead = {"tier": tier, "message": STRONG}
    py = check_draft(Draft(subject=subject, body=body), lead, CRITERIA).warnings
    js = run_js([{"fn": "checkDraft", "args": [{"subject": subject, "body": body}, lead]}])[0]
    assert js == py


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_validation_and_payloads():
    lead = {"name": "Ana María López", "email": "ana@example.com", "company": None, "tier": "strong", "score": 100,
            "extracted": {"service": "website", "budget_usd": 6000, "company": "Ana's <Shop>"},
            "checks": [{"id": "budget", "met": "yes", "evidence": "<b>$6,000</b>"}], "missing_info": [],
            "summary": "s & t", "next_action": "Reply today.", "message": "<script>x</script>"}
    v_ok, v_bot, v_bad, upsert, note, deal, claude_err = run_js([
        {"fn": "validateEnquiry", "args": [{"name": " Ana ", "email": "ANA@Example.com ", "message": "Need a website please"}]},
        {"fn": "validateEnquiry", "args": [{"name": "Bot", "email": "b@x.com", "message": "x" * 20, "website": "y"}]},
        {"fn": "validateEnquiry", "args": [{"name": "A", "email": "nope", "message": "x" * 20}]},
        {"fn": "hubspotUpsert", "args": [lead]},
        {"fn": "noteHtml", "args": [lead]},
        {"fn": "hubspotDeal", "args": ["501", lead]},
        {"fn": "claudeJson", "args": [{"type": "message", "stop_reason": "refusal", "content": []}]},
    ])
    assert v_ok == {"ok": True, "enquiry": {"name": "Ana", "email": "ana@example.com", "company": None,
                                            "message": "Need a website please"}}
    assert v_bot == {"ok": False, "reason": "honeypot"} and v_bad["reason"] == "email invalid"
    props = upsert["inputs"][0]["properties"]
    assert upsert["inputs"][0]["idProperty"] == "email"
    assert (props["firstname"], props["lastname"], props["company"]) == ("Ana", "María López", "Ana's <Shop>")
    assert "<script>" not in note and "&lt;script&gt;" in note and "&lt;b&gt;$6,000" in note
    assert deal["associations"][0]["types"][0]["associationTypeId"] == 3 and deal["properties"]["amount"] == "6000"
    assert claude_err == {"error": "Claude declined to answer"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_requests_match_python_prompts():
    from pipeline.claude import system_prompt
    q, d = run_js([
        {"fn": "buildQualifyRequest", "args": [{"name": "Ana", "company": "Shop", "message": "Need a site"}, "2026-11-03"]},
        {"fn": "buildDraftRequest", "args": [{"name": "Ana", "tier": "possible", "summary": "s", "message": "m",
                                               "checks": [], "missing_info": ["When?"]}]},
    ])
    assert q["system"] == system_prompt(CRITERIA) and q["fallbacks"] == "default"
    assert q["output_config"]["format"]["type"] == "json_schema"
    assert "Today's date: 2026-11-03" in q["messages"][0]["content"] and "Company: Shop" in q["messages"][0]["content"]
    assert "When?" in d["messages"][0]["content"] and "Never mention prices" in d["system"]


def test_schema_fields_match_the_python_models():
    from pipeline.claude import _Answer, _Extracted
    from pipeline.drafts import _DraftOut
    assert set(build.QUALIFY_SCHEMA["required"]) == set(_Answer.model_fields)
    assert set(build.QUALIFY_SCHEMA["properties"]["extracted"]["required"]) == set(_Extracted.model_fields)
    assert set(build.DRAFT_SCHEMA["required"]) == set(_DraftOut.model_fields)


def test_workflow_is_well_formed(tmp_path):
    wf = build.lead_pipeline(CFG, "owner@x.example", "team@x.example", None)
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    for source, outputs in wf["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            for target in branch:
                assert target["node"] in names, target
    # every node except the trigger has an incoming connection
    targets = {t["node"] for o in wf["connections"].values() for b in o["main"] for t in b}
    assert set(names) - targets == {"Enquiry webhook"}
    # nothing reaches the lead's inbox before the owner's decision
    order = ["Email owner for approval", "Wait for decision", "Approved?", "Final reply", "Send reply to lead"]
    for a, b in zip(order, order[1:]):
        assert {"node": b, "type": "main", "index": 0} in wf["connections"][a]["main"][0]
    assert all(n["typeVersion"] for n in wf["nodes"])
    json.dumps(wf)  # serialisable
