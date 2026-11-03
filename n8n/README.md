# n8n version

The same lead flow as the Python app, as an importable n8n workflow, for clients who already run n8n or want to change steps without code.

**Tested on n8n 2.46.4 (2026-11-03)** in Docker, with real Claude and HubSpot calls and SMTP to a local mail catcher. Three runs were checked:
- **Strong lead:** qualified, HubSpot contact, note and deal, draft, the owner approved through the form with an edited subject, the reply was sent and logged in HubSpot.
- **Spam:** stopped with nothing created.
- **HubSpot error:** the error workflow emailed the owner the failing step and HubSpot's message.

## Files
- `lead-pipeline.json`: the main workflow, 20 nodes.
- `error-alert.json`: emails the owner when any run of the main workflow fails.
- `src/logic.js` and `build.py`: the source. The JSON files are built from these, using `criteria.yaml` and the Python app's prompts, so both versions follow the same rules. `tests/test_n8n.py` runs the JavaScript under Node and checks that it matches Python case by case.

## Flow
Webhook → validate (honeypot, required fields) → Claude qualifies → score in code (quotes checked, tier from `criteria.yaml`) → spam stops here → HubSpot: upsert contact by email → note with evidence, and a deal for strong leads → Claude drafts the reply → draft checked in code → **owner gets an email with the draft, any warnings and a signed form link** → the workflow waits (up to 72 hours) → owner chooses *Send the reply*, *Send my edited version* or *Don't reply* → reply sent → logged on the HubSpot contact.

The owner decides through a form, not a plain link. Email security scanners open links automatically, and a plain approve link could send a reply nobody approved.

## Import
1. Build with your addresses (or import as-is and edit the two email nodes):
   ```bash
   .venv/bin/python n8n/build.py --owner-email you@yourco.com --mail-from team@yourco.com --reply-to hello@yourco.com
   ```
2. In n8n: **Workflows → Import from file**, import `error-alert.json` first, then `lead-pipeline.json`.
3. Create three credentials and select them in the nodes marked with a warning:
   - **Anthropic API key**: *Header Auth*, name `x-api-key`, value your key.
   - **HubSpot service key**: *Header Auth*, name `Authorization`, value `Bearer <service key>` (scopes as in `.env.example`).
   - **SMTP account**: your mail server.
4. In the main workflow's **Settings → Error workflow**, choose *AI lead pipeline: error alert*.
5. HubSpot needs the two contact properties `lead_fit_tier` and `lead_fit_score`. The Python app creates them on its first sync. Otherwise, add them in HubSpot under **Settings → Properties** (a dropdown with values strong/possible/poor, and a number).
6. Publish both workflows, then point your website form at `https://<your-n8n>/webhook/lead-intake`. It accepts JSON with `name`, `email`, `company`, `message` and the honeypot `website`.

## Differences from the Python app
- **No duplicate-delivery check.** The same form post sent twice runs twice. HubSpot still keeps one contact (upsert by email), but gets two notes.
- **Rejected input stops silently, with no record.** The Python app keeps every lead, including rejected ones.
- **A failed run doesn't resume from the failed step.** n8n retries each HTTP call 3 times, then the run fails and the owner is emailed. Re-run it from n8n's executions list.
- **The lead status isn't changed to ATTEMPTED_TO_CONTACT after sending.**
- **Edited replies with warnings aren't blocked.** The warnings appear in the approval email, and the owner's form choice is final.
- **The wait times out after 72 hours** and the lead gets no reply. Change this in *Wait for decision*.
