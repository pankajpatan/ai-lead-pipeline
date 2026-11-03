"""Build the n8n workflows from criteria.yaml, the Python prompts and n8n/src/logic.js.

    .venv/bin/python n8n/build.py                      # writes n8n/lead-pipeline.json and n8n/error-alert.json
    .venv/bin/python n8n/build.py --owner-email you@yourco.com --mail-from team@yourco.com

Rebuild after changing criteria.yaml or the prompts, then re-import the workflow in n8n.
Checked against n8n 2.46.4 (node versions: Webhook 2.2, Code 2, HTTP Request 4.2, IF 2.2,
Wait 1.1, Send Email 2.1).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.claude import FALLBACK_MODELS, system_prompt  # noqa: E402
from pipeline.criteria import load_criteria  # noqa: E402
from pipeline.drafts import GOALS, ClaudeDrafter  # noqa: E402
from pipeline.service import NEXT_ACTION  # noqa: E402

ANTHROPIC_VERSION = "2026-10-21"
HUBSPOT = "https://api.hubapi.com/crm/objects/2026-09"
# Placeholder credential IDs. After import, pick your own credentials in each node.
CRED = {
    "anthropic": {"httpHeaderAuth": {"id": "anthropicApiKey1", "name": "Anthropic API key"}},
    "hubspot": {"httpHeaderAuth": {"id": "hubspotService01", "name": "HubSpot service key"}},
    "smtp": {"smtp": {"id": "smtpAccount00001", "name": "SMTP account"}},
}

NULLABLE_STR = {"type": ["string", "null"]}
QUALIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_spam", "checks", "extracted", "missing_info", "summary"],
    "properties": {
        "is_spam": {"type": "boolean"},
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "met", "evidence"],
            "properties": {"id": {"type": "string"}, "met": {"type": "string", "enum": ["yes", "no", "unknown"]},
                           "evidence": {"type": "string"}}}},
        "extracted": {
            "type": "object", "additionalProperties": False,
            "required": ["service", "budget_usd", "budget_text", "timeline", "company", "role"],
            "properties": {"service": NULLABLE_STR, "budget_usd": {"type": ["integer", "null"]},
                           "budget_text": NULLABLE_STR, "timeline": NULLABLE_STR, "company": NULLABLE_STR,
                           "role": NULLABLE_STR}},
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}
DRAFT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subject", "body"],
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}}


def config(criteria_path: Path, model: str, effort: str) -> dict:
    criteria = load_criteria(criteria_path)
    drafter = ClaudeDrafter(model=model, effort=effort, client=object())
    return {
        "model": model, "effort": effort, "fallbackModels": sorted(FALLBACK_MODELS),
        "qualifySystem": system_prompt(criteria), "qualifySchema": QUALIFY_SCHEMA,
        "draftSystem": drafter._system(criteria), "draftSchema": DRAFT_SCHEMA,
        "goals": GOALS, "nextAction": NEXT_ACTION,
        "criteria": [c.model_dump() for c in criteria.criteria], "tiers": criteria.tiers,
        "reply": criteria.reply.model_dump(), "noteToContact": 202, "dealToContact": 3,
    }


def code(logic: str, snippet: str) -> str:
    return logic + "\n// ---- this node ----\n" + snippet.strip() + "\n"


def http(name, pos, url, body_expr, cred, headers=None):
    params = {
        "method": "POST", "url": url,
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "sendBody": True, "specifyBody": "json", "jsonBody": body_expr,
        "options": {"timeout": 120000},
    }
    if headers:
        params["sendHeaders"] = True
        params["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
    return {"parameters": params, "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": pos, "credentials": CRED[cred],
            # Retries 429/5xx and network errors; a 4xx after retries fails the run -> error workflow.
            "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000}


def if_node(name, pos, conditions, combinator="and"):
    return {"parameters": {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": [{"id": f"{name}-{i}", **c} for i, c in enumerate(conditions)],
        "combinator": combinator}, "options": {}},
        "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": pos}


def equals(left, right):
    return {"leftValue": left, "rightValue": right, "operator": {"type": "string", "operation": "equals"}}


def code_node(name, pos, logic, snippet, each=True):
    params = {"jsCode": code(logic, snippet)}
    if each:
        params["mode"] = "runOnceForEachItem"
    return {"parameters": params, "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos}


def email(name, pos, to, subject, text, mail_from, reply_to=None):
    options = {"appendAttribution": False}
    if reply_to:
        options["replyTo"] = reply_to
    return {"parameters": {"fromEmail": mail_from, "toEmail": to, "subject": subject, "emailFormat": "text",
                           "text": text, "options": options},
            "name": name, "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1, "position": pos,
            "credentials": CRED["smtp"], "webhookId": None}


def noop(name, pos):
    return {"parameters": {}, "name": name, "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": pos}


def lead_pipeline(cfg: dict, owner: str, mail_from: str, reply_to: str | None) -> dict:
    logic = (ROOT / "n8n" / "src" / "logic.js").read_text(encoding="utf-8")
    logic = logic.replace("/*CONFIG*/ {}", json.dumps(cfg, ensure_ascii=False))
    anthropic_headers = {"anthropic-version": ANTHROPIC_VERSION, "anthropic-beta": "server-side-fallback-2026-07-01"}

    nodes = [
        {"parameters": {"httpMethod": "POST", "path": "lead-intake", "responseMode": "onReceived", "options": {}},
         "name": "Enquiry webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.2, "position": [0, 300],
         "webhookId": "7d3702af-1468-5165-8d1e-d56b1777bfdc"},
        code_node("Validate enquiry", [220, 300], logic, """
