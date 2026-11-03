// Shared logic for the n8n workflow. build.py inlines this file into each Code node and fills
// CONFIG from criteria.yaml and the Python prompts, so both versions apply the same rules.
// Pure functions only (no n8n globals), so tests/test_n8n.py can run them with plain Node.

const CONFIG = /*CONFIG*/ {};

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function validateEnquiry(body) {
  const s = (v) => (typeof v === "string" ? v.trim() : "");
  const b = body && typeof body === "object" ? body : {};
  if (s(b.website)) return { ok: false, reason: "honeypot" };
  const enquiry = {
    name: s(b.name).slice(0, 120),
    email: s(b.email).toLowerCase().slice(0, 254),
    company: s(b.company).slice(0, 160) || null,
    message: s(b.message).slice(0, 5000),
  };
  if (!enquiry.name) return { ok: false, reason: "name missing" };
  if (!EMAIL_RE.test(enquiry.email)) return { ok: false, reason: "email invalid" };
  if (enquiry.message.length < 10) return { ok: false, reason: "message too short" };
  return { ok: true, enquiry };
}

function anthropicBody(model, system, user, schema, maxTokens) {
  const body = {
    model,
    max_tokens: maxTokens,
    system,
    messages: [{ role: "user", content: user }],
    output_config: { effort: CONFIG.effort, format: { type: "json_schema", schema } },
  };
  if (CONFIG.fallbackModels.includes(model)) body.fallbacks = "default";
  return body;
}

function buildQualifyRequest(enquiry, today) {
  const company = enquiry.company ? `\nCompany: ${enquiry.company}` : "";
  const user = `Today's date: ${today}\nFrom: ${enquiry.name}${company}\n<enquiry>\n${enquiry.message}\n</enquiry>`;
  return anthropicBody(CONFIG.model, CONFIG.qualifySystem, user, CONFIG.qualifySchema, 4000);
}

function claudeJson(response) {
  if (!response || response.type === "error") {
    throw new Error("Claude API error: " + JSON.stringify(response && response.error).slice(0, 300));
  }
  if (response.stop_reason === "refusal") throw new Error("Claude declined to answer");
  if (response.stop_reason === "max_tokens") throw new Error("Claude's answer was cut off (max_tokens)");
  const block = (response.content || []).find((b) => b.type === "text");
  if (!block) throw new Error("Claude returned no text");
  return JSON.parse(block.text);
}

