/* Maple Bank payment help: customer self-service for Layer 3.
   Server text is always rendered as text (h() builds DOM nodes; no innerHTML). */
"use strict";

const app = document.getElementById("app");
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const money = (v) => v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const date = (v) => { if (!v) return "—"; const d = new Date(String(v).slice(0, 10) + "T00:00:00");
  return isNaN(d) ? String(v) : d.toLocaleDateString("en-CA", { weekday: "short", day: "numeric", month: "long" }); };
const label = (s) => s ? String(s).replace(/_/g, " ") : "";
const SNAPSHOT = "2026-09-28";   // "today" in the synthetic dataset

let token = null;
try { token = sessionStorage.getItem("mb.token"); } catch (e) { token = null; }
let me = null;

async function api(path, opts = {}) {
  const res = await fetch("/api/me" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (res.status === 401 && path !== "/login") { signOut(); throw new Error("Please sign in again."); }
  if (!res.ok) { const d = (data && (data.detail || data)) || {}; throw Object.assign(new Error(d.error || "Something went wrong. Please try again."), { data: d }); }
  return data;
}
function show(...nodes) { app.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false)); app.focus({ preventScroll: true }); window.scrollTo(0, 0); }
function back() { return h("div", { class: "back" }, h("button", { class: "link", onclick: home }, "← Back to my account")); }
function signOut() {
  token = null; me = null;
  try { sessionStorage.removeItem("mb.token"); } catch (e) { /* ignore */ }
  document.getElementById("signout").hidden = true;
  login();
}
document.getElementById("signout").addEventListener("click", signOut);

// ---------- sign in ----------
function login(msg) {
  const caseId = h("input", { id: "case", autocomplete: "off", placeholder: "CS-2026-614370", required: true });
  const last = h("input", { id: "last", autocomplete: "family-name", placeholder: "Ayers", required: true });
  const err = h("div", { class: "err", role: "alert" }, msg || "");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try {
      const out = await api("/login", { method: "POST", body: { case_id: caseId.value, last_name: last.value } });
      token = out.token; try { sessionStorage.setItem("mb.token", token); } catch (x) { /* ignore */ }
      home();
    } catch (x) { err.textContent = x.message; }
  } },
    h("label", { for: "case" }, "Reference number"), caseId, h("div", { class: "hint" }, "It's on the letter or text we sent you, and starts with CS-."),
    h("label", { for: "last" }, "Last name"), last, err,
    h("div", { class: "row" }, h("button", { class: "btn", type: "submit" }, "Continue")));
  show(h("h1", null, "Let's sort out your account together"),
    h("p", { class: "muted" }, "See what you owe, choose a payment date, or tell us if things are hard right now. No call needed."),
    h("div", { class: "card" }, form),
    h("p", { class: "hint" }, "Demo sign-in: reference CS-2026-614370 and last name Ayers, or CS-2026-855160 and Marshall."));
}

// ---------- home ----------
async function home() {
  document.getElementById("signout").hidden = false;
  show(h("p", { class: "muted" }, "Loading your account…"));
  try { me = await api(""); } catch (e) { return login(e.message); }
  const plan = me.active_plan, next = me.next_promise;
  const pending = (me.requests || []).filter((r) => r.status === "pending");
  show(
    h("h1", null, `Hi ${me.first_name || "there"}`),
    h("div", { class: "card" },
      h("div", { class: "owe" },
        h("div", null, h("div", { class: "muted" }, "Overdue now"), h("div", { class: "amt" }, money(me.overdue))),
        h("span", { class: "pill" }, `${me.days_past_due} days past due`)),
      h("p", { class: "muted", style: "margin-top:8px" }, `Total balance ${money(me.balance)} on your ${label(me.product)}. Reference ${me.case_id}.`),
      next ? h("p", null, h("span", { class: "pill ok" }, "Promise"), ` ${money(next.amount)} by ${date(next.due_date)}`) : null,
      plan ? h("p", null, h("span", { class: "pill ok" }, "Plan"), ` ${money(plan.proposed_payment_amount)} a month for ${plan.duration_months} months`) : null),
    suggestionCard(),
    h("h2", { class: "or" }, "Or choose for yourself"),
    h("div", { class: "actions" },
      h("button", { class: "tile", onclick: hardshipFlow }, h("b", null, "Money is tight right now"), h("span", null, "See options that lower or pause payments")),
      h("button", { class: "tile", onclick: promiseFlow }, h("b", null, "Choose a payment date"), h("span", null, "Promise an amount within 21 days")),
      h("button", { class: "tile", onclick: payFlow }, h("b", null, "Make a payment"), h("span", null, "Pay any amount now")),
      h("button", { class: "tile", onclick: chat }, h("b", null, "Ask a question"), h("span", null, "Get answers about your account")),
      h("button", { class: "tile", onclick: contactFlow }, h("b", null, "How we contact you"), h("span", null, "Change channels or ask us to stop")),
      h("button", { class: "tile", onclick: disputeFlow }, h("b", null, "Something looks wrong"), h("span", null, "Dispute a fee or a charge"))),
    pending.length ? h("div", { class: "card" }, h("h2", null, "Waiting for our team"),
      pending.map((r) => h("p", null, `${label(r.type)} · sent ${date(r.ts)} `, h("span", { class: "status pending" }, "pending review")))) : null,
    (me.promises || []).length ? h("div", { class: "card" }, h("h2", null, "Your payment promises"),
      h("table", null, h("tbody", null, me.promises.map((p) => h("tr", null, h("td", null, date(p.ptp_due_date)), h("td", { class: "n" }, money(p.ptp_amount)),
        h("td", null, h("span", { class: "status " + (p.ptp_status === "kept" ? "approved" : p.ptp_status === "broken" ? "rejected" : "pending") }, label(p.ptp_status)))))))) : null,
    (me.requests || []).length ? h("div", { class: "card" }, h("h2", null, "Your recent requests"),
      h("table", null, h("tbody", null, me.requests.slice(0, 8).map((r) => h("tr", null, h("td", null, label(r.type)),
        h("td", null, h("span", { class: "status " + r.status }, r.status === "recorded" ? "done" : r.status)),
        h("td", { class: "muted" }, r.decision_reason || "")))))) : null);
}

