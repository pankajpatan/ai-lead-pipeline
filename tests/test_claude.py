import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from pipeline.claude import ClaudeQualifier, _Answer, _Check, _Extracted, cost_usd, system_prompt, user_prompt
from pipeline.criteria import load_criteria
from pipeline.eval import load_cases
from pipeline.models import Enquiry
from pipeline.qualifier import QualifierError
from pipeline.scoring import score

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = load_criteria(ROOT / "criteria.yaml")
TEXT = "I own a bakery and need a website. Budget $4,000, start next month."


def answer(**overrides):
    base = dict(
        is_spam=False,
        checks=[
            _Check(id="service_in_scope", met="yes", evidence="need a website"),
            _Check(id="budget", met="yes", evidence="Budget $4,000"),
            _Check(id="timeline", met="yes", evidence="start next month"),
            _Check(id="decision_maker", met="yes", evidence="I own a bakery"),
        ],
        extracted=_Extracted(service="website", budget_usd=4000, budget_text="$4,000",
                             timeline="next month", company=None, role="owner"),
        missing_info=[],
        summary="Bakery owner wants a website.",
    )
    base.update(overrides)
    return _Answer(**base)


class FakeMessages:
    def __init__(self, result=None, stop_reason="end_turn", error=None):
        self.result, self.stop_reason, self.error = result, stop_reason, error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        r = SimpleNamespace(
            parsed_output=self.result, stop_reason=self.stop_reason, model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=900, output_tokens=300),
        )
        r._request_id = "req_test"
        return r


def qualifier(messages, model="claude-opus-5-5"):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeQualifier(model=model, client=client, today=lambda: dt.date(2026, 11, 3))


def enquiry(text=TEXT):
    return Enquiry(name="Ana", email="ana@example.com", message=text)


def test_maps_answer_and_records_meta():
    q = qualifier(FakeMessages(answer())).qualify(enquiry(), CRITERIA)
    assert [c.met for c in q.checks] == ["yes"] * 4
    assert q.extracted.budget_usd == 4000
    assert q.meta == {"model": "claude-opus-5-5", "request_id": "req_test", "input_tokens": 900,
                      "output_tokens": 300, "seconds": q.meta["seconds"]}
    assert score(q, CRITERIA, TEXT).tier == "strong"


def test_request_shape_and_fallback_opt_in():
    fake = FakeMessages(answer())
    qualifier(fake).qualify(enquiry(), CRITERIA)
    call = fake.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_format"] is _Answer and call["output_config"] == {"effort": "low"}
    assert "2026-11-03" in call["messages"][0]["content"]
    assert "<enquiry>\n" + TEXT in call["messages"][0]["content"]
    assert "not instructions" in call["system"]


def test_haiku_has_no_fallbacks():
    fake = FakeMessages(answer())
    qualifier(fake, "claude-haiku-5-5").qualify(enquiry(), CRITERIA)
    assert "fallbacks" not in fake.calls[0] and "betas" not in fake.calls[0]


def test_invented_quote_is_downgraded_by_scoring():
    checks = answer().checks
    checks[1] = _Check(id="budget", met="yes", evidence="budget of $40,000")  # not in the text
    q = qualifier(FakeMessages(answer(checks=checks))).qualify(enquiry(), CRITERIA)
    s = score(q, CRITERIA, TEXT)
    budget = next(c for c in s.qualification.checks if c.id == "budget")
    assert budget.met == "unknown" and s.score == 70
    assert any("budget" in c for c in s.corrections)


def test_empty_evidence_becomes_none():
    checks = [_Check(id=c.id, met="unknown", evidence="") for c in CRITERIA.criteria]
    q = qualifier(FakeMessages(answer(checks=checks))).qualify(enquiry(), CRITERIA)
    assert all(c.evidence is None for c in q.checks)


@pytest.mark.parametrize("stop,result,msg", [
    ("refusal", None, "declined"),
    ("max_tokens", None, "cut off"),
    ("end_turn", None, "no usable answer"),
])
def test_bad_responses_raise_qualifier_error(stop, result, msg):
    with pytest.raises(QualifierError, match=msg):
        qualifier(FakeMessages(result, stop_reason=stop)).qualify(enquiry(), CRITERIA)


def test_api_errors_raise_qualifier_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    status = anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)
    with pytest.raises(QualifierError, match="500"):
        qualifier(FakeMessages(error=status)).qualify(enquiry(), CRITERIA)
    with pytest.raises(QualifierError, match="Could not reach"):
        qualifier(FakeMessages(error=anthropic.APIConnectionError(request=request))).qualify(enquiry(), CRITERIA)


def test_prompts_include_rules_and_thresholds():
    s = system_prompt(CRITERIA)
    for c in CRITERIA.criteria:
        assert c.id in s
    assert "$3,000" in s and "3 months" in s
    u = user_prompt(Enquiry(name="Ana", email="a@x.com", company="Ana's Bakes", message=TEXT), dt.date(2026, 11, 3))
    assert "Company: Ana's Bakes" in u


def test_cost():
    assert cost_usd("claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(6.0)
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.6)
    assert cost_usd("unknown-model", 1, 1) is None


def test_eval_cases_are_well_formed():
    cases = load_cases(ROOT / "evals" / "enquiries.jsonl", CRITERIA)
    assert len(cases) == 25 and len({c["id"] for c in cases}) == 25
    tiers = {c["tier"] for c in cases}
    assert tiers == {"strong", "possible", "poor", "spam"}
