/* C360 Terminal: employee console for Layer 3.
   All server text is rendered with textContent (via h()), never as HTML. */
"use strict";

const $ = (s) => document.querySelector(s);
const main = $("#main");

// ---------- tiny DOM builder (text only, no innerHTML) ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.setAttribute("style", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const svg = (tag, attrs) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
};

// ---------- formatting (format only; numbers come from the server) ----------
const fmt = {
  money: (v) => v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  money0: (v) => v === null || v === undefined ? "—" : "$" + Math.round(Number(v)).toLocaleString("en-CA"),
  big: (v) => { if (v === null || v === undefined) return "—"; const n = Number(v);
    return n >= 1e6 ? "$" + (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? "$" + (n / 1e3).toFixed(0) + "K" : "$" + n.toFixed(0); },
  pct: (v, d = 1) => v === null || v === undefined ? "—" : (Number(v) * 100).toFixed(d) + "%",
  int: (v) => v === null || v === undefined ? "—" : Number(v).toLocaleString("en-CA"),
  num: (v, d = 1) => v === null || v === undefined ? "—" : Number(v).toFixed(d),
  date: (v) => { if (!v) return "—"; const d = new Date(String(v).slice(0, 10) + "T00:00:00");
    return isNaN(d) ? String(v) : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }); },
  ts: (v) => v ? String(v).slice(0, 16).replace("T", " ") : "—",
  label: (s) => s ? String(s).replace(/_/g, " ") : "—",
};
const BUCKETS = ["current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "180+"];
const RAMP = { "current": "var(--b-current)", "1-30": "var(--b-1-30)", "31-60": "var(--b-31-60)", "61-90": "var(--b-61-90)",
  "91-120": "var(--b-91-120)", "121-150": "var(--b-121-150)", "151-180": "var(--b-151-180)", "180+": "var(--b-180)" };
const bucket = (b) => b ? h("span", { class: "bucket", style: `background:${RAMP[b] || "#333"}`, title: "days past due bucket" }, b) : "—";
const yes = (v) => v === true || v === "true";

// ---------- plain language: codes the data uses -> words an agent reads ----------
const PLAIN = {
  hardship: { job_loss: "Lost job", reduced_income: "Reduced hours or pay", illness: "Illness", disability: "Disability",
    separation: "Separation or divorce", ei_waiting: "Waiting for EI", disaster_evacuation: "Disaster or evacuation",
    rate_reset: "Payment went up after rate reset", bereavement: "Bereavement", strike_lockout: "Strike or lockout",
    other: "Other / not stated", "not recorded": "Not recorded" },
  product: { card: "Credit card", personal_loan: "Personal loan", auto_loan: "Car loan", loc: "Line of credit",
    overdraft: "Overdraft", mortgage: "Mortgage" },
  role: { agent: "Agent", hardship_specialist: "Hardship specialist", team_lead: "Team lead" },
  assign: { special_handling: "special handling team", insolvency_hold: "insolvency team", representative: "the customer's representative",
    disputes: "disputes team", hardship_specialist: "a hardship specialist", certified_agent: "an agent certified for vulnerable customers" },
};
const plain = (map, v) => v === null || v === undefined ? "—" : (PLAIN[map][v] || fmt.label(v));
function scriptLabel(code) {
  if (!code) return "—";
  const lang = /-FR$/.test(code) ? "French" : "English";
  if (/VUL/.test(code)) return `Vulnerable-customer script (${lang})`;
  const stage = /EARLY/.test(code) ? "early stage" : /LATE/.test(code) ? "late stage" : "mid stage";
  return `Standard call script, ${stage} (${lang})`;
}
const GLOSSARY = [
  ["Days late", "How many days the payment is overdue (the data calls it DPD, days past due)."],
  ["Days-late band", "Days late grouped: current, 1-30, 31-60 … 180+. Accounts are written off at 181 days."],
  ["Reached the customer", "We spoke to the customer themselves, not voicemail or someone else (\"right-party contact\", RPC)."],
  ["Promise", "The customer agreed to pay an amount by a date (\"promise to pay\", PTP). Kept = paid in full on time."],
  ["Hardship", "The customer can't pay because of a life event (job loss, illness …). Policy: offer help before asking for payment."],
  ["Queue", "Which team works the case: early, mid or late stage, hardship, disputes, recoveries …"],
  ["Postal area", "The first 3 characters of the postal code (e.g. M5V), about one neighbourhood."],
  ["Next best action", "What the system suggests doing next, with the reasons. Nothing happens until a person approves it."],
  ["Promise risk", "A model's estimate of the chance a promise will not be kept in full. Advice only."],
];

// ---------- state + API ----------
const state = {
  user: storage("c360.user") || "demo.agent",
  role: storage("c360.role") || "agent",
  view: null,
  health: null,
};
function storage(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
}
async function api(path, opts = {}) {
  const res = await fetch("/api/l3" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-C360-User": state.user, "X-C360-Role": state.role },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const rid = res.headers.get("X-C360-Request-Id");
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) {
    const d = (data && (data.detail || data)) || {};
    const err = new Error(d.error || `Request failed (${res.status})`);
    err.status = res.status; err.data = d; err.rid = rid;
    throw err;
  }
  return data;
}
function errorBox(e) {
  return h("div", { class: "err" }, e.message, e.rid ? ` (request ${e.rid})` : "");
}

// ---------- shell ----------
const FKEYS = [
  ["F1", "HELP", "help"], ["F2", "OVERVIEW", "mon"], ["F3", "WORKLIST", "work"], ["F4", "CUSTOMER", "cust"],
  ["F5", "CALL REVIEW", "call"], ["F6", "APPROVALS", "appr"], ["F7", "CALL QUALITY", "qa"], ["F8", "POLICY", "pol"],
  ["F9", "GROUPS", "groups"], ["F10", "ASK", "ask"],
];
function renderShell() {
  const fk = $("#fkeys");
  fk.replaceChildren(...FKEYS.map(([k, label, view]) =>
    h("button", { class: state.view === view ? "on" : "", onclick: () => route(view === "cust" ? (state.lastCustomer || "") : view === "call" ? "CALL " + (state.lastCall || "") : view) },
      h("span", { class: "k" }, k), label)));
  const roleSel = h("select", { "aria-label": "Role", onchange: (e) => { state.role = e.target.value; storage("c360.role", state.role); renderShell(); rerender(); } },
    ...["agent", "hardship_specialist", "team_lead"].map((r) => h("option", { value: r, selected: r === state.role }, "ROLE: " + plain("role", r).toUpperCase())));
  const hl = state.health;
  $("#status").replaceChildren(
    h("span", null, "DATA AS OF ", h("b", null, hl ? fmt.date(hl.snapshot_date).toUpperCase() : "…")),
    h("span", { title: "Where customer records come from: local store, Layer 1 MongoDB record, or the API" }, "SOURCE ", h("b", null, hl ? hl.source.toUpperCase() : "…")),
    h("span", null, "USER ", h("b", null, state.user)), roleSel);
}
let lastRoute = "mon";
function rerender() { route(lastRoute, true); }

async function route(input, silent) {
  const raw = (input || "").trim();
  const cmd = raw.toUpperCase();
  lastRoute = raw || "mon";
  if (!silent) $("#cmd").value = "";
  const [head, ...rest] = cmd.split(/\s+/);
  const arg = rest.join(" ");
  try {
    if (!cmd || head === "MON" || head === "OVERVIEW") return viewMon();
    if (head === "HELP" || head === "?") return viewHelp();
    if (head === "WORK" || head === "WORKLIST") return viewWork(arg);
    if (head === "GROUP" || head === "GROUPS") return viewGroups(arg);
    if (head === "ASK") return viewAsk(raw.replace(/^\S+\s*/, ""));
    if (head === "LOG") return viewLog(arg);
    if (head === "CONV") return viewConv(arg);
    if (head === "AREA" || head === "PINCODE" || head === "POSTAL") return viewGroups("AREA " + arg);
    if (head === "APPR" || head === "APPROVALS") return viewAppr();
    if (head === "QA" || head === "QUALITY") return viewQa();
    if (head === "POL" || head === "POLICY") return viewPol(raw.replace(/^\S+\s*/, ""));
    if (head === "CALL") return arg ? viewCall(arg) : viewHelp("Type CALL followed by a transcript id, e.g. CALL 5010384.");
    if (head === "CUST" || head === "CUSTOMER") return arg ? viewCust(arg) : viewHelp("Type a customer, case or account id, e.g. CS-2026-614370.");
    if (head === "ROLE" && arg) { state.role = arg.toLowerCase(); storage("c360.role", state.role); renderShell(); return viewMon(); }
    if (/^(CS-\d{4}-\d+|C360-[A-Z]{3}-\d{4}|[A-Z]{3}-\d{4}|(C|L|CHQ|SAV|TFS|USD)-\d+)$/.test(head)) return viewCust(head);
    if (/^\d{4,8}$/.test(head)) return viewCall(head);
    return viewHelp(`Unknown command "${raw}".`);
  } catch (e) {
    main.replaceChildren(errorBox(e));
  }
}
function setView(v, ...nodes) {
  state.view = v;
  renderShell();
  main.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
  main.focus({ preventScroll: true });
}
function title(t, sub) { return h("div", { class: "title" }, h("h1", null, t), sub ? h("span", { class: "sub" }, sub) : null); }
function panel(t, body, right) { return h("section", { class: "panel" }, h("h2", null, t, right ? h("span", { class: "r" }, right) : null), h("div", { class: "body" }, body)); }
const loading = (what) => h("div", { class: "loading" }, `LOADING ${what}…`);

// ---------- HELP ----------
function viewHelp(msg) {
  const rows = [
    ["CS-2026-614370", "Open a customer: type a case id, CRM id (ZOA-3450), C360 id or account id (C-818030)"],
    ["OVERVIEW", "The whole portfolio: open cases, how late they are, teams, calls, promises, hardship (charts)"],
    ["WORKLIST [TEAM] [FILTER]", "Cases to work, most urgent first. Filters: HARDSHIP, PROMISE (due now), BROKEN (2+ broken promises), DISPUTE, VULNERABLE, HOLD"],
    ["GROUPS [SECTOR|INDUSTRY|EMPLOYER|AREA|CITY|PROVINCE]", "Compare open cases by job sector, industry, employer, postal area, city or province"],
    ["AREA M5V 2T6", "Everyone behind on payments in a postal area (a full postal code or its first 3 characters)"],
    ["ASK <question>", "Ask anything about the portfolio in plain English (Gemini, via Layer 2). Shows the SQL or the documents behind every answer"],
    ["LOG CS-2026-614370", "Log a conversation you just had: paste the transcript or your notes, check what was found, save it to the customer's file"],
    ["CONV CNV-…", "Open a conversation that was logged here (read only)"],
    ["CALL 5010384", "Review a recorded call: transcript, what was said, summary, quality score"],
    ["APPROVALS", "Customer requests waiting for a person (hardship plans, stop-contact, disputes, callbacks)"],
    ["QUALITY", "How accurate the automatic call review is, checked against 500 human-labelled calls"],
    ["POLICY <question>", "Search the bank's current policies (old versions are never shown)"],
    ["ROLE TEAM_LEAD", "Switch role for the demo: agent, hardship specialist or team lead"],
  ];
  setView("help", title("HELP", "type a command and press Enter, or use the F-keys"),
    msg ? h("div", { class: "banner hard" }, h("b", null, "NOTE"), msg) : null,
    h("div", { class: "grid g-2" },
      panel("COMMANDS", h("table", { class: "help" }, h("tbody", null, rows.map(([c, d]) => h("tr", null, h("td", null, h("code", null, c)), h("td", null, d)))))),
      panel("WORDS USED ON THESE SCREENS", h("table", null, h("tbody", null, GLOSSARY.map(([w, d]) => h("tr", null, h("td", { style: "white-space:nowrap" }, h("b", null, w)), h("td", null, d))))))),
    h("p", { class: "note" }, "Every suggested action needs a person to approve it. Approve stays switched off while the action breaks one of the customer's contact rules or a policy."));
}

// ---------- charts (numbers come from the server; these only draw them) ----------
// Horizontal bars. o: label(r), value(r), text(r), color(r)?, title(r)?, onclick(r)?
function hbars(rows, o) {
  if (!rows || !rows.length) return h("div", { class: "muted" }, "No data.");
  const max = Math.max(...rows.map(o.value), 1);
  return h("div", { class: "hbars" }, rows.map((r) => {
    const click = o.onclick ? () => o.onclick(r) : null;
    return h("div", { class: "row" + (click ? " click" : ""), onclick: click, tabindex: click ? 0 : null, title: o.title ? o.title(r) : null,
      onkeydown: click ? (e) => e.key === "Enter" && click() : null },
      h("span", { class: "lbl" }, o.label(r)),
      h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(100 * o.value(r) / max).toFixed(1)}%;background:${o.color ? o.color(r) : "var(--amber)"}` })),
      h("span", { class: "n" }, o.text(r)));
  }));
}
// Column chart. o: x(r) short label, y(r), tip(r), fmtY(v), color(r)?
function columns(rows, o) {
  if (!rows || !rows.length) return h("div", { class: "muted" }, "No data.");
  const W = 600, H = 140, L = 44, B = 18;
  const max = Math.max(...rows.map(o.y), 1);
  const bw = (W - L) / rows.length;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark tall", role: "img", "aria-label": o.aria || "chart" });
  [0, 0.5, 1].forEach((f) => {
    const y = H - B - (H - B - 8) * f;
    s.append(svg("line", { x1: L, x2: W, y1: y, y2: y, stroke: "#23272f" }));
    const t = svg("text", { x: 0, y: y + 3, fill: "#8a8f98", "font-size": 10 }); t.textContent = o.fmtY(max * f); s.append(t);
  });
  rows.forEach((r, i) => {
    const bh = (H - B - 8) * o.y(r) / max, x = L + i * bw;
    const rect = svg("rect", { x: x + 2, y: H - B - bh, width: Math.max(bw - 4, 1), height: bh, fill: o.color ? o.color(r) : "#ffb000", opacity: 0.8 });
    const tt = svg("title", {}); tt.textContent = o.tip(r); rect.append(tt); s.append(rect);
    if (rows.length <= 16 || i % 2 === 0) { const t = svg("text", { x: x + bw / 2, y: H - 4, fill: "#8a8f98", "font-size": 10, "text-anchor": "middle" }); t.textContent = o.x(r); s.append(t); }
  });
  return h("div", null, s);
}
// A ring showing one share (0..1).
function ring(share, label, color) {
  const r = 26, c = 2 * Math.PI * r, v = Math.max(0, Math.min(1, share || 0));
  const s = svg("svg", { viewBox: "0 0 70 70", class: "ring", role: "img", "aria-label": `${label}: ${(v * 100).toFixed(0)}%` });
  s.append(svg("circle", { cx: 35, cy: 35, r, fill: "none", stroke: "#23272f", "stroke-width": 8 }));
  s.append(svg("circle", { cx: 35, cy: 35, r, fill: "none", stroke: color || "#ffb000", "stroke-width": 8, "stroke-dasharray": `${c * v} ${c}`, transform: "rotate(-90 35 35)" }));
  const t = svg("text", { x: 35, y: 39, fill: "#e8e6e1", "font-size": 13, "text-anchor": "middle" }); t.textContent = share === null || share === undefined ? "—" : (v * 100).toFixed(0) + "%"; s.append(t);
  return h("div", { class: "ringbox" }, s, h("div", { class: "muted" }, label));
}
// Card payment history: 24 months, most recent on the right. 0 = paid on time, 1-9 = months behind, X = no history.
function paymentStrip(hist) {
  if (!hist) return null;
  const cells = String(hist).split("").reverse();
  const col = (ch) => ch === "0" ? "var(--green)" : ch === "X" ? "#2a2e36" : Number(ch) >= 3 ? "var(--red)" : "var(--yellow)";
  const say = (ch, i) => `${cells.length - i} month(s) ago: ${ch === "0" ? "paid on time" : ch === "X" ? "no history" : ch + " payment(s) behind"}`;
  return h("div", { class: "strip", title: "Payment history, last 24 months (right = most recent)" },
    cells.map((ch, i) => h("span", { style: `background:${col(ch)}`, title: say(ch, i) })));
}
// Promises on a timeline: one dot per promise, height = amount, colour = outcome.
function promiseChart(items) {
  if (!items || !items.length) return null;
  const rows = [...items].filter((x) => x.ptp_due_date).sort((a, b) => String(a.ptp_due_date).localeCompare(String(b.ptp_due_date)));
  if (!rows.length) return null;
  const W = 360, H = 90, L = 8;
  const max = Math.max(...rows.map((r) => r.ptp_amount || 0), 1);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark small", role: "img", "aria-label": "Promises over time" });
  s.append(svg("line", { x1: L, x2: W - 8, y1: H - 16, y2: H - 16, stroke: "#23272f" }));
  const colr = { kept: "#3ddc84", broken: "#ff5a52", partially_kept: "#ffd84d", open: "#4fd1e8", cancelled: "#8a8f98" };
  rows.forEach((r, i) => {
    const x = L + 10 + i * (W - L - 30) / Math.max(rows.length - 1, 1), y = H - 16 - (H - 30) * (r.ptp_amount || 0) / max;
    s.append(svg("line", { x1: x, x2: x, y1: H - 16, y2: y, stroke: "#23272f" }));
    const c = svg("circle", { cx: x, cy: y, r: 5, fill: colr[r.ptp_status] || "#8a8f98" });
    const tt = svg("title", {}); tt.textContent = `${fmt.date(r.ptp_due_date)}: ${fmt.money(r.ptp_amount)} · ${fmt.label(r.ptp_status)}`; c.append(tt); s.append(c);
  });
  [0, rows.length - 1].forEach((i) => { const t = svg("text", { x: i ? W - 70 : L, y: H - 3, fill: "#8a8f98", "font-size": 10 }); t.textContent = fmt.date(rows[i].ptp_due_date); s.append(t); });
  return h("div", null, s, h("div", { class: "note" }, h("span", { class: "good" }, "● kept "), h("span", { class: "bad" }, "● broken "),
    h("span", { class: "warn" }, "● part paid "), h("span", { style: "color:var(--cyan)" }, "● still open"), " · higher dot = bigger amount"));
}

// ---------- OVERVIEW ----------
async function viewMon() {
  setView("mon", loading("PORTFOLIO"));
  const p = await api("/portfolio");
  const t = p.tiles;
  const tile = (l, v, d, cls, tip) => h("div", { class: "tile", title: tip || null }, h("div", { class: "l" }, l), h("div", { class: "v " + (cls || "") }, v), d ? h("div", { class: "d" }, d) : null);
  const tiles = h("div", { class: "grid g-tiles" },
    tile("OPEN CASES", fmt.int(t.open_cases), `${fmt.int(t.on_hold)} where contact is on hold (insolvency or deceased)`),
    tile("TOTAL OVERDUE", fmt.big(t.overdue_total), "across open cases"),
    tile("AVERAGE DAYS LATE", fmt.num(t.avg_dpd), "open cases"),
    tile("PROMISES KEPT · SEPTEMBER", fmt.pct(t.kept_rate), `of ${fmt.int(t.resolved)} promises that fell due`, t.kept_rate < 0.4 ? "bad" : "",
      "kept in full ÷ (kept + part paid + broken), promises due 1-28 Sep"),
    tile("CALLS THAT REACHED THE CUSTOMER", fmt.pct(t.rpc_rate), `of ${fmt.int(t.attempts)} calls made in September`, "",
      "Calls where we spoke to the customer themselves (right-party contact)"),
    tile("PROMISES DUE IN NEXT 7 DAYS", fmt.int(t.promises_due_7d), fmt.big(t.amount_due_7d) + " promised"),
    tile("CASES WITH HARDSHIP", fmt.pct(t.hardship_share), `${fmt.int(t.vulnerable)} customers need extra care`),
    tile("REQUESTS WAITING FOR A PERSON", fmt.int(p.pending_approvals), "from the customer app", p.pending_approvals ? "warn" : ""));

  const maxB = Math.max(...p.buckets.map((b) => b.cases));
  const ladder = h("div", { class: "ladder" }, p.buckets.map((b) => h("div", { class: "row" },
    bucket(b.bucket),
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(100 * b.cases / maxB).toFixed(1)}%;background:${RAMP[b.bucket]}` })),
    h("span", { class: "n" }, fmt.int(b.cases)), h("span", { class: "muted n" }, fmt.big(b.overdue)))));

  const queues = h("table", null, h("thead", null, h("tr", null, ["TEAM (QUEUE)", "CASES", "AVG DAYS LATE", "OVERDUE", "WITH HARDSHIP", "PROMISE PENDING"].map((c, i) => h("th", { class: i ? "n" : "" }, c)))),
    h("tbody", null, p.queues.map((q) => h("tr", { class: "click", tabindex: 0, onclick: () => route("WORK " + q.queue), onkeydown: (e) => e.key === "Enter" && route("WORK " + q.queue) },
      h("td", null, fmt.label(q.queue)), h("td", { class: "n" }, fmt.int(q.cases)), h("td", { class: "n" }, fmt.num(q.avg_dpd)),
      h("td", { class: "n" }, fmt.big(q.overdue)), h("td", { class: "n" }, fmt.pct(q.hardship_share, 0)), h("td", { class: "n" }, fmt.int(q.with_open_promise))))));

  const hard = (p.hardship_reasons || []).filter((r) => r.reason !== "other" && r.reason !== "not recorded");
  const other = (p.hardship_reasons || []).filter((r) => r.reason === "other" || r.reason === "not recorded").reduce((a, r) => a + r.cases, 0);
  const sectorMax = (p.sectors || []).slice(0, 12);

  setView("mon", title("PORTFOLIO OVERVIEW", `as of ${fmt.date(p.snapshot_date)} · an open case has no outcome recorded yet · click a row or bar to drill in`), tiles,
    h("div", { class: "grid g-mon", style: "margin-top:10px" },
      panel("OPEN CASES BY HOW LATE THEY ARE", ladder, "cases · overdue"),
      panel("TEAMS", queues, "click a team to open its worklist")),
    h("div", { class: "grid g-2", style: "margin-top:10px" },
      panel("CALLS MADE PER DAY · LAST 30 DAYS", callsChart(p.daily_calls, p.events), "bars: calls · blue line: reached the customer"),
      panel("SHARE OF PROMISES KEPT, BY MONTH DUE", keptChart(p.kept_by_month), "kept in full ÷ promises that fell due")),
    h("div", { class: "grid g-3eq", style: "margin-top:10px" },
      panel("WHY CUSTOMERS ARE IN HARDSHIP", h("div", null, hbars(hard, { label: (r) => plain("hardship", r.reason), value: (r) => r.cases, text: (r) => fmt.int(r.cases),
        color: () => "var(--yellow)", onclick: () => route("WORK HARDSHIP") }),
        other ? h("div", { class: "note" }, `${fmt.int(other)} more cases with hardship give another reason or none.`) : null), "open cases"),
      panel("OPEN CASES BY JOB SECTOR", hbars(sectorMax, { label: (r) => r.sector, value: (r) => r.cases, text: (r) => fmt.int(r.cases),
        title: (r) => `${fmt.pct(r.hardship_share, 0)} of these cases have hardship`, onclick: (r) => route("GROUPS SECTOR") }), "click for Groups"),
      panel("WHAT HAPPENED ON SEPTEMBER CALLS", hbars(p.contact_outcomes || [], { label: (r) => ({ "Right party contact - no promise": "Reached customer, no promise",
          "Promise to pay": "Customer promised to pay", "Left message": "Left a message", "Hardship disclosed": "Customer told us of hardship" })[r.outcome] || r.outcome, value: (r) => r.contacts, text: (r) => fmt.int(r.contacts),
        color: (r) => /promise/i.test(r.outcome) ? "var(--green)" : /refus|wrong/i.test(r.outcome) ? "var(--red)" : "var(--cyan)" }), "outbound calls")),
    h("div", { class: "grid g-2", style: "margin-top:10px" },
      panel("PROMISES FALLING DUE · NEXT 14 DAYS", columns(p.due_by_day || [], { x: (r) => String(r.day).slice(8, 10), y: (r) => r.promises,
        tip: (r) => `${fmt.date(r.day)}: ${fmt.int(r.promises)} promises, ${fmt.big(r.amount)}`, fmtY: (v) => fmt.int(Math.round(v)), color: () => "#4fd1e8", aria: "Promises due per day" }),
        "promises per day (day of month)"),
      panel("OPEN CASES BY PRODUCT", hbars(p.products || [], { label: (r) => plain("product", r.product), value: (r) => r.cases,
        text: (r) => `${fmt.int(r.cases)} · ${fmt.big(r.overdue)}` }), "cases · overdue")));
  ticker(p.events);
}
function callsChart(rows, events) {
  const W = 600, H = 120, pad = 18;
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const bw = (W - pad) / rows.length;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img", "aria-label": "Outbound calls per day" });
  const out = new Set();
  for (const e of events || []) if (e.event_type === "dialer_outage") {
    const a = String(e.start_ts).slice(0, 10), b = String(e.end_ts).slice(0, 10);
    rows.forEach((r) => { if (r.day >= a && r.day <= b) out.add(r.day); });
  }
  let line = "";
  rows.forEach((r, i) => {
    const x = pad + i * bw, bh = (H - 22) * r.calls / max, rh = (H - 22) * r.rpc / max;
    const rect = svg("rect", { x: x + 1, y: H - 14 - bh, width: Math.max(bw - 2, 1), height: bh, fill: out.has(r.day) ? "#ff5a52" : "#ffb000", opacity: out.has(r.day) ? 0.9 : 0.55 });
    const tt = svg("title", {}); tt.textContent = `${r.day}: ${r.calls} calls, ${r.rpc} reached the customer${out.has(r.day) ? " (dialler outage IR-2026-0923)" : ""}`;
    rect.append(tt); s.append(rect);
    line += `${i ? "L" : "M"}${x + bw / 2},${H - 14 - rh}`;
  });
  s.append(svg("path", { d: line, stroke: "#4fd1e8", fill: "none", "stroke-width": 1.5 }));
  [0, Math.floor(rows.length / 2), rows.length - 1].forEach((i) => { if (rows[i]) {
    const t = svg("text", { x: pad + i * bw, y: H - 2, fill: "#8a8f98", "font-size": 10 }); t.textContent = rows[i].day.slice(5); s.append(t); } });
  const wrap = h("div", null, s);
  if (out.size) wrap.append(h("div", { class: "note" }, h("span", { class: "bad" }, "■ "), "red bars: the auto-dialler was down 23–24 Sep (incident IR-2026-0923). Fewer calls those days is a system problem, not customer behaviour."));
  return wrap;
}
function keptChart(rows) {
  const W = 600, H = 120, pad = 26;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img", "aria-label": "Promise-kept rate by month" });
  const vals = rows.map((r) => r.kept_rate);
  const lo = Math.min(...vals) - 0.02, hi = Math.max(...vals) + 0.02;
  const x = (i) => pad + i * (W - pad - 10) / Math.max(rows.length - 1, 1);
  const y = (v) => 8 + (H - 30) * (1 - (v - lo) / (hi - lo || 1));
  s.append(svg("path", { d: rows.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r.kept_rate)}`).join(""), stroke: "#3ddc84", fill: "none", "stroke-width": 2 }));
  rows.forEach((r, i) => {
    const c = svg("circle", { cx: x(i), cy: y(r.kept_rate), r: 3, fill: "#3ddc84" });
    const tt = svg("title", {}); tt.textContent = `${r.month}: ${(r.kept_rate * 100).toFixed(1)}% of ${r.promises} promises`; c.append(tt); s.append(c);
    if (i % 2 === 0 || i === rows.length - 1) { const t = svg("text", { x: x(i) - 14, y: H - 4, fill: "#8a8f98", "font-size": 10 }); t.textContent = r.month.slice(2); s.append(t); }
  });
  [lo + 0.02, hi - 0.02].forEach((v) => { const t = svg("text", { x: 0, y: y(v) + 3, fill: "#8a8f98", "font-size": 10 }); t.textContent = (v * 100).toFixed(0) + "%"; s.append(t); });
  return h("div", null, s);
}
function ticker(events) {
  const tr = $("#ticker");
  tr.replaceChildren(...(events || []).map((e) => h("span", null, h("span", { class: "sev " + (e.severity === "sev3" ? "s3" : "") }, (e.severity || "").toUpperCase()),
    `${e.event_id} · ${e.system} · ${String(e.start_ts).slice(0, 16)} · ${e.description}`)));
}

// ---------- WORKLIST ----------
const FLAGS = { HARDSHIP: "hardship", PROMISE: "promise_due", BROKEN: "broken", DISPUTE: "dispute", VULNERABLE: "vulnerable", HOLD: "hold" };
const FLAG_LABEL = { HARDSHIP: "HARDSHIP", PROMISE: "PROMISE DUE NOW", BROKEN: "2+ BROKEN PROMISES", DISPUTE: "DISPUTE", VULNERABLE: "VULNERABLE", HOLD: "CONTACT ON HOLD" };
// open the worklist for one group (from Groups, Overview or Similar customers); kept until cleared
function openGroupWork(g) { state.workGroup = g; route("WORK"); }
async function viewWork(arg) {
  const parts = (arg || "").split(/\s+/).filter(Boolean);
  let flag = null, queue = null;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p === "AREA" && parts[i + 1]) { state.workGroup = { area: parts.slice(i + 1).join(" "), label: "postal area " + parts.slice(i + 1).join(" ") }; break; }
    if (FLAGS[p]) flag = FLAGS[p]; else queue = p.toLowerCase();
  }
  const g = state.workGroup;
  setView("work", loading("WORKLIST"));
  const qs = new URLSearchParams(); if (flag) qs.set("flag", flag); if (queue) qs.set("queue", queue);
  if (g) { if (g.group_by) { qs.set("group_by", g.group_by); qs.set("group", g.group); } if (g.area) qs.set("area", g.area); }
  const data = await api("/worklist?" + qs);
  const flagKey = Object.keys(FLAGS).find((k) => FLAGS[k] === flag) || "";
  const filters = h("div", { class: "filters" },
    h("select", { "aria-label": "Team", onchange: (e) => route(["WORK", e.target.value.toUpperCase(), flagKey].join(" ")) },
      h("option", { value: "" }, "ALL TEAMS"), data.queues.map((q) => h("option", { value: q, selected: q === queue }, q.replace(/_/g, " ").toUpperCase()))),
    Object.entries(FLAGS).map(([k, v]) => h("button", { class: "btn " + (flag === v ? "on" : ""), onclick: () => route(`WORK ${queue || ""} ${flag === v ? "" : k}`) }, FLAG_LABEL[k])),
    g ? h("button", { class: "btn on", title: "Remove this group filter", onclick: () => { state.workGroup = null; route(`WORK ${queue || ""} ${flagKey}`); } }, `GROUP: ${g.label || g.group} ✕`) : null);
  const rows = data.rows.map((r) => {
    const flags = [];
    if (yes(r.insolvency_hold_flag)) flags.push(h("span", { class: "chip red", title: "Bankruptcy or consumer proposal: no collection contact" }, "INSOLVENCY"));
    if (yes(r.deceased_hold_flag)) flags.push(h("span", { class: "chip red" }, "DECEASED"));
    if (yes(r.cease_contact_flag)) flags.push(h("span", { class: "chip red", title: "Customer asked in writing to stop contact: letters only" }, "STOP CONTACT"));
    if (yes(r.third_party_rep_flag)) flags.push(h("span", { class: "chip red", title: "Contact the representative, not the customer" }, "HAS REPRESENTATIVE"));
    if (yes(r.hardship_flag)) flags.push(h("span", { class: "chip yel", title: plain("hardship", r.hardship_reason) }, yes(r.hardship_program_accepted) ? "HARDSHIP · ON A PLAN" : "HARDSHIP"));
    if (yes(r.vulnerable_customer_flag)) flags.push(h("span", { class: "chip yel", title: "Needs extra care: certified agent, no pressure" }, "VULNERABLE"));
    if (yes(r.dispute_flag)) flags.push(h("span", { class: "chip cyn" }, "DISPUTE"));
    if (r.promises_broken >= 2) flags.push(h("span", { class: "chip" }, `${r.promises_broken} BROKEN PROMISES`));
    const open = () => route(r.case_id);
    return h("tr", { class: "click", tabindex: 0, onclick: open, onkeydown: (e) => e.key === "Enter" && open() },
      h("td", { class: "n" }, fmt.num(r.priority, 0)), h("td", null, h("span", { class: "id" }, r.case_id)),
      h("td", null, `${r.first_name || ""} ${r.last_name || ""}`.trim() || r.crm_id), h("td", null, fmt.label(r.queue)),
      h("td", null, bucket(r.current_bucket)), h("td", { class: "n" }, r.current_dpd), h("td", { class: "n" }, fmt.money(r.total_overdue)),
      h("td", null, r.current_ptp_due_date ? `${fmt.money0(r.current_ptp_amount)} by ${fmt.date(r.current_ptp_due_date)}` : "—"),
      h("td", null, flags), h("td", { class: "muted" }, [r.job_sector, r.postal_area].filter(Boolean).join(" · ") || "—"),
      h("td", { class: "muted" }, fmt.label(r.next_scheduled_action)));
  });
  setView("work", title("WORKLIST", `${data.rows.length} open cases, most urgent first${queue ? " · team " + fmt.label(queue) : ""}${flag ? " · " + FLAG_LABEL[flagKey].toLowerCase() : ""}${g ? " · " + (g.label || g.group) : ""}`), filters,
    panel("CASES", rows.length ? h("table", null, h("thead", null, h("tr", null, ["PRIORITY", "CASE", "CUSTOMER", "TEAM", "HOW LATE", "DAYS LATE", "OVERDUE", "PROMISE", "FLAGS", "JOB SECTOR · AREA", "NEXT STEP"].map((c, i) => h("th", { class: [0, 5, 6].includes(i) ? "n" : "" }, c)))), h("tbody", null, rows)) : h("div", { class: "empty" }, "No open cases match."),
      "priority is higher when: more days late, more owed, a promise due now, promises broken before, hardship with no plan yet"));
}

// ---------- GROUPS (job sector, industry, employer, postal area, city, province) ----------
const GROUP_KEYS = ["sector", "industry", "employer", "area", "city", "province"];
async function viewGroups(arg) {
  const parts = (arg || "").trim().split(/\s+/).filter(Boolean);
  let by = (parts[0] || "SECTOR").toLowerCase(), area = null;
  if (!GROUP_KEYS.includes(by)) by = "sector";
  if (by === "area" && parts.length > 1) { area = parts.slice(1).join(" "); by = "sector"; }   // AREA <postal code>: inside one area, by job sector
  if (!area && state.groupArea && parts.length <= 1 && by !== "area") area = state.groupArea;
  state.groupArea = area;
  setView("groups", loading("GROUPS"));
  const qs = new URLSearchParams({ by, limit: 30 }); if (area) qs.set("area", area);
  const d = await api("/groups?" + qs);
  const t = d.total || {};
  const label = d.options[by];
  const toWork = (g) => openGroupWork({ group_by: by, group: g.grp, area: d.area, label: `${label}: ${g.grp}${d.area ? " in " + d.area : ""}` });

  const input = h("input", { placeholder: "Postal code or area, e.g. M5V 2T6", value: d.area || "", style: "width:240px", "aria-label": "Postal code" });
  const go = () => { const v = input.value.trim(); state.groupArea = v || null; route(v ? "AREA " + v : "GROUPS " + by.toUpperCase()); };
  input.addEventListener("keydown", (e) => e.key === "Enter" && go());
  const tabs = h("div", { class: "filters" },
    h("span", { class: "muted" }, "GROUP BY "),
    GROUP_KEYS.filter((k) => !(d.area && k === "area")).map((k) => h("button", { class: "btn " + (k === by ? "on" : ""), onclick: () => route("GROUPS " + k.toUpperCase()) }, d.options[k].toUpperCase())),
    h("span", { class: "muted", style: "margin-left:12px" }, "POSTAL AREA "), input, h("button", { class: "btn", onclick: go }, "FIND"),
    d.area ? h("button", { class: "btn on", onclick: () => { state.groupArea = null; route("GROUPS " + by.toUpperCase()); } }, `AREA ${d.area} ✕`) : null,
    d.area && t.cases ? h("button", { class: "btn approve", onclick: () => openGroupWork({ area: d.area, label: "postal area " + d.area }) }, `OPEN ALL ${fmt.int(t.cases)} CASES IN ${d.area}`) : null);

  const alerts = !d.area && (d.layoff_alerts || []).length ? h("div", { class: "banner hard" }, h("b", null, "POSSIBLE LAYOFFS"),
    "Several customers at these employers fell behind in the last 4 months because they lost work income. Consider reaching out with hardship options early.",
    h("ul", null, d.layoff_alerts.map((a) => h("li", null, h("span", { class: "id", onclick: () => openGroupWork({ group_by: "employer", group: a.employer, label: "employer " + a.employer }) }, a.employer),
      ` — ${a.lost_income_recent} lost work income recently · ${a.open_cases} open cases · ${a.job_sector || "sector not recorded"}`)))) : null;

  if (!d.groups.length) {
    setView("groups", title("GROUPS", d.area ? `postal area ${d.area}` : label), tabs, h("div", { class: "empty" }, d.area ? `No open cases in postal area ${d.area}.` : "No data."));
    return;
  }
  const summary = h("div", { class: "grid g-tiles" },
    h("div", { class: "tile" }, h("div", { class: "l" }, "OPEN CASES"), h("div", { class: "v" }, fmt.int(t.cases)), h("div", { class: "d" }, d.area ? "in postal area " + d.area : "all groups")),
    h("div", { class: "tile" }, h("div", { class: "l" }, "TOTAL OVERDUE"), h("div", { class: "v" }, fmt.big(t.overdue))),
    h("div", { class: "tile" }, h("div", { class: "l" }, "AVERAGE DAYS LATE"), h("div", { class: "v" }, fmt.num(t.avg_days_late))),
    h("div", { class: "tile rings" }, ring(t.hardship_share, "have hardship", "#ffd84d"), ring(t.lost_income_share, "lost work income", "#ff5a52")));

  const sortedLoss = [...d.groups].filter((g) => g.cases >= (by === "employer" ? 3 : 20)).sort((a, b) => b.lost_income_share - a.lost_income_share).slice(0, 12);
  const table = h("table", null, h("thead", null, h("tr", null, [label.toUpperCase(), "CUSTOMERS", "OPEN CASES", "OVERDUE", "AVG DAYS LATE", "WITH HARDSHIP", "LOST WORK INCOME", "BROKE A PROMISE", "PROMISES KEPT", "NEW IN 60 DAYS"].map((c, i) => h("th", { class: i ? "n" : "" }, c)))),
    h("tbody", null, d.groups.map((g) => h("tr", { class: "click", tabindex: 0, onclick: () => toWork(g), onkeydown: (e) => e.key === "Enter" && toWork(g) },
      h("td", null, g.grp), h("td", { class: "n" }, fmt.int(g.customers)), h("td", { class: "n" }, fmt.int(g.cases)), h("td", { class: "n" }, fmt.big(g.overdue)),
      h("td", { class: "n" }, fmt.num(g.avg_days_late)), h("td", { class: "n" }, fmt.pct(g.hardship_share, 0)),
      h("td", { class: "n " + (g.lost_income_share > 1.5 * (t.lost_income_share || 1) ? "bad" : "") }, fmt.pct(g.lost_income_share, 1)),
      h("td", { class: "n" }, fmt.pct(g.broken_promise_share, 0)), h("td", { class: "n" }, fmt.pct(g.kept_rate, 0)), h("td", { class: "n" }, fmt.int(g.opened_60d))))));

  setView("groups", title("GROUPS", `open cases by ${label.toLowerCase()}${d.area ? " in postal area " + d.area : ""} · click a group to open its cases`), tabs, alerts, summary,
    h("div", { class: "grid g-2", style: "margin-top:10px" },
      panel(`OPEN CASES BY ${label.toUpperCase()}`, hbars(d.groups.slice(0, 15), { label: (g) => g.grp, value: (g) => g.cases, text: (g) => `${fmt.int(g.cases)} · ${fmt.big(g.overdue)}`,
        color: (g) => g.hardship_share > (t.hardship_share || 0) * 1.1 ? "var(--yellow)" : "var(--amber)",
        title: (g) => `${fmt.pct(g.hardship_share, 0)} with hardship, average ${fmt.num(g.avg_days_late)} days late`, onclick: toWork }),
        "yellow = more hardship than average"),
      panel("WHERE WORK INCOME IS BEING LOST", hbars(sortedLoss, { label: (g) => g.grp, value: (g) => g.lost_income_share, text: (g) => fmt.pct(g.lost_income_share, 1),
        color: () => "var(--red)", title: (g) => "share of open cases where the customer lost a job, hours or pay, is waiting for EI, or is on strike", onclick: toWork }),
        `share of cases · all groups: ${fmt.pct(t.lost_income_share, 1)}`)),
    h("div", { style: "margin-top:10px" }, panel("ALL GROUPS", table, `${d.groups.length} largest`)),
    h("p", { class: "note" }, "Groups help plan outreach, for example offering hardship help early where an employer has cut jobs. They are never used to decide what an individual customer is offered, and they contain no protected attributes (gender, age, origin …)."));
}

// ---------- CUST ----------
async function viewCust(key) {
  setView("cust", loading(key));
  const doc = await api("/customers/" + encodeURIComponent(key));
  state.lastCustomer = key;
  const cases = doc.collections.cases || [];
  const caseId = /^CS-/.test(key) ? key : (cases.find((c) => c.outcome === null) || cases[0] || {}).case_id;
  const kase = cases.find((c) => c.case_id === caseId) || {};
  const rec = caseId ? await api(`/cases/${caseId}/nba`) : null;
  const p = doc.profile || {}, sc = doc.servicing_constraints || {}, ex = doc.exposure || {}, ct = doc.contact || {}, wk = doc.work || {};

  // always-visible constraints and hardship (fair treatment: never behind a click)
  const blocking = [];
  if (yes(sc.bankruptcy) || yes(sc.consumer_proposal) || yes(kase.insolvency_hold_flag)) blocking.push("Bankruptcy or consumer proposal on file: do not contact about the debt (POL-COLL-001 §7.3)");
  if (yes(sc.deceased) || yes(kase.deceased_hold_flag)) blocking.push("Customer has died: deal with the estate only");
  if (yes(sc.cease_communication) || yes(kase.cease_contact_flag)) blocking.push("Customer asked us in writing to stop contacting them: letters and statements only (POL-COLL-001 §5.4)");
  if (yes(sc.credit_counselling_agency) || yes(sc.legal_representative) || yes(kase.third_party_rep_flag)) blocking.push("A credit counsellor, lawyer or trustee acts for the customer: contact them, not the customer (POL-COLL-001 §5.4)");
  if (yes(sc.power_of_attorney)) blocking.push("Power of attorney on file");
  const ch = (k, label) => yes(sc[k]) ? h("span", { class: "chip grn" }, label + " ALLOWED") : h("span", { class: "chip red" }, "NO " + label);
  const banners = [
    blocking.length ? h("div", { class: "banner" }, h("b", null, "DO NOT CONTACT AS USUAL"), h("ul", null, blocking.map((b) => h("li", null, b)))) : null,
    (doc.hardship_indicators || {}).financial_hardship_flag ? h("div", { class: "banner hard" }, h("b", null, "HARDSHIP: OFFER HELP BEFORE ASKING FOR PAYMENT"),
      h("ul", null, doc.hardship_indicators.reasons.map((r) => h("li", null, r)))) : null,
    h("div", { style: "margin-bottom:10px" }, h("span", { class: "muted" }, "HOW WE MAY CONTACT "), ch("consent_call", "CALLS"), ch("consent_sms", "TEXTS"), ch("consent_email", "EMAILS"),
      yes(sc.internal_do_not_call) ? h("span", { class: "chip red" }, "ON OUR DO-NOT-CALL LIST") : null,
      yes(sc.national_dncl_telemarketing_only) ? h("span", { class: "chip", title: "The national list covers sales calls only; collection calls are allowed (POL-COLL-001 §5.3)" }, "NATIONAL DO-NOT-CALL LIST (SALES ONLY)") : null,
      yes(kase.vulnerable_customer_flag) ? h("span", { class: "chip yel", title: "Certified agent, vulnerable-customer script, extra time, no pressure" }, "NEEDS EXTRA CARE") : null,
      ct.preferred_language ? h("span", { class: "chip" }, (String(ct.preferred_language).toUpperCase().startsWith("FR") ? "SPEAKS FRENCH" : "SPEAKS ENGLISH")) : null,
      h("span", { class: "chip" }, `CALL HOURS ${((ct.permitted_call_window_local || [])[0]) || "07:00"}–${((ct.permitted_call_window_local || [])[1]) || "21:00"} ${ct.time_zone ? "(" + ct.time_zone.split("/").pop().replace("_", " ") + " time)" : ""}`)),
  ];

  if ((doc._overlay || []).length) banners.unshift(h("div", { class: "banner hard" }, h("b", null, "FROM A CONVERSATION LOGGED IN THIS APP"),
    h("ul", null, doc._overlay.map((e) => h("li", null, overlayText(e))))));
  const caseSel = cases.length > 1 ? h("select", { "aria-label": "Case", onchange: (e) => route(e.target.value) },
    cases.map((c) => h("option", { value: c.case_id, selected: c.case_id === caseId }, `${c.case_id} ${c.outcome ? "(" + c.outcome + ")" : "(open)"}`))) : null;

  // left column: exposure, accounts, scores
  const accts = [...(doc.accounts.cards || []).map((a) => ["card", a]), ...(doc.accounts.loans || []).map((a) => ["loan", a]), ...(doc.accounts.deposits || []).map((a) => ["deposit", a])];
  const left = h("div", { class: "grid" },
    panel("WHAT THEY OWE", h("div", { class: "kv" },
      h("span", { class: "k" }, "Overdue now"), h("span", { class: "v bad" }, fmt.money(ex.past_due_total)),
      h("span", { class: "k" }, "Card balance"), h("span", { class: "v" }, fmt.money(ex.card_balance_total)),
      h("span", { class: "k" }, "Loans still to repay"), h("span", { class: "v" }, fmt.money(ex.loan_outstanding_total)),
      h("span", { class: "k" }, "Most days late (any account)"), h("span", { class: "v" }, ex.max_dpd ?? "—"),
      h("span", { class: "k" }, "Worst days-late band"), h("span", { class: "v" }, bucket(ex.worst_bucket)),
      h("span", { class: "k" }, "Customer type · with us for"), h("span", { class: "v" }, `${fmt.label(p.segment)} · ${p.tenure_months != null ? Math.floor(p.tenure_months / 12) + " yr " + (p.tenure_months % 12) + " mo" : "—"}`))),
    panel("WORK AND AREA", h("div", { class: "kv" },
      h("span", { class: "k" }, "Job sector"), h("span", { class: "v" }, wk.sector || fmt.label(wk.employment_status)),
      h("span", { class: "k" }, "Industry"), h("span", { class: "v" }, wk.industry || "—"),
      h("span", { class: "k" }, "Employer"), h("span", { class: "v" }, wk.employer || "—"),
      h("span", { class: "k" }, "Employment"), h("span", { class: "v" }, fmt.label(wk.employment_status)),
      h("span", { class: "k" }, "Postal area · city"), h("span", { class: "v" }, ct.postal_area ? h("span", { class: "id", onclick: () => route("AREA " + ct.postal_area) }, ct.postal_area) : "—", ` · ${ct.city || "—"}`)),
      "for finding similar customers only"),
    panel("ACCOUNTS", accts.length ? h("table", null, h("tbody", null, accts.map(([k, a]) => h("tr", null,
      h("td", null, h("span", { class: "id" }, a.account_id), h("div", { class: "muted" }, a.product_code || a.product_type || k)),
      h("td", { class: "n" }, fmt.money(a.past_due_amount ?? a.overdraft_used), h("div", { class: "muted" }, `${a.dpd ?? a.overdraft_dpd ?? "—"} days late`)),
      h("td", null, bucket(a.dpd_bucket || a.overdraft_dpd_bucket), yes(a.hardship_program_flag) ? h("div", { class: "warn" }, "on " + fmt.label(a.hardship_program_type)) : null,
        k === "card" ? paymentStrip(a.payment_history_24m) : null))))) : h("div", { class: "muted" }, "No linked accounts."), "squares: 24 months, green = on time"),
    panel("BANK MODEL SCORES", h("table", null, h("tbody", null, (doc.model_scores || []).map((m) => h("tr", { title: [m.top_reason_1_text, m.top_reason_2_text].filter(Boolean).join(" · ") },
      h("td", null, fmt.label(m.model_name)), h("td", { class: "n" }, fmt.num(m.score, 2)), h("td", { class: "muted" }, m.score_band || m.slot_label || ""))))),
      "hover a row for reasons"));

  // middle: NBA + approval
  const peersBox = h("div", null, loading("SIMILAR CUSTOMERS"));
  const middle = h("div", { class: "grid" }, rec ? nbaPanel(rec, doc) : panel("NEXT BEST ACTION", h("div", { class: "muted" }, "No case.")),
    panel("SIMILAR CUSTOMERS", peersBox, "other customers' names are not shown"),
    panel("DECISIONS ON THIS CASE", decisionHistory(rec)));
  if (caseId) api(`/cases/${caseId}/peers`).then((d) => peersBox.replaceChildren(peersEl(d))).catch((e) => peersBox.replaceChildren(errorBox(e)));
  else peersBox.replaceChildren(h("div", { class: "muted" }, "No case."));

  // right: case, promises, contacts, notes, calls
  const pr = kase.promises || {};
  const right = h("div", { class: "grid" },
    panel("CASE " + (kase.case_id || ""), h("div", { class: "kv" },
      h("span", { class: "k" }, "Status"), h("span", { class: "v" }, kase.outcome ? fmt.label(kase.outcome) : "open"),
      h("span", { class: "k" }, "Opened · why"), h("span", { class: "v" }, `${fmt.date(kase.case_open_date)} · ${fmt.label(kase.case_open_reason)}`),
      h("span", { class: "k" }, "Team · current approach"), h("span", { class: "v" }, `${fmt.label(kase.queue)} · ${fmt.label(kase.current_treatment)}`),
      h("span", { class: "k" }, "Days late · band"), h("span", { class: "v" }, kase.current_dpd ?? "—", " ", bucket(kase.current_bucket)),
      h("span", { class: "k" }, "Overdue · total owed"), h("span", { class: "v" }, `${fmt.money(kase.total_overdue)} · ${fmt.money(kase.total_exposure)}`),
      h("span", { class: "k" }, "Hardship"), h("span", { class: "v" }, yes(kase.hardship_flag) ? `${plain("hardship", kase.hardship_reason)}${yes(kase.hardship_program_accepted) ? " · on a plan" : ""}` : "none"),
      h("span", { class: "k" }, "Promises"), h("span", { class: "v" }, `${pr.kept ?? 0} kept · ${pr.broken ?? 0} broken · ${pr.open ?? 0} open`),
      h("span", { class: "k" }, "Agent"), h("span", { class: "v" }, `${kase.assigned_agent_id || "—"}${kase.assigned_agent ? " · " + kase.assigned_agent.site : ""}`)),
      (kase.offers || []).length ? h("div", { style: "margin-top:6px" }, h("div", { class: "muted" }, "Hardship offers made:"), kase.offers.slice(0, 3).map((o) => h("div", { class: "muted" },
        `${o.program_id}: ${fmt.money(o.proposed_payment_amount)} a month for ${o.duration_months ?? "?"} months · customer ${o.customer_response ? fmt.label(o.customer_response) : "has not answered"} · plan ${o.plan_status ? fmt.label(o.plan_status) : "not started"}`))) : null),
    panel("PROMISES TO PAY", (pr.items || []).length ? h("div", null, promiseChart(pr.items), h("table", null, h("tbody", null, pr.items.slice(0, 6).map((x) => h("tr", null,
      h("td", null, "due " + fmt.date(x.ptp_due_date)), h("td", { class: "n" }, fmt.money(x.ptp_amount)),
      h("td", { class: x.ptp_status === "kept" ? "good" : x.ptp_status === "broken" ? "bad" : "warn" }, x.ptp_status === "partially_kept" ? "part paid" : x.ptp_status === "open" ? "not due yet / open" : fmt.label(x.ptp_status))))))) : h("div", { class: "muted" }, "None.")),
    panel("CALLS", (doc.collections.call_transcripts || []).length ? h("table", null, h("tbody", null, doc.collections.call_transcripts.map((c) => h("tr", { class: "click", tabindex: 0, onclick: () => route("CALL " + c.transcript_id), onkeydown: (e) => e.key === "Enter" && route("CALL " + c.transcript_id) },
      h("td", null, h("span", { class: "id" }, c.transcript_id)), h("td", null, fmt.ts(c.call_start_ts)), h("td", { class: "n" }, `${c.duration_sec}s`), h("td", { class: "muted" }, c.direction))))) : h("div", { class: "muted" }, "No recorded calls."), "click to review a call"),
    panel("RECENT CONTACTS", h("table", null, h("tbody", null, (doc.collections.recent_contacts || []).slice(0, 8).map((c) => h("tr", null,
      h("td", { class: "muted" }, fmt.ts(c.contact_ts_local)), h("td", null, `${c.direction === "inbound" ? "from customer" : "to customer"} · ${c.channel}`), h("td", null, c.outcome_desc || c.outcome_code || ""),
      h("td", null, yes(c.rpc_flag) ? h("span", { class: "chip grn", title: "We spoke to the customer themselves" }, "REACHED") : null, yes(c.within_permitted_hours_flag) === false && c.within_permitted_hours_flag !== null ? h("span", { class: "chip red" }, "OUT OF HOURS") : null)))))),
    panel("NOTES", notesList(doc.collections.recent_notes || [])),
    (doc.interactions || []).length ? panel("ACTIONS TAKEN IN THIS APP", h("table", null, h("tbody", null, doc.interactions.slice(0, 8).map((i) => h("tr", null,
      h("td", { class: "muted" }, fmt.ts(i.ts)), h("td", null, fmt.label(i.type)), h("td", { class: i.status === "rejected" ? "bad" : i.status === "pending" ? "warn" : "good" }, i.status), h("td", { class: "muted" }, i.decided_by || i.actor)))))) : null);

  const hasDraft = !!(state.convDrafts && state.convDrafts[caseId]);
  const convBox = h("div", null, caseId ? loading("CONVERSATIONS") : h("div", { class: "muted" }, "No case."));
  right.prepend(...[
    panel("LOG A CONVERSATION", h("div", null, h("div", { class: "muted" }, "Just spoke to the customer? Paste the transcript or your notes."),
      h("div", { class: "bar" }, h("button", { class: "btn approve", disabled: caseId ? null : true, onclick: () => route("LOG " + caseId) }, !caseId ? "No case selected" : hasDraft ? "RESUME DRAFT" : "+ LOG CONVERSATION"),
        hasDraft ? h("span", { class: "chip yel" }, "UNSAVED DRAFT") : null))),
    panel("CONVERSATIONS LOGGED HERE", convBox, "click to open"),
    (pr.logged || []).length ? panel("PROMISES LOGGED HERE", h("table", null, h("tbody", null, pr.logged.map((x) => h("tr", null, h("td", null, "due " + fmt.date(x.date)), h("td", { class: "n" }, fmt.money(x.amount)),
      h("td", null, h("span", { class: "chip cyn" }, "LOGGED HERE")), h("td", { class: "muted" }, x.logged_by || ""))))), "from conversations, not yet in the bank's records") : null].filter(Boolean));
  if (caseId) api(`/cases/${caseId}/conversations`).then((c) => convBox.replaceChildren(c.items.length ? h("table", null, h("tbody", null, c.items.map((it) => {
    const cp = it.payload, open = () => route("CONV " + cp.conversation_id);
    return h("tr", { class: "click", tabindex: 0, onclick: open, onkeydown: (e) => e.key === "Enter" && open() },
      h("td", { class: "muted" }, fmt.date(cp.started_at)), h("td", null, fmt.label(cp.channel)), h("td", null, cp.summary,
        h("div", null, (c.followups[cp.conversation_id] || []).map((x) => h("span", { class: "chip " + (x.status === "rejected" ? "" : x.status === "pending" ? "yel" : "grn") }, `${CONV_TYPE[x.type] || fmt.label(x.type)}${x.status === "pending" ? " · PENDING" : x.status === "rejected" ? " · REJECTED" : ""}`))))); })))
    : h("div", { class: "muted" }, "None yet."))).catch((e) => convBox.replaceChildren(errorBox(e)));

  setView("cust", title(`${p.first_name || ""} ${p.last_name || ""}`.trim() || doc._id, `${doc._id} · CRM ${doc.master_crm_id} · ${ct.city || ""} ${ct.province_code || ""}${ct.postal_area ? " · area " + ct.postal_area : ""}${wk.sector ? " · " + wk.sector : ""}`), caseSel, ...banners,
    h("div", { class: "grid g-3" }, left, middle, right),
    doc.identity && doc.identity.note ? h("p", { class: "note" }, doc.identity.note) : null);
}
// Similar customers: same employer, same job sector in the same postal area, same postal area.
function peersEl(d) {
  const groups = (d.groups || []).filter((g) => g.open_cases);
  if (!groups.length) return h("div", { class: "muted" }, "No other open cases at the same employer or in the same postal area.");
  return h("div", null,
    d.signal ? h("div", { class: "banner hard", style: "margin-bottom:6px" }, h("b", null, "POSSIBLE LAYOFFS"), d.signal) : null,
    groups.map((g) => h("div", { class: "peer" },
      h("div", { class: "peer-h" }, h("span", { class: "id", onclick: () => openGroupWork({ group_by: g.group_by, group: g.group, area: g.area, label: g.title }) }, g.title)),
      h("div", { class: "peer-stats" },
        h("span", null, h("b", null, fmt.int(g.open_cases)), " other open cases"),
        h("span", null, h("b", null, fmt.int(g.hardship)), " with hardship"),
        h("span", { class: g.lost_income ? "bad" : "" }, h("b", null, fmt.int(g.lost_income)), " lost work income"),
        h("span", null, h("b", null, fmt.int(g.opened_60d)), " new in 60 days"),
        h("span", null, "avg ", h("b", null, fmt.num(g.avg_days_late, 0)), " days late")),
      hbars([{ k: "with hardship", v: g.hardship }, { k: "lost work income", v: g.lost_income }, { k: "new in 60 days", v: g.opened_60d }],
        { label: (r) => r.k, value: (r) => r.v, text: (r) => `${fmt.pct(g.open_cases ? r.v / g.open_cases : 0, 0)}`, color: (r) => r.k === "lost work income" ? "var(--red)" : r.k === "with hardship" ? "var(--yellow)" : "var(--cyan)" }),
      h("div", { class: "muted", style: "margin-top:2px" }, "e.g. ", (g.cases || []).slice(0, 4).map((c) =>
        h("span", { class: "id", style: "margin-right:8px", title: `${c.days_late} days late · ${fmt.money(c.overdue)}${c.hardship_reason ? " · " + plain("hardship", c.hardship_reason) : ""}`, onclick: () => route(c.case_id) }, c.case_id))))));
}

function notesList(notes) {
  if (!notes.length) return h("div", { class: "muted" }, "None.");
  return h("div", { class: "notes" }, notes.slice(0, 5).map((n) => {
    const full = String(n.note_text || ""); const short = full.length > 160 ? full.slice(0, 160) + "…" : full;
    const p = h("p", null, h("span", { class: "muted" }, `${fmt.ts(n.note_ts)} ${fmt.label(n.note_type)} · `), h("span", { class: "txt" }, short));
    if (full.length > 160) p.append(" ", h("button", { class: "more", onclick: (e) => { p.querySelector(".txt").textContent = full; e.target.remove(); } }, "show more"));
    return p;
  }));
}
function decisionHistory(rec) {
  const hist = (rec && rec.history) || [];
  if (!hist.length) return h("div", { class: "muted" }, "No decisions yet.");
  return h("table", null, h("tbody", null, hist.map((d) => h("tr", null, h("td", { class: "muted" }, fmt.ts(d.decided_ts || d.ts)),
    h("td", { class: d.status === "rejected" ? "bad" : "good" }, d.status.toUpperCase()), h("td", null, d.decided_by),
    h("td", null, ((d.payload || {}).final || (d.payload || {}).recommendation || {}).action ? fmt.label(((d.payload.final || d.payload.recommendation).action.code)) : ""),
    h("td", { class: "muted" }, d.decision_reason || "")))));
}

const ROLE_LEVEL = { agent: 0, hardship_specialist: 1, team_lead: 2 };
// Promise risk model (advice only): band, probability and the factors behind it.
function riskEl(r) {
  if (!r) return null;
  const cls = { high: "red", medium: "yel", low: "grn" }[r.band] || "";
  const item = (x, sign) => h("li", null, `${sign} ${x.text}`);
  return h("div", { style: "margin-top:8px" },
    h("span", { class: "muted" }, "WILL THE CURRENT PROMISE BE KEPT? "),
    h("span", { class: "chip " + cls, title: `model ${r.model} · features ${r.definitions} · advice only, a person decides` },
      `${Math.round(r.probability_not_kept * 100)}% CHANCE IT IS NOT KEPT IN FULL · ${r.band.toUpperCase()} RISK`),
    h("ul", { class: "reasons" }, (r.raises_risk || []).map((x) => item(x, "▲")).concat((r.lowers_risk || []).map((x) => item(x, "▼")))),
    h("div", { class: "note" }, "▲ makes a missed payment more likely · ▼ makes it less likely"));
}

function nbaPanel(rec, doc) {
  const box = h("div", { class: "nba" });
  const conflictsEl = h("div");
  const msg = h("div");
  const t = rec.timing;
  const needs = rec.approver_level || "agent";
  const canApprove = () => (ROLE_LEVEL[state.role] ?? 0) >= (ROLE_LEVEL[needs] ?? 0);

  const showConflicts = (cs) => conflictsEl.replaceChildren(cs.length ? h("div", null, h("div", { class: "bad", style: "margin-top:6px" }, "PROBLEMS — fix these before you can approve"),
    h("ul", { class: "reasons conflicts" }, cs.map((c) => h("li", null, c.text, c.cite ? h("span", { class: "cite" }, c.cite) : null)))) : "");
  showConflicts(rec.conflicts || []);

  const approveBtn = h("button", { class: "btn approve", disabled: (rec.conflicts || []).length > 0 || !canApprove() || rec.action.code === "NO_ACTION" ? true : null,
    title: !canApprove() ? `Only a ${plain("role", needs).toLowerCase()} can approve this` : "", onclick: () => submit("approve") }, "APPROVE");
  const editForm = h("div", { hidden: true });
  const rejectForm = h("div", { hidden: true });

  async function submit(decision, extra = {}) {
    msg.replaceChildren();
    try {
      const out = await api(`/cases/${rec.case_id}/decision`, { method: "POST", body: { decision, ...extra } });
      msg.replaceChildren(h("div", { class: "ok" }, `${out.status.toUpperCase()} by ${out.decided_by} at ${fmt.ts(out.decided_ts)}. Saved on the customer's file.`));
      approveBtn.disabled = true;
      setTimeout(() => route(rec.case_id, true), 900);
    } catch (e) {
      if (e.data && e.data.conflicts) showConflicts(e.data.conflicts);
      msg.replaceChildren(errorBox(e));
    }
  }

  // edit form
  const f = {
    channel: h("select", null, ["call", "sms", "email", "letter"].map((c) => h("option", { value: c, selected: c === rec.channel }, c.toUpperCase()))),
    date: h("input", { type: "date", value: t ? t.date : "" }),
    window: h("input", { value: t ? t.window_local : "", placeholder: "e.g. 17:00-19:00" }),
    amount: h("input", { type: "number", step: "0.01", value: rec.amount ?? "" }),
    reason: h("textarea", { placeholder: "Why are you changing it? (required)" }),
  };
  const editsBody = () => ({ channel: f.channel.value, date: f.date.value || null, window_local: f.window.value || null,
    amount: f.amount.value === "" ? null : Number(f.amount.value) });
  const recheck = async () => {
    try { const r = await api(`/cases/${rec.case_id}/check`, { method: "POST", body: { decision: "edit", edits: editsBody() } }); showConflicts(r.conflicts); saveEdit.disabled = r.conflicts.length > 0; }
    catch (e) { msg.replaceChildren(errorBox(e)); }
  };
  Object.values(f).forEach((el) => el.addEventListener("change", recheck));
  const saveEdit = h("button", { class: "btn approve", onclick: () => {
    if (!f.reason.value.trim()) { msg.replaceChildren(h("div", { class: "err" }, "Give a reason for the edit.")); return; }
    submit("edit", { edits: editsBody(), reason: f.reason.value.trim() }); } }, "SAVE EDIT AND APPROVE");
  editForm.append(h("div", { class: "form" }, h("label", null, "How to contact"), f.channel, h("label", null, "Date"), f.date,
    h("label", null, "Time (customer's local time)"), f.window, h("label", null, "Amount"), f.amount, h("label", { class: "full" }, "Why are you changing it?"), h("div", { class: "full" }, f.reason),
    h("div", { class: "full" }, saveEdit)));

  // reject form
  const rr = h("select", null, ["", "customer already paid", "customer on hardship plan", "wrong channel for this customer", "not appropriate now", "other"].map((r) => h("option", { value: r }, r || "Choose a reason…")));
  const rt = h("textarea", { placeholder: "Details (optional)" });
  rejectForm.append(h("div", { class: "form" }, h("label", null, "Reason"), rr, h("label", null, "Details"), rt,
    h("div", { class: "full" }, h("button", { class: "btn reject", onclick: () => {
      if (!rr.value) { msg.replaceChildren(h("div", { class: "err" }, "Choose a reason to reject.")); return; }
      submit("reject", { reason: [rr.value, rt.value.trim()].filter(Boolean).join(": "), reason_code: rr.value }); } }, "REJECT WITH REASON"))));

  box.append(...[
    h("div", { class: "muted" }, "SUGGESTED NEXT STEP"),
    h("div", { class: "action", title: "rule " + rec.action.code }, rec.action.label),
    h("div", { class: "kv" },
      h("span", { class: "k" }, "How"), h("span", { class: "v" }, rec.channel ? { call: "Phone call", sms: "Text message", email: "Email", letter: "Letter" }[rec.channel] || rec.channel : "—"),
      h("span", { class: "k" }, "When"), h("span", { class: "v" }, t ? `${fmt.date(t.date)}, ${t.window_local} (${(t.time_zone || "").split("/").pop().replace("_", " ")} time)` : "—"),
      h("span", { class: "k" }, "Amount to ask for"), h("span", { class: "v" }, rec.amount != null ? fmt.money(rec.amount) : "—"),
      h("span", { class: "k" }, "Hardship option"), h("span", { class: "v" }, rec.program ? `${rec.program_name || rec.program} (${rec.program})` : "—"),
      h("span", { class: "k" }, "Call script"), h("span", { class: "v" }, scriptLabel(rec.script)),
      h("span", { class: "k" }, "Send to"), h("span", { class: "v" }, rec.assign_to ? plain("assign", rec.assign_to) : "keep with the assigned agent"),
      h("span", { class: "k" }, "Who can approve"), h("span", { class: "v " + (canApprove() ? "" : "warn") }, plain("role", needs) + (canApprove() ? "" : " (not your role)"))),
    riskEl(rec.promise_risk),
    (doc._overlay || []).some((e) => e.case_id === rec.case_id) ? h("div", { class: "note" }, "Includes what was logged from a conversation in this app.") : null,
    h("div", { class: "muted", style: "margin-top:8px" }, "WHY THIS STEP"),
    h("ul", { class: "reasons" }, rec.reasons.filter((r) => !(rec.promise_risk && /^Promise risk model/.test(r.text)))   // shown above as the risk line
      .map((r) => h("li", null, r.text, r.cite ? h("span", { class: "cite" }, r.cite) : null))),
    rec.safeguards.length ? h("div", { class: "muted", style: "margin-top:6px" }, "TAKE CARE") : null,
    rec.safeguards.length ? h("ul", { class: "reasons safeguards" }, rec.safeguards.map((r) => h("li", null, r.text, r.cite ? h("span", { class: "cite" }, r.cite) : null))) : null,
    rec.blocked_channels.length ? h("div", { class: "muted" }, "Not allowed right now (hover for why): ", rec.blocked_channels.map((b) => h("span", { class: "chip red", title: `${b.reason} ${b.cite}` }, b.channel.toUpperCase()))) : null,
    t && t.notes && t.notes.length ? h("div", { class: "note" }, t.notes.join(" ")) : null,
    conflictsEl,
    h("div", { class: "bar" },
      h("button", { class: "btn", onclick: () => { editForm.hidden = !editForm.hidden; rejectForm.hidden = true; } }, "CHANGE IT"),
      h("button", { class: "btn reject", onclick: () => { rejectForm.hidden = !rejectForm.hidden; editForm.hidden = true; } }, "REJECT"),
      approveBtn,
      h("span", { class: "note", title: `rules ${rec.engine}` }, "Nothing is sent until a person approves.")),
    editForm, rejectForm, msg].filter((n) => n !== null && n !== undefined && n !== false));
  return panel("NEXT BEST ACTION · CASE " + rec.case_id, box);
}