const out = [];
for (const item of $input.all()) {
  const r = validateEnquiry(item.json.body);
  if (!r.ok) continue;  // honeypot or invalid: stop quietly, like the Python app
  const today = new Date().toISOString().slice(0, 10);
  out.push({ json: { ...r.enquiry, claude_request: buildQualifyRequest(r.enquiry, today) } });
}
return out;""", each=False),
        http("Claude: qualify", [440, 300], "https://api.anthropic.com/v1/messages",
             "={{ JSON.stringify($json.claude_request) }}", "anthropic", anthropic_headers),
        code_node("Score in code", [660, 300], logic, """
const lead = { ...$('Validate enquiry').item.json };
delete lead.claude_request;
const scored = { ...lead, ...scoreAnswer(claudeJson($json), lead.message) };
scored.hubspot_upsert = hubspotUpsert(scored);
return { json: scored };"""),
        if_node("Spam?", [880, 300], [{"leftValue": "={{ $json.is_spam }}", "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}]),
        noop("Ignore spam", [1100, 140]),
        http("HubSpot: upsert contact", [1100, 400], f"{HUBSPOT}/contacts/batch/upsert",
             "={{ JSON.stringify($json.hubspot_upsert) }}", "hubspot"),
        code_node("Prepare CRM records", [1320, 400], logic, """
const lead = $('Score in code').item.json;
const contactId = $json.results[0].id;
return { json: { ...lead, contact_id: contactId,
  note: hubspotNote(contactId, noteHtml(lead), Date.now()),
  deal: hubspotDeal(contactId, lead),
  draft_request: buildDraftRequest(lead) } };"""),
        http("HubSpot: add note", [1540, 240], f"{HUBSPOT}/notes", "={{ JSON.stringify($json.note) }}", "hubspot"),
        if_node("Strong fit?", [1540, 400], [equals("={{ $json.tier }}", "strong")]),
        http("HubSpot: create deal", [1760, 400], f"{HUBSPOT}/deals", "={{ JSON.stringify($json.deal) }}", "hubspot"),
        http("Claude: draft reply", [1540, 580], "https://api.anthropic.com/v1/messages",
             "={{ JSON.stringify($json.draft_request) }}", "anthropic", anthropic_headers),
        code_node("Check draft", [1760, 580], logic, """
const lead = { ...$('Prepare CRM records').item.json };
for (const k of ['note', 'deal', 'draft_request', 'hubspot_upsert']) delete lead[k];
const d = claudeJson($json);
const draft = { subject: d.subject.trim(), body: d.body.trim() };
const warnings = checkDraft(draft, lead);
return { json: { ...lead, draft, warnings, approval_text: approvalEmail(lead, draft, warnings, '{FORM_URL}') } };"""),
        email("Email owner for approval", [1980, 580], owner,
              "=Approve reply: {{ $json.tier }} lead from {{ $json.name }}",
              "={{ $json.approval_text.replace('{FORM_URL}', $execution.resumeFormUrl) }}", mail_from),
        {"parameters": {
            "resume": "form",
            "formTitle": "Reply to this lead?",
            "formDescription": "The draft is in the email you received. Choose what to do. Nothing is sent until you submit.",
            "formFields": {"values": [
                {"fieldLabel": "Decision", "fieldType": "dropdown", "requiredField": True,
                 "fieldOptions": {"values": [{"option": "Send the reply"}, {"option": "Send my edited version"},
                                             {"option": "Don't reply"}]}},
                {"fieldLabel": "Edited subject", "fieldType": "text"},
                {"fieldLabel": "Edited reply", "fieldType": "textarea"},
            ]},
            "limitWaitTime": True, "limitType": "afterTimeInterval", "resumeAmount": 72, "resumeUnit": "hours",
            "options": {}},
         "name": "Wait for decision", "type": "n8n-nodes-base.wait", "typeVersion": 1.1, "position": [2200, 580],
         "webhookId": "84305242-7e20-5333-aa3b-e36001f2a3d9"},
        if_node("Approved?", [2420, 580], [equals("={{ $json.Decision }}", "Send the reply"),
                                            equals("={{ $json.Decision }}", "Send my edited version")], "or"),
        noop("Not replying", [2640, 740]),
        code_node("Final reply", [2640, 500], logic, """
