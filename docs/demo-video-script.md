# Demo video script (about 2 minutes)

For an Upwork portfolio item or a proposal. Record the screen at 1080p with your voice, no music. Keep each shot short.

## Before recording
- Start the app with real keys (`.env`: Claude, HubSpot, `SMTP_*` pointing at Mailpit) and run `curl -X POST localhost:8020/api/demo/reset`.
- Run Mailpit (`docker run --rm -p 8025:8025 -p 1025:1025 axllent/mailpit`) and open http://localhost:8025 in a tab.
- Open HubSpot's contacts list in another tab. Use a test account, never a client's CRM.
- Zoom the browser to 125% so the text is readable.

## Shots
| Time | Screen | Say |
|---|---|---|
| 0:00 | Demo form | "Small businesses lose leads because nobody replies fast, or the team replies to the wrong ones. This pipeline qualifies every enquiry against the business's own rules, puts it in the CRM, and drafts the reply. A person approves before anything is sent." |
| 0:12 | Click **Strong fit**, submit. Click **Vague**, submit. Click **Spam**, submit. | "Three enquiries: a good one, a vague one and a sales pitch." |
| 0:25 | Lead list: tiers, quotes | "Claude answers each rule, budget, timeline, scope and decision maker, and has to quote the message. If it can't quote it, the code marks it unknown instead of guessing. The tier is calculated in code from the business's rules, so it's consistent." |
| 0:45 | HubSpot contact: fit tier and score fields, note, deal | "In HubSpot: the contact without duplicates, the fit score, a note with the evidence, and a deal for the strong lead." |
| 1:00 | Dashboard, vague lead: add "Our sites start at $2,500" to the reply, click **Approve and send** | "Replies wait here. If I add a price the client never mentioned, it stops me, because the business doesn't quote prices by email." |
| 1:12 | Remove the price, approve. Approve the strong lead unchanged. | "Fixed. Approved." |
| 1:20 | Mailpit inbox: open the strong lead's email | "The reply goes out with the booking link, and it's logged back on the HubSpot contact." |
| 1:30 | Terminal: run `.venv/bin/python -m pipeline.eval --qualifier claude`, or show the README eval table | "I measured it on 25 test enquiries: Hinglish, Spanish, budgets written in words, a prompt-injection attempt. Opus got every tier right and never invented a budget. About one cent per lead." |
| 1:45 | n8n canvas with the imported workflow | "The same flow is also available as an n8n workflow, if your team prefers to change steps visually." |
| 1:52 | README / HANDOVER.md | "It comes with tests, a handover guide, and retries that never lose a lead or send a reply twice. Happy to adapt it to your CRM and your rules." |

## After recording
- Cut dead time while Claude is thinking. Keep the 2-second pauses on results.
- Upload unlisted to YouTube or Loom. Put the link at the top of the repo README and in the Upwork portfolio item.
- Upwork portfolio title: "AI lead qualification → HubSpot with human approval (Python + n8n)".
