# AI Lead Pipeline

Turns website enquiries into qualified, CRM-ready leads. Each enquiry is checked against the business's own rules, with a quote from the message as evidence for every answer. Unknowns stay unknown instead of being guessed. Then a follow-up is drafted for a person to approve.

The sample business, Northwind Studio, is fictional. Its rules are in `criteria.yaml`.

## Status
Steps 1 and 2 of 5 are done: intake, storage, rules, scoring, Claude qualification and an accuracy eval.
Coming next: HubSpot sync, drafted follow-ups with an approval screen, retries and alerts, and an n8n version.

## Measured accuracy
25 fictional enquiries with hand-labelled answers (`evals/enquiries.jsonl`), run on 2026-11-03. They cover clear fits, vague messages, Hinglish, Spanish, pounds, rupees, budgets written in words, a prompt-injection attempt, job applications, sales pitches and out-of-scope requests.

| Qualifier | Tier correct | Spam correct | Answers correct | Guessed when not stated | Cost per lead | Avg time |
|---|---|---|---|---|---|---|
| Claude Opus 5.5 (effort low) | 25/25 | 25/25 | 83/84 | 0 | $0.0132 | 3.6 s |
| Claude Sonnet 5.5 (effort low) | 24/25 | 25/25 | 79/84 | 0 | $0.0066 | 2.9 s |
| Claude Haiku 5.5 (effort low) | 22/25 | 25/25 | 75/84 | 0 | $0.0004 | 2.7 s |
| Keyword rules (no API key) | 14/25 | 23/25 | 56/84 | 0 | free | instant |

- **No model invented a budget, timeline or role.** Their misses are "unknown" where the label says yes or no, which costs a follow-up question, not a wrong decision. Haiku also flipped two answers (yes ↔ no).
- **The quote check caught a real slip.** In the RFP case, Opus answered "no" on decision maker without a quote from the message. The code changed it to unknown, which matches the label.
- **The keyword rules are a fallback only.** For example, they rated an out-of-scope firmware request as a strong lead.
- **Caveat:** after the first Opus run (24/25), one prompt rule was clarified ("up to £10k" is a ceiling, so judge it by £10k). The same 25 cases were then re-run, so treat the numbers as indicative, not a benchmark.

Reproduce:
```bash
.venv/bin/python -m pipeline.eval                                    # keyword rules, free
.venv/bin/python -m pipeline.eval --qualifier claude                 # needs ANTHROPIC_API_KEY
.venv/bin/python -m pipeline.eval --qualifier claude --model claude-haiku-5-5 --save
```
Saved results are in `evals/results/`.

## How it works
1. `POST /intake` accepts JSON or a normal form post. Repeated deliveries (the same submission ID, or the same email and message) are stored once. A hidden honeypot field rejects bots quietly.
2. The lead is saved in SQLite and moves through states: `received → qualified → …`, or `needs_attention` when a step fails. Every step is written to an event log.
3. Claude answers each criterion with yes, no or unknown, plus a quote from the enquiry. It uses structured outputs, so the reply always matches the schema. The enquiry is passed as data, and the prompt tells Claude to ignore instructions inside it.
4. The tier and score are computed in code, not by the AI:
   - A yes or no whose quote is not in the enquiry is changed to unknown.
   - A clear no on a required rule (service, budget) makes the lead poor.
   - Otherwise the weights of the yes answers are summed against the thresholds in `criteria.yaml`.
5. A failed qualification never loses the lead: an API error, a refusal or a cut-off reply. The SDK retries 429 and 5xx errors first. If the call still fails, the lead waits in `needs_attention` with the error, and `POST /api/leads/{id}/retry` runs it again.
6. Every Claude answer stores the model, request ID, tokens and time with the lead, for cost tracking and support.

## Run the demo
```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env    # optional: add ANTHROPIC_API_KEY to use Claude
.venv/bin/uvicorn --factory pipeline.main:create_app --host 127.0.0.1 --port 8020
```

Open http://127.0.0.1:8020 and try the sample buttons. Without a key, the keyword rules qualify leads.

Settings in `.env`:
- `LEAD_MODEL` defaults to `claude-opus-5-5`. Pick a model from the eval table.
- `LEAD_EFFORT` defaults to `low`.
- On Opus and Sonnet, requests opt into Anthropic's server-side refusal fallback.

Keep demo mode on localhost, because the demo page and lead API have no login. With `DEMO_MODE=false`, the demo page is off and the lead API needs `ADMIN_PASSWORD`.

## Change the rules
Edit `criteria.yaml`: services, minimum budget, start window, weights, which rules are required, tier thresholds and spam phrases. No code changes needed. After a change, re-run the eval.

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/intake` | New enquiry (JSON or form). Returns `202` with `lead_id` and `duplicate`. |
| GET | `/api/leads?state=` | List leads, optionally by state |
| GET | `/api/leads/{id}` | One lead with its event log |
| POST | `/api/leads/{id}/retry` | Re-run a lead in `needs_attention` |
| GET | `/health` | Liveness and which qualifier is active |

## Tests
```bash
.venv/bin/python -m pytest -q
```
The Claude qualifier is tested against a fake client, so the tests need no key and cost nothing.