// ---------- ASK (Layer 2: plain-English questions, answered by Gemini with the SQL shown) ----------
async function api2(path, opts = {}) {
  const res = await fetch("/api/v1" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-C360-User": state.user, "X-C360-Role": state.role },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) {
    const err = new Error((data && (data.error || data.detail)) || `Request failed (${res.status})`);
    err.status = res.status; err.code = data && data.code; err.rid = data && data.request_id;
    err.retry = res.headers.get("Retry-After");
    throw err;
  }
  return data;
}
const ASK_STATUS = { answered: ["ANSWERED", "grn"], refused: ["REFUSED", "red"], partial: ["PARTLY ANSWERED", "yel"] };
const SOURCE_KIND = { governed_metric: "Official metric", sql: "Database query", policy: "Policy documents", notes: "Agent notes",
  transcripts: "Call transcripts", customer: "Customer record" };
function askErrorText(e) {
  if (e.status === 429) return `Too many questions right now. Try again in ${e.retry || "a few"} seconds. (${e.message})`;
  if (e.status === 502) return "The AI model is busy or its limit is used up. Wait a minute and ask again. " + e.message;
  if (e.status === 503) return "The question service is not ready: " + e.message;
  if (e.status === 404) return "The question service is not running on this server (Layer 2 not mounted).";
  return e.message;
}
// a small bar chart when the table is "label + number"
function tableChart(t) {
  if (!t || !t.rows || t.rows.length < 2 || t.rows.length > 25) return null;
  const numCol = t.columns.findIndex((c, i) => i > 0 && t.rows.every((r) => r[i] === null || typeof r[i] === "number"));
  if (numCol < 0) return null;
  const isRate = t.rows.every((r) => r[numCol] === null || (r[numCol] >= 0 && r[numCol] <= 1)) && /rate|share|pct|ratio/i.test(t.columns[numCol]);
  return hbars(t.rows.filter((r) => r[numCol] !== null), { label: (r) => String(r[0] ?? "—"), value: (r) => Math.abs(r[numCol]),
    text: (r) => isRate ? fmt.pct(r[numCol]) : Number.isInteger(r[numCol]) ? fmt.int(r[numCol]) : fmt.num(r[numCol], 2), color: () => "var(--cyan)" });
}
function answerCard(turn, onAsk) {
  const a = turn.answer, meta = a.meta || {};
  const [st, cls] = ASK_STATUS[a.status] || [a.status.toUpperCase(), ""];
  const fb = h("span", { class: "muted" });
  const sendFb = async (rating) => { try { await api2("/feedback", { method: "POST", body: { turn_id: turn.turn_id, rating } }); fb.textContent = " thanks, recorded"; } catch (e) { fb.textContent = " " + e.message; } };
  const t = a.table;
  const table = t && t.columns ? h("table", null, h("thead", null, h("tr", null, t.columns.map((c) => h("th", null, fmt.label(c))))),
    h("tbody", null, t.rows.slice(0, 50).map((r) => h("tr", null, r.map((v) => h("td", { class: typeof v === "number" ? "n" : "" },
      v === null ? "—" : typeof v === "number" ? (Number.isInteger(v) ? fmt.int(v) : fmt.num(v, Math.abs(v) < 1 ? 4 : 2)) : String(v))))))) : null;
  const sources = (a.sources || []).map((src) => h("div", { class: "src" },
    h("div", null, h("b", null, SOURCE_KIND[src.kind] || fmt.label(src.kind)), src.detail ? " · " + src.detail : ""),
    src.sql ? h("pre", { class: "sql" }, src.sql) : null,
    (src.ids || []).length ? h("div", { class: "muted" }, src.ids.slice(0, 12).join(", ") + (src.ids.length > 12 ? ` … (+${src.ids.length - 12})` : "")) : null));
  const steps = (turn.trace && turn.trace.steps) || [];
  return panel(`${turn.question}`, h("div", { class: "ask-card" },
    h("div", null, h("span", { class: "chip " + cls }, st),
      (a.interpretation || []).length ? h("span", { class: "muted" }, " read as: ", a.interpretation.map((x) => h("span", { class: "chip" }, x))) : null),
    h("div", { class: "ask-text" }, a.status === "refused" && a.refusal ? a.refusal.reason || a.answer_text : a.answer_text),
    a.insight ? h("div", { class: "note", style: "font-size:12px;margin-top:4px" }, "Insight: " + a.insight) : null,
    t && t.columns ? h("div", { class: "grid g-2", style: "margin-top:8px" }, h("div", null, table), h("div", null, tableChart(t))) : null,
    sources.length ? h("details", { open: true, style: "margin-top:8px" }, h("summary", null, "HOW WE GOT THIS (SQL and sources)"), sources) : null,
    (a.warnings || []).length ? h("details", { style: "margin-top:4px" }, h("summary", { class: "muted" }, `CHECKS (${a.warnings.length})`),
      h("ul", { class: "reasons" }, a.warnings.map((w) => h("li", null, w)))) : null,
    steps.length ? h("details", { style: "margin-top:4px" }, h("summary", { class: "muted" }, `STEPS THE AI TOOK (${steps.length})`),
      h("ol", { class: "steps" }, steps.map((x) => h("li", null, h("b", null, x.tool), " ", h("span", { class: "muted" }, JSON.stringify(x.args || {}).slice(0, 220)),
        x.executed === false ? h("span", { class: "bad" }, ` not run: ${x.note || ""}`) : null)))) : null,
    (a.follow_ups || []).length ? h("div", { style: "margin-top:8px" }, h("span", { class: "muted" }, "ASK NEXT "),
      a.follow_ups.map((f) => h("button", { class: "btn small", onclick: () => onAsk(f) }, f))) : null,
    h("div", { class: "note", style: "margin-top:8px" },
      `model ${meta.model || "—"} · ${meta.tool_calls ?? 0} tool call(s) · ${meta.latency_ms != null ? (meta.latency_ms / 1000).toFixed(1) + " s" : "—"} · data as of ${meta.snapshot_date || "—"} · `,
      h("button", { class: "more", onclick: () => sendFb("up"), title: "Good answer" }, "👍"), " ",
      h("button", { class: "more", onclick: () => sendFb("down"), title: "Wrong or unhelpful" }, "👎"), fb)), st);
}
async function viewAsk(initial) {
  state.askLog = state.askLog || [];
  const input = h("textarea", { class: "ask-input", placeholder: "Ask about the portfolio in plain English, e.g. What was the promise-kept rate by queue last month?", rows: 2 });
  const out = h("div", { class: "grid" });
  const status = h("div", { class: "note" }, "checking the question service…");
  const exBox = h("div", { class: "filters" });
  const btn = h("button", { class: "btn approve" }, "ASK");
  const render = () => out.replaceChildren(...state.askLog.map((t) => t.error ? h("div", { class: "banner" }, h("b", null, "COULD NOT ANSWER"), `${t.question}: ${t.error}`) : answerCard(t, ask)));
  async function ask(q) {
    q = (q || input.value).trim();
    if (!q) return;
    input.value = ""; btn.disabled = true;
    const pending = h("div", { class: "loading" }, `THINKING ABOUT: ${q} … (the AI usually takes 10–40 s)`);
    out.prepend(pending);
    try {
      const r = await api2("/ask", { method: "POST", body: { question: q, conversation_id: state.askConv || null, include_trace: true } });
      state.askConv = r.conversation_id;
      state.askLog.unshift({ ...r, question: q });
    } catch (e) {
      state.askLog.unshift({ question: q, error: askErrorText(e) });
    }
    btn.disabled = false; render(); input.focus();
  }
  btn.addEventListener("click", () => ask());
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
  const reset = h("button", { class: "btn", onclick: () => { state.askConv = null; state.askLog = []; render(); input.focus(); } }, "NEW CONVERSATION");
  setView("ask", title("ASK", "plain-English questions about the whole portfolio · answered by Gemini through Layer 2 · every answer shows its SQL or sources"),
    status, h("div", { class: "ask-bar" }, input, h("div", { class: "bar", style: "margin:0" }, btn, reset)), exBox,
    h("p", { class: "note" }, "The AI can only read approved, de-identified tables, official metrics, notes, transcripts and current policies. It refuses questions about protected characteristics or personal contact details. Follow-up questions keep the context of this conversation."),
    out);
  render();
  input.focus();
  api2("/health").then((hl) => {
    const m = hl.models || {};
    const models = m.rotation ? `rotation of ${m.rotation.length} Gemini models (${m.rotation.join(", ")})` : (m.single || "no model set");
    status.replaceChildren(h("span", { class: "chip " + (hl.status === "ok" ? "grn" : hl.status === "degraded" ? "yel" : "red") }, "SERVICE " + hl.status.toUpperCase()),
      ` ${models}${m.last_used ? " · last answer by " + m.last_used : ""} · data build ${(hl.serving || {}).build_ts || "—"}${m.note ? " · " + m.note : ""}`);
  }).catch((e) => status.replaceChildren(h("span", { class: "chip red" }, "SERVICE DOWN"), " " + askErrorText(e)));
  api2("/examples").then((ex) => exBox.replaceChildren(h("span", { class: "muted" }, "TRY "),
    ...ex.map((x) => h("button", { class: "btn small", title: "route: " + x.route, onclick: () => ask(x.question) }, x.question)))).catch(() => {});
  if (initial) ask(initial);
}

