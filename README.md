# AI Lead Pipeline

Turns website enquiries into qualified, CRM-ready leads. Each enquiry is checked against the business's own rules, with a quote from the message as evidence for every answer. Unknowns stay unknown instead of being guessed. Then a follow-up is drafted for a person to approve.

The sample business, Northwind Studio, is fictional. Its rules are in `criteria.yaml`.

## Status
Step 1 of 5 is done: intake, storage, rules, scripted qualifier, scoring and tests.
Coming next: the Claude qualifier with an accuracy eval, HubSpot sync, drafted follow-ups with an approval screen, retries and alerts, and an n8n version.

## How it works (so far)
1. `POST /intake` accepts JSON or a normal form post. Repeated deliveries (the same submission ID, or the same email and message) are stored once. A hidden honeypot field rejects bots quietly.
2. The lead is saved in SQLite and moves through states: `received → qualified → …`, or `needs_attention` when a step fails. Every step is written to an event log.
3. A qualifier answers each criterion with yes, no or unknown, plus a quote from the enquiry.
4. The tier and score are computed in code, not by the AI:
   - A yes or no without a matching quote is changed to unknown.
   - A clear no on a required rule (service, budget) makes the lead poor.
   - Otherwise the weights of the yes answers are summed against the thresholds in `criteria.yaml`.
5. A failed qualification never loses the lead. It waits in `needs_attention` with the error, and `POST /api/leads/{id}/retry` runs it again.

## Run the demo
No API key is needed. Without one, fixed keyword rules do the qualifying.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn --factory pipeline.main:create_app --host 127.0.0.1 --port 8020
```

Open http://127.0.0.1:8020 and try the sample buttons. Keep demo mode on localhost, because the demo page and lead API have no login. With `DEMO_MODE=false`, the demo page is off and the lead API needs `ADMIN_PASSWORD`.

## Change the rules
Edit `criteria.yaml`: services, minimum budget, start window, weights, which rules are required, tier thresholds and spam phrases. No code changes needed.

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