// ---------- "Not sure what to do next?" (a suggestion; it only opens one of the flows above) ----------
function suggestionCard() {
  const box = h("div");
  const done = (text) => box.replaceChildren(h("div", { class: "okmsg" }, text));
  const ACT = {
    hardship: ["Apply for help", () => hardshipFlow()], promise: ["Choose a payment date", () => promiseFlow()], pay: ["Make a payment", () => payFlow()],
    contact: ["Update how we contact you", () => contactFlow()], dispute: ["Question a charge", () => disputeFlow()],
    callback: ["Ask someone to call me", async () => {
      try { await api("/callback", { method: "POST", body: { time: "next business day", topic: "general" } }); done("Done. Someone from our team will call you on the next business day."); }
      catch (e) { box.replaceChildren(h("p", { class: "error" }, e.message)); } }],
  };
  const LABEL = "Show my AI next best step";
  const btnText = h("span", null, LABEL);
  const btn = h("button", { class: "ai-btn" }, h("span", { class: "sparkle", "aria-hidden": "true" }, "\u2726"), btnText);
  btn.addEventListener("click", async () => {
    btn.disabled = true; btnText.textContent = "Thinking about your options…";
    box.replaceChildren();
    let r;
    try { r = await api("/ai-nba", { method: "POST", body: {} }); }
    catch (e) { box.replaceChildren(h("div", { class: "ai-answer" }, h("p", { class: "muted" }, "We couldn't get a suggestion just now. You can still use the options below."))); return; }
    finally { btn.disabled = false; btnText.textContent = LABEL; }
    const s = r.suggestion, st = s.suggested_step, a = ACT[st.app_action], ai = r.source === "ai";
    const others = (s.other_options || []).filter((o) => ACT[o.app_action]);
    box.replaceChildren(h("div", { class: "ai-answer" },
      h("span", { class: "ai-pill" }, ai ? "\u2726 AI suggestion" : "Our standard suggestion"),
      ai ? null : h("p", { class: "muted" }, "The AI assistant isn't available right now, so this is the suggestion we give everyone in your situation."),
      h("h3", null, s.headline),
      h("p", null, h("b", null, st.title), st.description ? " " + st.description : ""),
      s.why_this_may_help ? h("p", { class: "muted" }, "Why this may help: " + s.why_this_may_help) : null,
      a ? h("div", { class: "row" }, h("button", { class: "btn", onclick: a[1] }, a[0])) : null,
      others.length ? h("p", { class: "hint" }, "Other options: ", others.map((o, i) => [i ? " · " : "", h("button", { class: "link", onclick: ACT[o.app_action][1] }, o.title)])) : null,
      s.reassurance ? h("p", { class: "muted" }, s.reassurance) : null,
      h("p", { class: "hint" }, r.notice)));
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  if (new URLSearchParams(location.search).get("ai") === "1") setTimeout(() => btn.click(), 0);      // demo link: open the suggestion straight away
  return h("section", { class: "ai-card", "aria-label": "AI next best step" },
    h("div", { class: "ai-eyebrow" }, h("span", { class: "sparkle", "aria-hidden": "true" }, "\u2726"), "AI next best step", h("span", { class: "ai-newtag" }, "New")),
    h("h2", null, "Not sure what to do next?"),
    h("p", null, "Our AI assistant looks at your account and suggests one clear next step, in plain words. Nothing changes until you choose, and a person reviews every request."),
    btn, box);
}

// ---------- hardship (self-service) ----------
const REASONS = [["reduced_income", "My hours or pay were cut"], ["job_loss", "I lost my job"], ["illness", "I'm ill or injured"],
  ["ei_waiting", "I'm waiting for Employment Insurance"], ["separation", "Separation or divorce"], ["bereavement", "A death in the family"],
  ["disaster_evacuation", "Fire, flood or evacuation"], ["rate_reset", "My payment went up"], ["other", "Something else"]];
function hardshipFlow() {
  const sel = h("select", { id: "reason" }, REASONS.map(([v, l]) => h("option", { value: v }, l)));
  show(back(), h("div", { class: "steps" }, h("span", { class: "on" }), h("span"), h("span")),
    h("h1", null, "We're here to help"),
    h("p", null, "Tell us what's changed. You don't need to send documents for options of 3 months or less."),
    h("div", { class: "card" }, h("label", { for: "reason" }, "What's happening?"), sel,
      h("div", { class: "row" }, h("button", { class: "btn", onclick: () => options(sel.value) }, "See my options"))),
    h("p", { class: "hint" }, "Asking about options never affects how we treat you. We won't ask you for a payment you've told us you can't afford."));
}
async function options(reason) {
  show(h("p", { class: "muted" }, "Finding options for your account…"));
  let d;
  try { d = await api("/options?reason=" + encodeURIComponent(reason)); } catch (e) { return show(back(), h("div", { class: "err" }, e.message)); }
  const elig = d.options.filter((o) => o.eligible), not = d.options.filter((o) => !o.eligible);
  let chosen = null;
  const note = h("textarea", { id: "note", placeholder: "Anything you'd like us to know (optional)" });
  const income = h("input", { id: "inc", type: "number", min: 0, step: "1", placeholder: "Optional" });
  const send = h("button", { class: "btn", disabled: true, onclick: async () => {
    send.disabled = true;
    try {
      await api("/hardship", { method: "POST", body: { program_id: chosen, reason, note: note.value || null, monthly_income: income.value ? Number(income.value) : null } });
      show(back(), h("div", { class: "steps" }, h("span", { class: "on" }), h("span", { class: "on" }), h("span", { class: "on" })),
        h("h1", null, "Request sent"),
        h("div", { class: "okmsg" }, "Our hardship team will review it and confirm with you, usually within 2 business days. While we review it, we won't call you asking for payment."),
        h("div", { class: "row" }, h("button", { class: "btn secondary", onclick: home }, "Back to my account")));
    } catch (e) { send.disabled = false; err.textContent = e.message; }
  } }, "Send to the hardship team");
  const err = h("div", { class: "err", role: "alert" });
  const cards = elig.map((o) => {
    const est = o.estimate || {};
    const c = h("div", { class: "option", role: "radio", tabindex: 0, "aria-checked": "false" },
      h("h2", null, o.name), h("p", null, o.plain),
      est.kind === "monthly" ? h("div", { class: "est" }, `About ${money(est.amount)} a month for ${est.months} month${est.months === 1 ? "" : "s"}`, h("span", { class: "muted" }, ` (usually ${money(est.regular)})`)) : null,
      est.kind === "rate" ? h("div", { class: "est" }, "Interest 9.99% for 6 months") : null,
      o.requires_income_evidence ? h("div", { class: "hint" }, "We'll ask for proof of income and expenses.") : null,
      o.approver_level !== "agent" ? h("div", { class: "hint" }, "A hardship specialist approves this option.") : null);
    const pick = () => { chosen = o.program_id; cards.forEach((x) => { x.classList.remove("sel"); x.setAttribute("aria-checked", "false"); }); c.classList.add("sel"); c.setAttribute("aria-checked", "true"); send.disabled = false; };
    c.addEventListener("click", pick); c.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), pick()));
    return c;
  });
  show(back(), h("div", { class: "steps" }, h("span", { class: "on" }), h("span", { class: "on" }), h("span")),
    h("h1", null, elig.length ? "Options for your account" : "Let's talk it through"),
    elig.length ? h("p", { class: "muted" }, "Estimates are based on your current payment. A person confirms the final amount with you.") :
      h("p", null, "None of our standard options fit your account right now. Send us a note and a specialist will contact you."),
    h("div", { role: "radiogroup", "aria-label": "Options" }, cards),
    h("div", { class: "card" }, h("label", { for: "inc" }, "Monthly take-home income"), income, h("label", { for: "note" }, "Notes"), note, err,
      h("div", { class: "row" }, elig.length ? send : h("button", { class: "btn", onclick: async () => {
        try { await api("/callback", { method: "POST", body: { time: "next business day", topic: "hardship" } }); show(back(), h("div", { class: "okmsg" }, "A specialist will call you on the next business day.")); }
        catch (e) { err.textContent = e.message; } } }, "Ask a specialist to call me"))),
    not.length ? h("details", null, h("summary", null, "Why some options aren't shown"),
      not.map((o) => h("p", { class: "hint" }, `${o.name}: ${o.why_not.join(" ")}`))) : null);
}