// ---------- LOG A CONVERSATION (paste a transcript or notes -> facts, follow-ups, checks) ----------
// The draft lives in memory only (state.convDrafts); conversation text is never put in browser storage.
const CONV_FIELDS = [
  ["hardship", "Hardship", "enum", ["none", "possible", "clear"], "hardship"],
  ["hardship_reason", "Hardship reason", "enum", [null, "job_loss", "reduced_income", "illness", "disability", "separation", "ei_waiting", "disaster_evacuation", "rate_reset"], "hardship"],
  ["ptp_mentioned", "Promise to pay", "bool", null, "ptp"],
  ["ptp_amount", "Promise amount ($)", "number", null, "ptp"],
  ["ptp_date", "Promise date", "date", null, "ptp"],
  ["dispute", "Dispute", "bool", null, "dispute"],
  ["dispute_type", "Dispute type", "enum", [null, "late_fee", "payment_not_applied", "fraud", "billing_error"], "dispute"],
  ["cease_request", "Asked us to stop contact", "bool", null, "cease_request"],
  ["insolvency_mention", "Mentioned bankruptcy", "bool", null, "insolvency"],
  ["vulnerability", "Needs extra care", "bool", null, "vulnerability"],
  ["complaint", "Complaint", "bool", null, "complaint"],
  ["third_party_present", "Someone else on the call", "bool", null, "third_party"],
  ["legal_threat_by_agent", "Agent threatened legal action", "bool", null, "legal_threat"],
  ["contact_restriction", "Contact wish", "text", null, "contact_restriction"],
  ["sentiment", "Customer's mood", "enum", ["cooperative", "frustrated", "distressed", "hostile"], null],
  ["next_step", "Next step", "enum", ["stop_contact", "escalate_supervisor", "dispute_investigation", "hardship_plan_review", "send_payment_link", "call_back"], null],
];
const CONV_PROTECTIVE = ["cease_request", "insolvency_mention", "vulnerability", "legal_threat_by_agent"];
const CONV_KIND = { stop_contact: "Stop contact (contact is held now)", insolvency_check: "Check bankruptcy / consumer proposal (contact is held now)",
  vulnerability: "Flag for extra care", conduct: "Review agent conduct", promise: "Record the promise", hardship: "Send hardship request to a specialist",
  dispute: "Open a dispute", contact_preference: "Record the contact wish" };
