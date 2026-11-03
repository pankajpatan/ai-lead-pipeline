# Handover guide

How to run, change and look after the lead pipeline. Written for the business owner or whoever maintains it. The README explains how it works.

## Day to day
- **Approve replies** at `/dashboard`. Each lead under *Waiting for approval* shows the enquiry, every rule's answer with its quote, the HubSpot link and the drafted reply.
  - Edit the reply if needed, then click **Approve and send**. To skip a lead, use **Reject, don't reply**.
  - If the reply mentions a price the client didn't give, offers a booking link to a lead that isn't a strong fit, or has a placeholder in brackets, the dashboard stops and lists the problem. Fix it, or click **Send anyway**. That choice is recorded.
- **Needs attention** lists leads that hit an error: Claude, HubSpot or email was down, or HubSpot rejected something. The card shows the error and the step it will resume from. Click **Retry** once the cause is fixed. Finished steps are never repeated, and a reply is never sent twice.
- **Alerts:** with `ALERT_WEBHOOK_URL` set, Slack gets a message for every strong lead and every lead that needs attention.
- Spam and sales pitches are rejected automatically, with no CRM record and no reply. They're still listed under *Other leads* if you want to check.

## Change the rules
All business rules live in `criteria.yaml`:

| To change | Edit |
|---|---|
| What counts as in scope | `services`, and `description` (Claude reads the description) |
| Minimum budget, start window | `min_budget_usd`, `max_start_months` |
| What matters most | `weight` per rule. The score is the sum of the weights answered "yes". |
| Dealbreakers | `required: true`. A clear "no" on a required rule makes the lead poor. |
| Tier thresholds | `tiers.strong`, `tiers.possible` |
| Reply sender, booking link, whether prices may be quoted | `reply` |
| Spam phrases (for the no-API-key fallback) | `spam_phrases` |

After changing rules:
1. Update the expected answers in `evals/enquiries.jsonl` if the rule change alters them.
2. Run `.venv/bin/python -m pipeline.eval --qualifier claude` and compare with the README table. It costs about $0.33 on Opus.
3. Restart the app.
4. If you use the n8n version, rebuild it with `.venv/bin/python n8n/build.py` and re-import it.

## Change the model or cost
Set `LEAD_MODEL` in `.env`. Measured on the 25-case eval (see the README):

| Model | Tiers correct | Cost per lead |
|---|---|---|
| `claude-opus-5-5` (default) | 25/25 | about $0.013 |
| `claude-sonnet-5-5` | 24/25 | about $0.0066 |
| `claude-haiku-5-5` | 22/25 | about $0.0004 |

The draft reply is one more call per lead, at a similar cost. Run the eval again before switching.

## Swap or add a CRM
The CRM sits behind a small interface in `pipeline/crm.py`. Write a class with:
- `name`
- `sync(lead, state, save)`: create or update the contact, add the note and the deal. Call `save(state)` after each record is created, so a retry resumes where it stopped.
- `log_reply(lead, subject, body, delivered)`

`FakeCRM` in the same file is the shortest working example. Choose your class in `pipeline/main.py` (`create_app`) and add tests like `tests/test_crm.py`.

## Keys and passwords
Everything secret is in `.env`. That file is git-ignored, so never commit or email it.

| Key | Where to rotate it | Then |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com, API keys | Update `.env`, restart |
| `HUBSPOT_TOKEN` | HubSpot, Development > Keys > Service keys (rotate or create a new key with the scopes in `.env.example`) | Update `.env`, restart |
| `SMTP_PASSWORD` | Your email provider | Update `.env`, restart |
| `ADMIN_PASSWORD` | You choose it (12+ characters) | Update `.env`, restart |
| `ALERT_WEBHOOK_URL` | Slack app settings, Incoming Webhooks | Update `.env`, restart |

If a key leaks, rotate it first and investigate afterwards. The HubSpot service key can read and write all contacts and deals.

## Logs and records
- **Lead records** are in the SQLite file `DATABASE_PATH` (default `leads.db`), in the `leads` and `events` tables. `GET /api/leads/{id}` returns one lead with its full history. Every Claude answer stores the model, request ID and tokens, which is useful for cost checks and for Anthropic support.
- **App logs** go to standard output: one line per step, including `qualified lead_id=… tier=…`, `email sent message_id=…` and every failure with its traceback.
- **Backups:** copy `leads.db` while the app is stopped, or use `sqlite3 leads.db ".backup backup.db"` while it runs.
- **Personal data:** leads contain names, emails and messages, and the same data is in HubSpot. Keep both under your privacy policy. To delete a person, delete the HubSpot contact and the lead rows (`DELETE FROM events WHERE lead_id=…; DELETE FROM leads WHERE id=…;`).

## Running it for real
1. Put it behind HTTPS (for example Caddy or nginx), and set `DEMO_MODE=false`, `ADMIN_PASSWORD`, and `PUBLIC_URL` (used in alert links).
2. Set `SMTP_*`, `MAIL_FROM` and `REPLY_TO`. Send yourself a test lead first.
3. Run **one** app process. The database is SQLite, and background work runs inside that process:
   ```bash
   .venv/bin/uvicorn --factory pipeline.main:create_app --host 127.0.0.1 --port 8020 --workers 1
   ```
4. Restarts are safe. On startup the app finishes any lead that was mid-pipeline (qualifying, syncing, drafting, or approved but not yet sent). Leads waiting for a person stay where they are.
5. Point the website form at `POST /intake`. It takes a normal form post or JSON with `name`, `email`, `company` and `message`, plus a hidden `website` field as the bot trap.

## Health checks
- `GET /health` shows which qualifier, CRM, drafter, mailer and alert channel are active.
- Run the tests with `.venv/bin/python -m pytest -q`. They need no keys.
- If many leads land in *Needs attention* at once, the cause is usually a service, not the leads. Check the Anthropic status page, HubSpot status, or the email provider, then retry.