// ---------- promise ----------
function promiseFlow() {
  const max = new Date(SNAPSHOT + "T00:00:00"); max.setDate(max.getDate() + 21);
  const min = new Date(SNAPSHOT + "T00:00:00"); min.setDate(min.getDate() + 1);
  const iso = (d) => d.toISOString().slice(0, 10);
  const amt = h("input", { id: "amt", type: "number", min: "1", step: "0.01", value: me ? me.overdue : "" });
  const dt = h("input", { id: "dt", type: "date", min: iso(min), max: iso(max), value: iso(min) });
  const err = h("div", { class: "err", role: "alert" });
  show(back(), h("h1", null, "Choose a payment date"),
    h("p", { class: "muted" }, "Pick a date within the next 21 days, ideally on or after your payday. A payment that arrives within 2 business days of the date counts as on time."),
    h("div", { class: "card" }, h("label", { for: "amt" }, "Amount"), amt, h("div", { class: "hint" }, `Paying ${money(me && me.overdue)} brings your account up to date.`),
      h("label", { for: "dt" }, "Date"), dt, err,
      h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => {
        err.textContent = "";
        try {
          const r = await api("/promise", { method: "POST", body: { amount: Number(amt.value), date: dt.value } });
          show(back(), h("h1", null, "Thanks, it's set"), h("div", { class: "okmsg" }, `${money(r.payload.amount)} on ${date(r.payload.date)}. We'll send a reminder before the date.`),
            h("div", { class: "row" }, h("button", { class: "btn secondary", onclick: home }, "Back to my account")));
        } catch (e) { err.textContent = e.message; }
      } }, "Confirm my promise"))));
}