const CONV_TYPE = { cease_request: "STOP CONTACT", insolvency_check: "BANKRUPTCY CHECK", vulnerability_flag: "EXTRA CARE", conduct_alert: "CONDUCT REVIEW",
  promise: "PROMISE LOGGED", hardship_request: "HARDSHIP → APPROVAL", dispute_request: "DISPUTE", contact_preference: "CONTACT WISH" };
const CONV_PLACEHOLDER = "[00:00] Agent: Hi, this is Priya calling from Maple Bank. This call may be recorded. Can you confirm your date of birth?\n" +
  "[00:12] Customer: Sure, it's on file. Look, I was laid off three weeks ago and things are really tight.\n" +
  "[00:25] Agent: I'm sorry to hear that. The amount past due today is $412.50. We do have hardship options like a reduced payment.\n" +
  "[00:41] Customer: I can pay $150 on the 14th. And please don't call me at work.\n" +
  "[00:55] Agent: Just to confirm, a payment of $150.00 on October 14. Thank you for your time.";

function convNow() { const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); }
function convWhere(ev, approx) { return ev ? (approx ? `line ${ev.turn_index + 1}` : `${Number(ev.sec || 0).toFixed(0)}s`) : ""; }
function convQuote(ev, approx) { return ev ? h("div", { class: "muted" }, `${convWhere(ev, approx)} "${ev.text.slice(0, 110)}${ev.text.length > 110 ? "…" : ""}"`) : null; }
function convTurns(turns, hl, approx) {
  return h("div", { class: "turns" }, turns.map((t) => h("div", { class: `turn ${t.speaker} ${hl.has(t.turn_index) ? "hl" : ""}` },
    h("span", { class: "t" }, approx ? `#${t.turn_index + 1}` : `${Number(t.start_sec || 0).toFixed(0)}s`), h("span", { class: "s" }, fmt.label(t.speaker)), h("span", null, t.text))));
}
function convErr(e) { const d = e.data || {}; return h("div", { class: "err" }, d.error || e.message, d.field ? ` (${fmt.label(d.field)})` : ""); }