const lead = $('Check draft').item.json;
const form = $json;
const edited = form['Decision'] === 'Send my edited version';
const subject = (edited && (form['Edited subject'] || '').trim()) || lead.draft.subject;
const body = (edited && (form['Edited reply'] || '').trim()) || lead.draft.body;
return { json: { ...lead, final: { subject, body, edited, warnings: checkDraft({ subject, body }, lead) },
  reply_note: hubspotNote(lead.contact_id, replyNoteHtml(subject, body), Date.now()) } };"""),
        email("Send reply to lead", [2860, 500], "={{ $json.email }}", "={{ $json.final.subject }}",
              "={{ $json.final.body }}", f"{cfg['reply']['sender_name']} <{mail_from}>", reply_to),
        http("HubSpot: log reply", [3080, 500], f"{HUBSPOT}/notes",
             "={{ JSON.stringify($('Final reply').item.json.reply_note) }}", "hubspot"),
    ]
    for n in nodes:
        if n.get("webhookId") is None:
            n.pop("webhookId", None)

    def link(*targets):
        return {"main": [[{"node": t, "type": "main", "index": 0} for t in targets]]}

    connections = {
        "Enquiry webhook": link("Validate enquiry"),
        "Validate enquiry": link("Claude: qualify"),
        "Claude: qualify": link("Score in code"),
        "Score in code": link("Spam?"),
        "Spam?": {"main": [[{"node": "Ignore spam", "type": "main", "index": 0}],
                           [{"node": "HubSpot: upsert contact", "type": "main", "index": 0}]]},
        "HubSpot: upsert contact": link("Prepare CRM records"),
        "Prepare CRM records": link("HubSpot: add note", "Strong fit?", "Claude: draft reply"),
        "Strong fit?": {"main": [[{"node": "HubSpot: create deal", "type": "main", "index": 0}], []]},
        "Claude: draft reply": link("Check draft"),
        "Check draft": link("Email owner for approval"),
        "Email owner for approval": link("Wait for decision"),
        "Wait for decision": link("Approved?"),
        "Approved?": {"main": [[{"node": "Final reply", "type": "main", "index": 0}],
                               [{"node": "Not replying", "type": "main", "index": 0}]]},
        "Final reply": link("Send reply to lead"),
        "Send reply to lead": link("HubSpot: log reply"),
    }
    return {"name": "AI lead pipeline", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True},
            "pinData": {}, "active": False, "meta": {"templateCredsSetupCompleted": False},
            "tags": []}


def error_alert(owner: str, mail_from: str) -> dict:
    nodes = [
        {"parameters": {}, "name": "When the lead pipeline fails", "type": "n8n-nodes-base.errorTrigger",
         "typeVersion": 1, "position": [0, 300]},
        email("Email owner", [240, 300], owner, "=Lead pipeline failed at {{ $json.execution.lastNodeExecuted }}",
              "=A lead could not be processed.\n\nStep: {{ $json.execution.lastNodeExecuted }}\n"
              "Error: {{ $json.execution.error.message }}\n\nOpen the run to retry it: {{ $json.execution.url }}",
              mail_from),
    ]
    return {"name": "AI lead pipeline: error alert", "nodes": nodes,
            "connections": {"When the lead pipeline fails": {"main": [[{"node": "Email owner", "type": "main",
                                                                          "index": 0}]]}},
            "settings": {"executionOrder": "v1"}, "pinData": {}, "active": False, "tags": []}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--criteria", default=str(ROOT / "criteria.yaml"))
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--effort", default="low")
    p.add_argument("--owner-email", default="owner@example.com")
    p.add_argument("--mail-from", default="team@example.com")
    p.add_argument("--reply-to", default=None)
    p.add_argument("--out", default=str(ROOT / "n8n"))
    args = p.parse_args()

    cfg = config(Path(args.criteria), args.model, args.effort)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, wf in (("lead-pipeline.json", lead_pipeline(cfg, args.owner_email, args.mail_from, args.reply_to)),
                     ("error-alert.json", error_alert(args.owner_email, args.mail_from))):
        (out / name).write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out / name} ({len(wf['nodes'])} nodes)")


if __name__ == "__main__":
    main()