// ---------- pay ----------
function payFlow() {
  const amt = h("input", { id: "pay", type: "number", min: "1", step: "0.01", value: me ? me.overdue : "" });
  const err = h("div", { class: "err", role: "alert" });
  show(back(), h("h1", null, "Make a payment"),
    h("div", { class: "card" }, h("label", { for: "pay" }, "Amount"), amt, h("div", { class: "hint" }, "Demo only: no money moves."), err,
      h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => {
        try { const r = await api("/payment", { method: "POST", body: { amount: Number(amt.value) } });
          show(back(), h("h1", null, "Payment received"), h("div", { class: "okmsg" }, `${money(r.payload.amount)} received. Thank you.`), h("div", { class: "row" }, h("button", { class: "btn secondary", onclick: home }, "Back to my account"))); }
        catch (e) { err.textContent = e.message; }
      } }, "Pay now"))));
}

// ---------- contact preferences / cease ----------
function contactFlow() {
  const prefs = (me && me.contact_preferences) || {};
  const box = (k, l) => { const i = h("input", { type: "checkbox", id: "c_" + k, style: "width:auto;margin-right:8px" }); i.checked = prefs[k] === true; return [h("label", { for: "c_" + k, style: "font-weight:400" }, i, l), i]; };
  const [lc, cc] = box("call", "Phone calls"), [ls, cs] = box("sms", "Text messages"), [le, ce] = box("email", "Email");
  const msg = h("div", { role: "status" });
  const send = async (kind) => {
    try { await api("/contact", { method: "POST", body: { kind, channels: kind === "preferences" ? { call: cc.checked, sms: cs.checked, email: ce.checked } : null } });
      msg.replaceChildren(h("div", { class: "okmsg" }, kind === "cease" ? "Request sent. A person reviews it and confirms in writing. Letters and statements we're required to send will continue." : "Preferences sent. A person confirms the change.")); }
    catch (e) { msg.replaceChildren(h("div", { class: "err" }, e.message)); }
  };
  show(back(), h("h1", null, "How we contact you"),
    h("div", { class: "card" }, h("h2", null, "Channels you're happy for us to use"), lc, ls, le,
      h("div", { class: "row" }, h("button", { class: "btn", onclick: () => send("preferences") }, "Save preferences"))),
    h("div", { class: "card" }, h("h2", null, "Ask us to stop contacting you"),
      h("p", null, "We'll stop calls, texts and emails about this account. We'll still send letters and statements we're required to send, and the balance stays owing."),
      h("div", { class: "row" }, h("button", { class: "btn secondary", onclick: () => send("cease") }, "Send a stop-contact request"))), msg);
}