async function viewLog(arg) {
  let caseId = (arg || "").trim();
  if (!caseId && state.lastCustomer && /^CS-/.test(state.lastCustomer)) caseId = state.lastCustomer;
  if (!/^CS-\d{4}-\d+$/.test(caseId)) return viewHelp("Open a customer first, then click + LOG CONVERSATION (or type LOG followed by a case id).");
  state.convDrafts = state.convDrafts || {};
  const d = state.convDrafts[caseId] = state.convDrafts[caseId] || { form: { mode: "transcript", channel: "call", direction: "outbound", started_at: convNow(), language: "auto", duration_min: "", text: "", transcript_json: null },
    draft: null, corrections: {}, reasons: {}, kinds: null, note: "" };
  const body = () => ({ mode: d.form.mode, text: d.form.text, transcript_json: d.form.transcript_json, channel: d.form.channel, direction: d.form.direction,
    started_at: d.form.started_at, language: d.form.language, duration_sec: d.form.duration_min === "" ? null : Math.round(Number(d.form.duration_min) * 60) });
  const head = title("LOG A CONVERSATION", `case ${caseId} · paste what was said, check what the system found, then save it to the customer's file`);

  function capture(msg) {
    const sel = (key, opts) => h("select", { onchange: (e) => { d.form[key] = e.target.value; } }, opts.map(([v, l]) => h("option", { value: v, selected: d.form[key] === v }, l)));
    const counter = h("span", { class: "muted" }, `${d.form.text.length.toLocaleString()} / 20,000`);
    const ta = h("textarea", { class: "ask-input", rows: 14, maxlength: 20000, placeholder: CONV_PLACEHOLDER });
    ta.value = d.form.text;
    ta.addEventListener("input", () => { d.form.text = ta.value; d.form.transcript_json = null; counter.textContent = `${ta.value.length.toLocaleString()} / 20,000`; });
    const file = h("input", { type: "file", accept: ".txt,.json", "aria-label": "Load a file" });
    file.addEventListener("change", () => { const f = file.files[0]; if (!f) return; const r = new FileReader();
      r.onload = () => { const txt = String(r.result || ""); let js = null; if (/\.json$/i.test(f.name)) { try { js = JSON.parse(txt); } catch (e) { js = null; } }
        if (js && js.turns) { d.form.transcript_json = js; d.form.mode = "transcript"; ta.value = js.turns.map((t) => `${fmt.label(t.speaker)}: ${t.text}`).join("\n"); }
        else { d.form.transcript_json = null; ta.value = txt.slice(0, 20000); }
        d.form.text = ta.value; counter.textContent = `${ta.value.length.toLocaleString()} / 20,000`; };
      r.readAsText(f); });
    const out = h("div", null, msg || null);
    const go = h("button", { class: "btn approve" }, "EXTRACT DATA");
    const run = async () => {
      if (go.disabled) return; go.disabled = true; out.replaceChildren(loading("READING THE CONVERSATION"));
      try { d.draft = await api(`/cases/${caseId}/conversations/extract`, { method: "POST", body: body() }); d.corrections = {}; d.reasons = {}; d.kinds = null; review(); }
      catch (e) { go.disabled = false; out.replaceChildren(convErr(e)); }
    };
    go.addEventListener("click", run);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
    const when = h("input", { type: "datetime-local", value: d.form.started_at, max: convNow() }); when.addEventListener("change", () => { d.form.started_at = when.value; });
    const dur = h("input", { type: "number", min: 0, max: 240, step: 1, value: d.form.duration_min, placeholder: "optional" }); dur.addEventListener("change", () => { d.form.duration_min = dur.value; });
    setView("log", head, panel("STEP 1 · WHAT WAS SAID", h("div", null,
      h("div", { class: "form" },
        h("label", null, "How"), sel("channel", [["call", "Phone call"], ["sms", "Text messages"], ["email", "Email"], ["chat", "Chat"], ["in_person", "In person"], ["video", "Video call"]]),
        h("label", null, "Who started it"), sel("direction", [["outbound", "We contacted the customer"], ["inbound", "The customer contacted us"]]),
        h("label", null, "When"), when,
        h("label", null, "Language"), sel("language", [["auto", "Detect automatically"], ["en", "English"], ["fr", "French"]]),
        h("label", null, "Length (minutes)"), dur,
        h("label", null, "What you're pasting"), sel("mode", [["transcript", "TRANSCRIPT (lines start with Agent: / Customer:)"], ["notes", "MY NOTES (free text)"]])),
      h("div", { style: "margin-top:8px" }, ta),
      h("div", { class: "bar" }, go, h("button", { class: "btn", onclick: () => { ta.value = CONV_PLACEHOLDER; d.form.text = ta.value; d.form.mode = "transcript"; counter.textContent = `${ta.value.length.toLocaleString()} / 20,000`; } }, "LOAD EXAMPLE"),
        h("button", { class: "btn", onclick: () => route(caseId) }, "CANCEL"), h("span", { class: "muted" }, "or load a .txt / .json file "), file, counter),
      h("div", { class: "note" }, "Card numbers, SINs, phone numbers and emails are masked before anything is stored. Nothing is saved until you confirm on the next step. Ctrl+Enter = extract."),
      out), "transcript or your own notes"));
    ta.focus();
    if (new URLSearchParams(location.search).get("sample") === "1" && !d.form.text) { d.form.text = ta.value = CONV_PLACEHOLDER; run(); }   // demo link
  }

  function review() {
    const dr = d.draft, approx = !!dr.times_approximate;
    const fields = dr.fields_final || dr.fields;
    if (d.kinds === null) d.kinds = dr.proposed_updates.filter((u) => u.default_selected || u.mandatory).map((u) => u.kind);
    const hl = new Set(Object.values(dr.evidence || {}).filter(Boolean).map((e) => e.turn_index));
    const msg = h("div");
    const changed = (k) => Object.prototype.hasOwnProperty.call(d.corrections, k);
    let timer = null;
    const recheck = () => { clearTimeout(timer); timer = setTimeout(async () => {
      try { d.draft = await api(`/cases/${caseId}/conversations/extract`, { method: "POST", body: { ...body(), corrections: d.corrections, correction_reasons: d.reasons, selected_kinds: d.kinds } }); review(); }
      catch (e) { msg.replaceChildren(convErr(e)); } }, 350); };
    const setField = (k, v) => { if (v === dr.fields[k] || (v === "" && dr.fields[k] === null)) delete d.corrections[k]; else d.corrections[k] = v === "" ? null : v; recheck(); };
    const control = ([k, , type, opts]) => {
      const v = fields[k];
      if (type === "bool") {
        if (v === null) return h("span", { class: "muted" }, "not assessed (notes)");
        const c = h("input", { type: "checkbox", checked: v === true ? true : null, "aria-label": k }); c.addEventListener("change", () => setField(k, c.checked)); return c;
      }
      if (type === "enum") { const s = h("select", null, opts.map((o) => h("option", { value: o === null ? "" : o, selected: (v ?? "") === (o ?? "") }, o === null ? "—" : plain("hardship", o))));
        s.addEventListener("change", () => setField(k, s.value === "" ? null : s.value)); return s; }
      const i = h("input", { type: type === "number" ? "number" : type === "date" ? "date" : "text", step: "0.01", value: v ?? "" });
      i.addEventListener("change", () => setField(k, type === "number" ? (i.value === "" ? null : Number(i.value)) : i.value)); return i;
    };
    const rows = CONV_FIELDS.map((f) => { const [k, label, , , evKey] = f;
      const needWhy = CONV_PROTECTIVE.includes(k) && dr.fields[k] === true && d.corrections[k] === false;
      const why = needWhy ? h("input", { placeholder: "Why is this not right? (required)", value: d.reasons[k] || "", style: "width:100%" }) : null;
      if (why) why.addEventListener("input", () => { d.reasons[k] = why.value; });
      return h("tr", null, h("td", { class: "muted" }, label, changed(k) ? h("span", { class: "chip yel", style: "margin-left:6px" }, "CHANGED") : null),
        h("td", null, control(f), why), h("td", null, convQuote((dr.evidence || {})[evKey], approx))); });
    const alertRows = (dr.alerts || []).map((a) => h("li", null, a.text, a.cite ? h("span", { class: "cite" }, a.cite) : null, convQuote(a.evidence, approx)));
    const stop = (dr.alerts || []).filter((a) => a.level === "stop"), care = (dr.alerts || []).filter((a) => a.level !== "stop");
    const updates = dr.proposed_updates.map((u) => { const c = h("input", { type: "checkbox", checked: d.kinds.includes(u.kind) || u.mandatory ? true : null, disabled: u.mandatory ? true : null, "aria-label": CONV_KIND[u.kind] });
      c.addEventListener("change", () => { d.kinds = c.checked ? [...new Set([...d.kinds, u.kind])] : d.kinds.filter((x) => x !== u.kind); recheck(); });
      return h("div", { class: "upd" }, c, " ", h("b", null, CONV_KIND[u.kind] || fmt.label(u.kind)), u.mandatory ? h("span", { class: "chip red", title: "Cannot be skipped" }, "🔒 REQUIRED") : null,
        h("span", { class: "chip" }, "approval: " + (u.needs === "agent" ? "you" : plain("role", u.needs).toLowerCase())),
        h("div", { class: "muted" }, u.why, u.cite ? h("span", { class: "cite" }, u.cite) : null)); });
    const blocking = (dr.checks || []).filter((c) => c.blocking);
    const note = h("textarea", { placeholder: "Your note (optional)", maxlength: 1000, style: "width:100%;background:#000;border:1px solid var(--rule);margin-top:6px" }); note.value = d.note; note.addEventListener("input", () => { d.note = note.value; });
    const save = h("button", { class: "btn approve", disabled: blocking.length ? true : null }, "SAVE TO CUSTOMER FILE");
    save.addEventListener("click", async () => {
      if (save.disabled) return; save.disabled = true; msg.replaceChildren(loading("SAVING"));
      const selected = dr.proposed_updates.filter((u) => u.mandatory || d.kinds.includes(u.kind)).map((u) => u.id);
      try {
        const out = await api(`/cases/${caseId}/conversations`, { method: "POST", body: { ...body(), corrections: d.corrections, correction_reasons: d.reasons, selected_updates: selected, agent_note: d.note || null, confirm: true } });
        delete state.convDrafts[caseId];
        const made = ["Conversation logged"].concat(out.created.map((i) => i.type === "promise" ? `Promise ${fmt.money(i.payload.amount)} on ${fmt.date(i.payload.date)} recorded`
          : `${CONV_TYPE[i.type] || fmt.label(i.type)} sent for approval${["cease_request", "insolvency_check"].includes(i.type) ? " (contact on hold now)" : ""}`));
        setView("log", head, h("div", { class: "banner", style: "border-color:var(--green);background:#0c1b10" }, h("b", { style: "color:var(--green)" }, "SAVED"), made.join(" · "),
          h("div", { class: "bar" }, h("button", { class: "btn approve", onclick: () => route(caseId) }, "OPEN CUSTOMER"))));
        setTimeout(() => { if (state.view === "log") route(caseId); }, 2000);
      } catch (e) {
        save.disabled = false; const det = e.data || {};
        msg.replaceChildren(convErr(e), det.checks ? h("ul", { class: "reasons conflicts" }, det.checks.filter((c) => c.blocking).map((c) => h("li", null, c.text, c.cite ? h("span", { class: "cite" }, c.cite) : null))) : null);
      }
    });
    const discard = h("div", { hidden: true, class: "note" }, "Discard this draft? ", h("button", { class: "btn reject", onclick: () => { delete state.convDrafts[caseId]; route(caseId); } }, "YES, DISCARD"), " ",
      h("button", { class: "btn", onclick: () => { discard.hidden = true; } }, "KEEP"));
    const qa = dr.qa;
    setView("log", head, h("div", { class: "grid g-2" },
      panel("CONVERSATION", h("div", null, (dr.warnings || []).map((w) => h("div", { class: "warn" }, w)), convTurns(dr.turns, hl, approx)), "highlighted = where a finding came from · masked"),
      h("div", { class: "grid" },
        stop.length || care.length ? panel("MUST KNOW", h("div", null,
          stop.length ? h("ul", { class: "reasons conflicts" }, alertRows.slice(0, stop.length)) : null,
          care.length ? h("ul", { class: "reasons safeguards" }, alertRows.slice(stop.length)) : null)) : null,
        panel("SUMMARY", h("div", null, h("div", { style: "font-size:15px" }, dr.summary), Object.keys(d.corrections).length ? h("div", { class: "note" }, "Updated with your changes; the final summary is written when you save.") : null)),
        panel("WHAT WAS SAID (FOUND AUTOMATICALLY)", h("table", null, h("tbody", null, rows)), "change anything that is wrong"),
        panel("UPDATES TO MAKE", updates.length ? h("div", null, updates) : h("div", { class: "muted" }, "Nothing to update: only the conversation itself will be logged."), "ticked ones are created when you save"),
        (dr.checks || []).length ? panel("CHECKS", h("ul", { class: "reasons " + (blocking.length ? "conflicts" : "safeguards") }, dr.checks.map((c) => h("li", null, c.blocking ? "Fix before saving: " : "", c.text, c.cite ? h("span", { class: "cite" }, c.cite) : null)))) : null,
        panel(qa ? `CALL QUALITY SCORE ${qa.score}/100` : "CALL QUALITY", qa ? h("div", null, qa.coaching_required ? h("div", { class: "bad" }, "COACHING NEEDED: " + qa.critical_failures.map(fmt.label).join(", ")) : h("div", { class: "good" }, "No serious failures"),
          h("table", { class: "qa" }, h("tbody", null, qa.items.map((i) => h("tr", { title: i.pass_criteria }, h("td", null, i.item_no), h("td", null, i.name),
            h("td", { class: i.verdict === "pass" ? "pass" : i.verdict === "fail" ? "fail" : "na" }, i.verdict.replace("_", " ").toUpperCase()), h("td", { class: "n" }, i.weight)))))) : h("div", { class: "muted" }, "Quality score needs a transcript.")),
        panel("SAVE", h("div", null, note,
          h("div", { class: "bar" }, save, h("button", { class: "btn", onclick: () => capture() }, "EDIT TEXT"), h("button", { class: "btn reject", onclick: () => { discard.hidden = false; } }, "DISCARD")),
          discard, msg, h("div", { class: "note" }, "The server reads the text again when you save; your changes are stored next to what was found automatically."))))));
  }
  if (d.draft) review(); else capture();
}