const SAME = { "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " " };
function norm(text) {
  let t = String(text || "").replace(/[’‘“”–— ]/g, (c) => SAME[c]);
  t = t.replace(/\s+/g, " ").trim().replace(/^["'.,]+|["'.,]+$/g, "");
  return t.toLowerCase();
}

// Same rules as pipeline/scoring.py: a yes/no needs a quote that is really in the enquiry,
// and the tier comes from the weights in criteria.yaml.
function scoreAnswer(answer, message) {
  const corrections = [];
  const text = norm(message);
  const given = {};
  for (const c of answer.checks || []) {
    if (!CONFIG.criteria.some((k) => k.id === c.id)) corrections.push(`dropped unknown criterion '${c.id}'`);
    else if (given[c.id]) corrections.push(`dropped repeated answer for '${c.id}'`);
    else given[c.id] = c;
  }
  const checks = CONFIG.criteria.map((k) => {
    let c = given[k.id];
    if (!c) {
      corrections.push(`'${k.id}' was not answered; set to unknown`);
      return { id: k.id, met: "unknown", evidence: null };
    }
    if (c.met !== "unknown") {
      const ev = norm(c.evidence);
      if (!ev || !text.includes(ev)) {
        corrections.push(`'${k.id}' said ${c.met} without a quote from the enquiry; set to unknown`);
        return { id: k.id, met: "unknown", evidence: null };
      }
    }
    return { id: k.id, met: c.met, evidence: c.evidence || null };
  });
  const answers = Object.fromEntries(checks.map((c) => [c.id, c.met]));
  let tier, score;
  if (answer.is_spam) {
    tier = "spam"; score = 0;
  } else {
    score = CONFIG.criteria.filter((k) => answers[k.id] === "yes").reduce((a, k) => a + k.weight, 0);
    if (CONFIG.criteria.some((k) => k.required && answers[k.id] === "no")) tier = "poor";
    else if (score >= CONFIG.tiers.strong) tier = "strong";
    else if (score >= CONFIG.tiers.possible) tier = "possible";
    else tier = "poor";
  }
  return {
    tier, score, checks, corrections,
    is_spam: !!answer.is_spam,
    extracted: answer.extracted || {},
    missing_info: answer.is_spam ? [] : answer.missing_info || [],
    summary: answer.summary || "",
    next_action: CONFIG.nextAction[tier],
  };
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" }[c]));

function noteHtml(lead) {
  const rows = lead.checks.map((c) => `<li><b>${esc(c.id)}</b>: ${esc(c.met)}` +
    (c.evidence ? ` (&ldquo;${esc(c.evidence)}&rdquo;)` : "") + "</li>").join("");
  const missing = lead.missing_info.map((m) => `<li>${esc(m)}</li>`).join("");
  return `<p><b>Website enquiry: ${esc(lead.tier)} fit, score ${lead.score}/100</b></p>` +
    `<p>${esc(lead.summary)}</p><p><b>Criteria</b></p><ul>${rows}</ul>` +
    (missing ? `<p><b>Still to ask</b></p><ul>${missing}</ul>` : "") +
    `<p><b>Next step:</b> ${esc(lead.next_action)}</p>` +
    `<p><b>Original message</b></p><blockquote>${esc(lead.message)}</blockquote>` +
    `<p><i>Qualified by the n8n workflow (${esc(CONFIG.model)}).</i></p>`;
}

const LEAD_STATUS = { strong: "NEW", possible: "NEW", poor: "UNQUALIFIED" };

function hubspotUpsert(lead) {
  const parts = lead.name.trim().split(/\s+/);
  const props = {
    email: lead.email, firstname: parts[0], lastname: parts.slice(1).join(" "),
    lead_fit_tier: lead.tier, lead_fit_score: String(lead.score), hs_lead_status: LEAD_STATUS[lead.tier] || "NEW",
  };
  const company = lead.company || lead.extracted.company;
  if (company) props.company = company;
  return { inputs: [{ idProperty: "email", id: lead.email, properties: props }] };
}

function hubspotNote(contactId, html, now) {
  return {
    properties: { hs_timestamp: String(now), hs_note_body: html },
    associations: [{ to: { id: String(contactId) },
      types: [{ associationCategory: "HUBSPOT_DEFINED", associationTypeId: CONFIG.noteToContact }] }],
  };
}

function hubspotDeal(contactId, lead) {
  const label = lead.company || lead.extracted.company || lead.name;
  const props = {
    dealname: `${label}: ${lead.extracted.service || "project"} (website enquiry)`.slice(0, 200),
    pipeline: "default", dealstage: "appointmentscheduled",
  };
  if (lead.extracted.budget_usd) props.amount = String(lead.extracted.budget_usd);
  return {
    properties: props,
    associations: [{ to: { id: String(contactId) },
      types: [{ associationCategory: "HUBSPOT_DEFINED", associationTypeId: CONFIG.dealToContact }] }],
  };
}

function buildDraftRequest(lead) {
  const reasons = lead.checks.filter((c) => c.met === "no")
    .map((c) => `${c.id}: ${c.met}` + (c.evidence ? ` (${c.evidence})` : "")).join("; ") || "outside what we do";
  const goal = CONFIG.goals[lead.tier]
    .replace("{link}", CONFIG.reply.booking_link || "")
    .replace("{questions}", lead.missing_info.join(" | ") || "what they need")
    .replace("{reasons}", reasons);
  const user = `Goal: ${goal}\n\nFrom: ${lead.name}\nSummary: ${lead.summary}\n<enquiry>\n${lead.message}\n</enquiry>`;
  return anthropicBody(CONFIG.model, CONFIG.draftSystem, user, CONFIG.draftSchema, 3000);
}

const AMOUNT = "\\d(?:[\\d,.]*\\d)?";
const MONEY = new RegExp(`(?:[$€£₹]\\s?${AMOUNT}(?:\\s?k\\b)?|\\b${AMOUNT}\\s?(?:usd|eur|gbp|inr|dollars|euros|pounds|rupees|lakh)\\b)`, "gi");

// Same checks as pipeline/drafts.py check_draft().
function checkDraft(draft, lead) {
  const warnings = [];
  const text = `${draft.subject}\n${draft.body}`;
  const link = CONFIG.reply.booking_link;
  if (lead.tier === "strong" && link && !draft.body.includes(link)) warnings.push("Strong lead, but the booking link is missing.");
  if (lead.tier !== "strong" && link && draft.body.includes(link)) warnings.push("Booking link offered to a lead that is not a strong fit.");
  if (!CONFIG.reply.allow_prices) {
    const enquiry = lead.message.toLowerCase();
    const quoted = (text.match(MONEY) || []).filter((m) => !enquiry.includes(m.toLowerCase()));
    if (quoted.length) warnings.push(`Mentions a price the client did not give: ${quoted.join(", ")}.`);
  }
  if (/\[[^\]]{2,40}\]|\{[^}]{2,40}\}/.test(text)) warnings.push("Contains a placeholder in brackets.");
  const words = draft.body.split(/\s+/).filter(Boolean).length;
  if (words > 220) warnings.push(`Long for a first reply (${words} words).`);
  return warnings;
}

function approvalEmail(lead, draft, warnings, formUrl) {
  const lines = [
    `New ${lead.tier} lead: ${lead.name} <${lead.email}> (score ${lead.score}/100)`,
    "", lead.summary, "",
    ...lead.checks.map((c) => `- ${c.id}: ${c.met}` + (c.evidence ? ` ("${c.evidence}")` : "")),
    "", "Draft reply:", `Subject: ${draft.subject}`, "", draft.body, "",
  ];
  if (warnings.length) lines.push("CHECK BEFORE SENDING:", ...warnings.map((w) => "- " + w), "");
  lines.push(`Approve, edit or reject: ${formUrl}`, "", "Nothing is sent until you submit that form.");
  return lines.join("\n");
}

function replyNoteHtml(subject, body) {
  return `<p><b>Reply sent: ${esc(subject)}</b></p><p>${esc(body).replace(/\n/g, "<br>")}</p>` +
    "<p><i>Approved in the n8n workflow.</i></p>";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { CONFIG, validateEnquiry, buildQualifyRequest, claudeJson, scoreAnswer, noteHtml, hubspotUpsert,
    hubspotNote, hubspotDeal, buildDraftRequest, checkDraft, approvalEmail, replyNoteHtml, norm };
}
