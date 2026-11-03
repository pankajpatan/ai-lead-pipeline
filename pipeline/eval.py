"""Measure a qualifier against labelled enquiries.

    python -m pipeline.eval                         # scripted rules, free
    python -m pipeline.eval --qualifier claude      # uses ANTHROPIC_API_KEY, costs a few cents
    python -m pipeline.eval --qualifier claude --model claude-haiku-5-5

Each case in evals/enquiries.jsonl lists the expected answer per criterion
(y/n/u in criteria order) or "spam": true. The expected tier comes from those
labels through the same rules the app uses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from .criteria import Criteria, load_criteria
from .models import Enquiry
from .qualifier import ScriptedQualifier
from .scoring import score, tier_for

ROOT = Path(__file__).resolve().parent.parent
LETTER = {"y": "yes", "n": "no", "u": "unknown"}
EVAL_DATE = dt.date(2026, 11, 3)  # fixed, so relative dates in the cases keep their meaning


def load_cases(path: Path, criteria: Criteria) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if case.get("spam"):
            case["answers"] = {c.id: "unknown" for c in criteria.criteria}
        else:
            if len(case["expect"]) != len(criteria.criteria):
                raise ValueError(f"{case['id']}: expect needs {len(criteria.criteria)} letters")
            case["answers"] = {c.id: LETTER[ch] for c, ch in zip(criteria.criteria, case["expect"])}
        case["tier"], _ = tier_for(case["answers"], bool(case.get("spam")), criteria)
        cases.append(case)
    return cases


def run_case(qualifier, case: dict, criteria: Criteria) -> dict:
    enquiry = Enquiry(name=case["name"], email="eval@example.com", company=case.get("company"),
                      message=case["message"])
    started = time.monotonic()
    try:
        answer = qualifier.qualify(enquiry, criteria)
    except Exception as exc:
        return {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
    scored = score(answer, criteria, case["message"])
    got = {c.id: c.met for c in scored.qualification.checks}
    return {
        "id": case["id"],
        "expected_tier": case["tier"],
        "tier": scored.tier,
        "expected": case["answers"],
        "got": got,
        "expected_spam": bool(case.get("spam")),
        "spam": scored.qualification.is_spam,
        "corrections": scored.corrections,
        "meta": answer.meta,
        "seconds": round(time.monotonic() - started, 2),
        "summary": scored.qualification.summary,
    }


def report(results: list[dict], label: str) -> dict:
    ok = [r for r in results if "error" not in r]
    n = len(results)
    tier_hits = sum(r["tier"] == r["expected_tier"] for r in ok)
    spam_hits = sum(r["spam"] == r["expected_spam"] for r in ok)
    pairs = [(r["expected"][k], r["got"][k]) for r in ok if not r["expected_spam"] for k in r["expected"]]
    exact = sum(e == g for e, g in pairs)
    guessed = sum(e == "unknown" and g != "unknown" for e, g in pairs)
    missed = sum(e != "unknown" and g == "unknown" for e, g in pairs)
    flipped = sum({e, g} == {"yes", "no"} for e, g in pairs)
    tokens_in = sum(r["meta"].get("input_tokens", 0) for r in ok)
    tokens_out = sum(r["meta"].get("output_tokens", 0) for r in ok)
    summary = {
        "qualifier": label,
        "cases": n,
        "errors": n - len(ok),
        "tier_accuracy": f"{tier_hits}/{n}",
        "spam_accuracy": f"{spam_hits}/{n}",
        "answers_correct": f"{exact}/{len(pairs)}",
        "guessed_when_unknown": guessed,
        "said_unknown_when_stated": missed,
        "yes_no_flipped": flipped,
        "quotes_rejected": sum(len(r["corrections"]) for r in ok),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "avg_seconds": round(sum(r["seconds"] for r in ok) / max(len(ok), 1), 2),
    }
    model = next((r["meta"].get("model") for r in ok if r["meta"].get("model")), None)
    if model:
        from .claude import cost_usd
        cost = cost_usd(label.split(":", 1)[-1], tokens_in, tokens_out)
        if cost is not None:
            summary["cost_usd_total"] = round(cost, 4)
            summary["cost_usd_per_lead"] = round(cost / max(len(ok), 1), 5)

    width = max(len(k) for k in summary)
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")
    misses = [r for r in results if "error" in r or r["tier"] != r["expected_tier"]
              or r["expected"] != r["got"] or r["spam"] != r["expected_spam"]]
    if misses:
        print("\nDifferences:")
    for r in misses:
        if "error" in r:
            print(f"- {r['id']}: ERROR {r['error']}")
            continue
        diffs = [f"{k} expected {r['expected'][k]} got {r['got'][k]}" for k in r["expected"]
                 if r["expected"][k] != r["got"][k] and not r["expected_spam"]]
        if r["spam"] != r["expected_spam"]:
            diffs.insert(0, f"spam expected {r['expected_spam']} got {r['spam']}")
        tier = "" if r["tier"] == r["expected_tier"] else f" [tier {r['expected_tier']} -> {r['tier']}]"
        print(f"- {r['id']}{tier}: " + "; ".join(diffs))
        for c in r["corrections"]:
            print(f"    quote check: {c}")
    return summary


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qualifier", choices=["scripted", "claude"], default="scripted")
    parser.add_argument("--model", default=os.getenv("LEAD_MODEL", "claude-opus-5-5"))
    parser.add_argument("--effort", default=os.getenv("LEAD_EFFORT", "low"))
    parser.add_argument("--cases", default=str(ROOT / "evals" / "enquiries.jsonl"))
    parser.add_argument("--criteria", default=str(ROOT / "criteria.yaml"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--save", action="store_true", help="write results to evals/results/")
    args = parser.parse_args()

    criteria = load_criteria(args.criteria)
    cases = load_cases(Path(args.cases), criteria)
    if args.qualifier == "claude":
        from .claude import ClaudeQualifier
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("Set ANTHROPIC_API_KEY in .env to run the Claude eval.")
        qualifier = ClaudeQualifier(os.environ["ANTHROPIC_API_KEY"], args.model, args.effort,
                                    today=lambda: EVAL_DATE)
        label = f"{qualifier.name}" + (f" (effort {args.effort})" if args.effort != "low" else "")
    else:
        qualifier, label = ScriptedQualifier(), "scripted"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: run_case(qualifier, c, criteria), cases))
    summary = report(results, label.split(" ")[0])

    if args.save:
        out = ROOT / "evals" / "results"
        out.mkdir(parents=True, exist_ok=True)
        name = label.split(" ")[0].replace(":", "-") + f"-{args.effort}.json"
        (out / name).write_text(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False))
        print(f"\nSaved {out / name}")


if __name__ == "__main__":
    main()