async function viewConv(id) {
  if (!id) return viewHelp("Type CONV followed by a conversation id (shown on the customer page).");
  id = /^cnv-/i.test(id) ? "CNV-" + id.slice(4).toLowerCase() : id;      // the command line upper-cases what is typed
  setView("log", loading("CONVERSATION " + id));
  let r;
  try { r = await api("/conversations/" + encodeURIComponent(id)); }
  catch (e) { setView("log", title("CONVERSATION " + id), errorBox(e)); return; }
  const it = r.conversation, p = it.payload, f = p.fields_final, approx = p.mode === "notes" || (p.warnings || []).includes("Times are approximate.");
  const hl = new Set(Object.values(p.evidence || {}).filter(Boolean).map((e) => e.turn_index));
  const corr = Object.fromEntries((p.corrections || []).map((c) => [c.field, c]));
  const show = (v) => v === null || v === undefined ? "—" : v === true ? "yes" : v === false ? "no" : fmt.label(v);
  setView("log", title("CONVERSATION " + p.conversation_id, `case ${it.case_id} · ${fmt.ts(p.started_at)} · ${fmt.label(p.channel)} · logged by ${it.actor} at ${fmt.ts(it.ts)} · read only`),
    h("div", { class: "grid g-2" },
      panel("CONVERSATION (MASKED)", convTurns(p.turns_masked || [], hl, approx)),
      h("div", { class: "grid" },
        panel("SUMMARY", h("div", null, h("div", { style: "font-size:15px" }, p.summary), p.agent_note ? h("div", { class: "muted", style: "margin-top:6px" }, "Agent's note: " + p.agent_note) : null,
          h("div", { style: "margin-top:6px" }, h("span", { class: "id", onclick: () => route(it.case_id) }, "open case " + it.case_id)))),
        panel("FOLLOW-UPS", (r.followups || []).length ? h("table", null, h("tbody", null, r.followups.map((x) => h("tr", null, h("td", null, CONV_TYPE[x.type] || fmt.label(x.type)),
          h("td", { class: x.status === "rejected" ? "bad" : x.status === "pending" ? "warn" : "good" }, x.status), h("td", { class: "muted" }, x.decided_by || ""))))) : h("div", { class: "muted" }, "None.")),
        panel("WHAT WAS SAID", h("table", null, h("tbody", null, CONV_FIELDS.map(([k, label]) => h("tr", null, h("td", { class: "muted" }, label), h("td", null, show(f[k])),
          h("td", { class: "muted" }, corr[k] ? `changed from ${show(corr[k].from)}${corr[k].reason ? " — " + corr[k].reason : ""}` : ""))))), p.engine))));
}