// ---------- dispute ----------
function disputeFlow() {
  const t = h("select", { id: "dt" }, [["late_fee", "A late fee"], ["payment_not_applied", "A payment wasn't applied"], ["billing_error", "A wrong amount or charge"], ["fraud", "I didn't make this charge"]].map(([v, l]) => h("option", { value: v }, l)));
  const d = h("textarea", { id: "dd", placeholder: "What happened?" });
  const msg = h("div", { role: "status" });
  show(back(), h("h1", null, "Something looks wrong"),
    h("p", { class: "muted" }, "While we investigate, we won't ask you to pay the amount you're disputing."),
    h("div", { class: "card" }, h("label", { for: "dt" }, "What's wrong?"), t, h("label", { for: "dd" }, "Details"), d,
      h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => {
        try { await api("/dispute", { method: "POST", body: { dispute_type: t.value, details: d.value } }); msg.replaceChildren(h("div", { class: "okmsg" }, "Thanks. We'll investigate and get back to you.")); }
        catch (e) { msg.replaceChildren(h("div", { class: "err" }, e.message)); } } }, "Send"))), msg);
}

// ---------- assistant ----------
function chat() {
  const log = h("div", { class: "chat", "aria-live": "polite" });
  const input = h("input", { placeholder: "Type your question", "aria-label": "Your message", maxlength: 1000 });
  const add = (who, text, extra) => { const m = h("div", { class: "msg " + who }, text, extra || null); log.append(m); log.scrollTop = log.scrollHeight; };
  const ask = async (text) => {
    if (!text.trim()) return;
    add("me", text); input.value = "";
    try {
      const r = await api("/chat", { method: "POST", body: { message: text } });
      const extras = [];
      if (r.citations && r.citations.length) extras.push(h("div", { class: "cite" }, "Source: " + r.citations.join(", ")));
      for (const c of r.cards || []) {
        if (c.type === "quick_replies") extras.push(h("div", { class: "chips", style: "margin-top:8px" }, c.items.map((q) => h("button", { onclick: () => ask(q) }, q))));
        if (c.type === "hardship_options") extras.push(h("div", { style: "margin-top:8px" }, (c.options || []).map((o) => h("div", { class: "hint" }, `• ${o.name}: ${o.plain}`)),
          h("button", { class: "btn", style: "margin-top:8px", onclick: () => options(c.reason) }, "Choose an option")));
        if (c.type === "promise_form") extras.push(h("button", { class: "btn", style: "margin-top:8px", onclick: promiseFlow }, "Choose a date"));
        if (c.type === "pay_form") extras.push(h("button", { class: "btn", style: "margin-top:8px", onclick: payFlow }, "Make a payment"));
        if (c.type === "contact_form") extras.push(h("button", { class: "btn", style: "margin-top:8px", onclick: contactFlow }, "Contact preferences"));
        if (c.type === "dispute_form") extras.push(h("button", { class: "btn", style: "margin-top:8px", onclick: disputeFlow }, "Tell us what's wrong"));
        if (c.type === "callback_form") extras.push(h("button", { class: "btn", style: "margin-top:8px", onclick: async () => {
          await api("/callback", { method: "POST", body: { time: "next business day", topic: c.topic } }); add("bot", "Done. Someone from our team will call you on the next business day."); } }, "Request a callback"));
        if (c.type === "actions") extras.push(h("div", { class: "chips", style: "margin-top:8px" },
          h("button", { onclick: payFlow }, "Pay now"), h("button", { onclick: promiseFlow }, "Choose a date"), h("button", { onclick: hardshipFlow }, "See options")));
      }
      add("bot", r.reply, extras);
    } catch (e) { add("bot", e.message); }
  };
  show(back(), h("h1", null, "Ask a question"),
    h("p", { class: "muted" }, "Answers come from your account and Maple Bank's current policies. For anything that changes your account, a person confirms."),
    h("div", { class: "card" }, log, h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, h("button", { class: "btn", type: "submit" }, "Send"))));
  ask("hi");
  input.focus();
}

token ? home() : login();