// plain words for the effects of logged conversations (customer page banner)
function overlayText(e) {
  const what = { cease_contact_flag: "Customer asked us to stop contacting them. Calls, texts and emails are on hold now.", insolvency_hold_flag: "Bankruptcy or a consumer proposal was mentioned. Contact is on hold until it is checked.",
    vulnerable_customer_flag: "The customer may need extra care: safeguards apply.", hardship_flag: "Hardship was stated: offer help before asking for payment.", dispute_flag: "A dispute was raised: sort it out before collecting the disputed amount.",
    "promises.logged": e.value && e.value.amount != null ? `Promise of ${fmt.money(e.value.amount)} on ${fmt.date(e.value.date)} was logged.` : "A promise was logged.",
    contact_preference: `Contact wish: "${e.value}".`, conduct_alert: "A call was flagged for an agent conduct review." }[e.field] || fmt.label(e.field);
  const st = e.status === "pending" ? "waiting for approval" : e.status;
  return `${what} Logged ${fmt.date(e.ts)} by ${e.logged_by} · ${st}.`;
}

// ---------- CALL ----------
async function viewCall(id) {
  setView("call", loading("CALL " + id));
  const d = await api("/calls/" + encodeURIComponent(id));
  state.lastCall = id;
  const a = d.analysis, f = a.fields, qa = a.qa;
  const evSecs = new Set(Object.values(a.evidence || {}).filter(Boolean).map((e) => e.sec));
  qa.items.forEach((i) => i.evidence && evSecs.add(i.evidence.sec));
  const turns = h("div", { class: "turns" }, d.turns.map((t) => h("div", { class: `turn ${t.speaker} ${evSecs.has(t.start_sec) ? "hl" : ""}` },
    h("span", { class: "t" }, `${Number(t.start_sec).toFixed(0)}s`), h("span", { class: "s" }, t.speaker), h("span", null, t.text))));
  const fieldRows = [
    ["Hardship", f.hardship === "none" ? "none" : `${f.hardship}${f.hardship_reason ? " · " + plain("hardship", f.hardship_reason) : ""}`, a.evidence.hardship],
    ["Promise", f.ptp_mentioned ? `${fmt.money(f.ptp_amount)} on ${fmt.date(f.ptp_date)}` : "none", a.evidence.ptp],
    ["Dispute", f.dispute ? fmt.label(f.dispute_type) : "no", a.evidence.dispute],
    ["Asked us to stop contact", f.cease_request ? "YES" : "no", a.evidence.cease_request],
    ["Needs extra care", f.vulnerability ? fmt.label(f.vulnerability_type) : "no", a.evidence.vulnerability],
    ["Complaint", f.complaint ? "yes" : "no", a.evidence.complaint],
    ["Mentioned bankruptcy", f.insolvency_mention ? "YES" : "no", a.evidence.insolvency],
    ["Someone else on the call", f.third_party_present ? "yes" : "no", a.evidence.third_party],
    ["Agent threatened legal action", f.legal_threat_by_agent ? "YES" : "no", a.evidence.legal_threat],
    ["Customer's mood", fmt.label(f.sentiment), null],
    ["Next step", fmt.label(f.next_step), null],
  ];
  const fields = h("table", null, h("tbody", null, fieldRows.map(([k, v, ev]) => h("tr", null, h("td", { class: "muted" }, k),
    h("td", { class: /YES/.test(v) ? "bad" : "" }, v), h("td", { class: "muted" }, ev ? `${Number(ev.sec).toFixed(0)}s "${ev.text.slice(0, 60)}${ev.text.length > 60 ? "…" : ""}"` : "")))));
  const qaTable = h("table", { class: "qa" }, h("thead", null, h("tr", null, ["#", "CHECK", "RESULT", "POINTS", "WHERE"].map((c) => h("th", null, c)))),
    h("tbody", null, qa.items.map((i) => h("tr", { title: i.pass_criteria },
      h("td", null, i.item_no), h("td", null, i.name, i.critical ? h("span", { class: "chip red" }, "CRITICAL") : null),
      h("td", { class: i.verdict === "pass" ? "pass" : i.verdict === "fail" ? "fail" : "na" }, i.verdict.replace("_", " ").toUpperCase()),
      h("td", { class: "n" }, i.weight), h("td", { class: "muted" }, i.evidence ? `${Number(i.evidence.sec).toFixed(0)}s` : "", i.note ? h("div", { class: "bad" }, i.note) : null)))));
  const msg = h("div");
  const review = async (verdict) => {
    try { await api(`/calls/${id}/review`, { method: "POST", body: { verdict, note: note.value || null } }); msg.replaceChildren(h("div", { class: "ok" }, "Review recorded on the customer's file.")); }
    catch (e) { msg.replaceChildren(errorBox(e)); }
  };
  const note = h("textarea", { placeholder: "Reviewer note (optional)", style: "width:100%;background:#000;border:1px solid var(--rule);margin-top:8px" });
  setView("call", title("CALL REVIEW " + id, `case ${a.case_id || "—"} · ${fmt.ts(a.call_start_ts)} · ${a.duration_sec}s · ${a.direction} · ${a.language || ""}`),
    h("div", { class: "grid g-2" },
      panel("TRANSCRIPT", turns, "highlighted lines = where a finding came from"),
      h("div", { class: "grid" },
        panel("SUMMARY", h("div", null, h("div", { style: "font-size:15px" }, a.summary),
          a.case_id ? h("div", { style: "margin-top:6px" }, h("span", { class: "id", onclick: () => route(a.case_id) }, "open case " + a.case_id)) : null)),
        panel("WHAT WAS SAID (FOUND AUTOMATICALLY)", fields, "rules, no AI model"),
        panel(`CALL QUALITY SCORE ${qa.score}/100`, h("div", null, qa.coaching_required ? h("div", { class: "bad" }, "COACHING NEEDED: " + qa.critical_failures.map(fmt.label).join(", ")) : h("div", { class: "good" }, "No serious failures"),
          qaTable), "100 minus the points of each failed check"),
        panel("YOUR REVIEW", h("div", null, h("div", { class: "note" }, "A person confirms or corrects the summary before it is used for coaching."), note,
          h("div", { class: "bar" }, h("button", { class: "btn approve", onclick: () => review("accept") }, "ACCEPT SUMMARY"), h("button", { class: "btn", onclick: () => review("correct") }, "FLAG FOR CORRECTION")), msg,
          (d.reviews || []).map((r) => h("div", { class: "muted" }, `${fmt.ts(r.ts)} ${r.actor}: ${r.payload.verdict}${r.payload.note ? " — " + r.payload.note : ""}`)))))));
}

// ---------- APPR ----------
async function viewAppr() {
  setView("appr", loading("APPROVALS"));
  const d = await api("/approvals?status=pending");
  const rows = d.items.map((i) => {
    const p = i.payload || {};
    const msg = h("div");
    const reason = h("input", { placeholder: "reason (required to reject)", style: "background:#000;border:1px solid var(--rule);padding:3px 6px;width:180px" });
    const act = async (status) => {
      try { await api(`/approvals/${i.id}`, { method: "POST", body: { status, reason: reason.value || null } }); msg.replaceChildren(h("span", { class: "ok" }, status.toUpperCase())); setTimeout(viewAppr, 700); }
      catch (e) { msg.replaceChildren(errorBox(e)); }
    };
    const what = i.type === "hardship_request" && !p.program_id ? `Programme: to be chosen by the specialist · reason ${plain("hardship", p.reason)}${p.evidence ? ` · "${String(p.evidence).slice(0, 90)}"` : ""}`
      : i.type === "hardship_request" ? `${p.program_id} ${p.program_name || ""} · reason ${fmt.label(p.reason)}${p.estimate && p.estimate.amount != null ? ` · ~${fmt.money(p.estimate.amount)}/mo × ${p.estimate.months}` : ""}${p.requires_income_evidence ? " · needs income evidence" : ""}`
      : i.type === "cease_request" ? (p.conversation_id ? "Customer asked us to stop contact, logged from a conversation (POL-COLL-001 §5.4)" : "Written cease-communication request (POL-COLL-001 §5.4)")
      : i.type === "dispute_request" ? `${fmt.label(p.dispute_type)}: ${p.details || ""}`
      : i.type === "callback_request" ? `Callback ${p.time || ""} · ${p.topic || ""}`
      : i.type === "contact_preference" ? (p.note ? `Contact wish: "${p.note}"` : `Channels: ${JSON.stringify(p.channels || {})}`)
      : i.type === "vulnerability_flag" ? "Possible vulnerability: check and confirm"
      : i.type === "insolvency_check" ? "Bankruptcy mentioned: verify before any contact"
      : i.type === "conduct_alert" ? "Agent conduct: review call" : "";
    return h("tr", null, h("td", { class: "muted" }, fmt.ts(i.ts)), h("td", null, h("span", { class: "id", onclick: () => route(i.case_id) }, i.case_id)),
      h("td", null, fmt.label(i.type)), h("td", null, what, p.note ? h("div", { class: "muted" }, "Customer: " + p.note) : null),
      h("td", null, p.approver_level ? fmt.label(p.approver_level) : "agent"),
      h("td", null, h("div", { class: "bar", style: "margin:0" }, reason, h("button", { class: "btn reject", onclick: () => act("rejected") }, "REJECT"), h("button", { class: "btn approve", onclick: () => act("approved") }, "APPROVE")), msg));
  });
  setView("appr", title("REQUESTS WAITING FOR A PERSON", "sent by customers from the self-service app · approve or reject each one"),
    panel("PENDING", rows.length ? h("table", null, h("thead", null, h("tr", null, ["RECEIVED", "CASE", "TYPE", "REQUEST", "APPROVER", ""].map((c) => h("th", null, c)))), h("tbody", null, rows))
      : h("div", { class: "empty" }, "Nothing waiting. Requests made in the customer app (/app) appear here.")));
}

// ---------- QA (post-call engine accuracy) ----------
async function viewQa() {
  setView("qa", loading("EVALUATION"));
  let d;
  try { d = await api("/postcall/eval"); } catch (e) { setView("qa", title("POST-CALL ACCURACY"), errorBox(e)); return; }
  const rows = Object.entries(d.fields).map(([k, v]) => h("tr", null, h("td", null, fmt.label(k)),
    h("td", { class: "n " + (v.accuracy >= 0.95 ? "good" : v.accuracy >= 0.85 ? "" : "warn") }, fmt.pct(v.accuracy)), h("td", { class: "n muted" }, `${v.correct}/${v.n}`)));
  setView("qa", title("HOW ACCURATE IS THE AUTOMATIC CALL REVIEW?", `checked against ${d.n_labels} calls labelled by people (${d.labels})`),
    panel("SHARE OF CALLS WHERE IT AGREES WITH THE PERSON", h("table", null, h("thead", null, h("tr", null, h("th", null, "FIELD"), h("th", { class: "n" }, "ACCURACY"), h("th", { class: "n" }, "N"))), h("tbody", null, rows)),
      "rules, no AI model · rerun with python -m c360.layer3.postcall --eval"));
}

// ---------- POL ----------
async function viewPol(q) {
  const input = h("input", { value: q || "", placeholder: "e.g. how many call attempts in a week", style: "width:60%;background:#000;border:1px solid var(--rule);padding:5px 8px;color:var(--yellow)" });
  const out = h("div");
  const run = async () => {
    if (!input.value.trim()) return;
    out.replaceChildren(loading("POLICY"));
    const d = await api("/policy?q=" + encodeURIComponent(input.value) + "&k=4");
    out.replaceChildren(...(d.hits.length ? d.hits.map((s) => panel(`${s.cite} · ${s.title}`, h("div", null, h("div", { class: "muted" }, `${s.heading} · v${s.version} · effective ${s.effective_date}`),
      h("pre", { style: "white-space:pre-wrap;font-family:inherit;margin:6px 0 0" }, s.text)), s.doc_type)) : [h("div", { class: "empty" }, "No matching section.")]));
  };
  input.addEventListener("keydown", (e) => e.key === "Enter" && run());
  setView("pol", title("POLICY SEARCH", "ask in your own words · only current versions · when a policy and an FAQ disagree, the policy wins"), h("div", { class: "filters" }, input, h("button", { class: "btn", onclick: run }, "SEARCH")), h("div", { class: "grid" }, out));
  if (q) run();
}

// ---------- boot ----------
$("#cmdForm").addEventListener("submit", (e) => { e.preventDefault(); route($("#cmd").value); });
document.addEventListener("keydown", (e) => {
  const m = /^F(10|[1-9])$/.exec(e.key);
  if (m) { e.preventDefault(); const [, , view] = FKEYS[Number(m[1]) - 1];
    route(view === "cust" ? (state.lastCustomer || "HELP") : view === "call" ? (state.lastCall ? "CALL " + state.lastCall : "HELP") : view); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#cmd").focus(); }
  if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); $("#cmd").focus(); }
});
(async () => {
  try { state.health = await api("/health"); } catch (e) { /* shown by views */ }
  renderShell();
  const q = new URLSearchParams(location.search).get("q");
  route(q || "MON");
})();
