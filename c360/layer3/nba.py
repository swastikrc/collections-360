"""Next best action for one collections case, with reasons, policy citations and conflict checks.

Rules, not a model, so every recommendation can be explained line by line and audited. The order
follows the bank's policies: legal holds first (insolvency, deceased), then communication limits
(cease, representatives), then disputes, then hardship before any request for payment
(POL-COLL-004 §4.2), then promise follow-up, then the routine treatment for the stage.

Nothing here reads protected attributes; the document doesn't carry them. Vulnerability is used
only as a safeguard (route to a certified agent, use the vulnerable-customer script), never to
lower priority (POL-COLL-004 §4.5).

An open promise is scored by the promise risk model (c360/models/ptp_risk.py, features from the feature
store). The score is advice inside these rules, never a rule of its own: it can turn a reminder into a
call when the promise is likely to fail (so the agent can check affordability), and it adds its
reasons and safeguards (hardship, distress, dispute) for the agent to read.

A recommendation is only a proposal: a person approves, edits or rejects it (see `check()` and the
approval endpoints). `check()` is the same rule the UI uses to disable Approve.
"""
import datetime as dt
import os

from c360.layer3 import config, hardship
from c360.layer3.util import as_date, as_dt, flag, money, short_date

ENGINE_VERSION = "nba-rules-1.1"
STAT_HOLIDAYS = {dt.date(2026, 9, 30), dt.date(2026, 10, 12), dt.date(2026, 11, 11), dt.date(2026, 12, 25)}
CALL_CAP_7D = 3

ACTIONS = {
    "HOLD_DECEASED": "Stop asking for payment; the estate team takes over",
    "HOLD_INSOLVENCY": "Stop all contact about the debt (bankruptcy or consumer proposal)",
    "LETTERS_ONLY": "Customer asked us to stop contact: send only the letters we must send",
    "CONTACT_REPRESENTATIVE": "Contact the customer's representative, not the customer",
    "RESOLVE_DISPUTE": "Sort out the customer's dispute before asking for the disputed amount",
    "OFFER_HARDSHIP": "Offer a hardship programme before asking for payment",
    "PLAN_END_REVIEW": "Hardship plan ends soon: a specialist calls to review it",
    "MONITOR_PLAN": "Plan on track: send a payment reminder, no collection call",
    "PTP_REMINDER": "Remind the customer before the promise date",
    "PTP_FOLLOW_UP": "Promised payment did not arrive: follow up",
    "PAYMENT_PLAN": "Several promises broken: offer a smaller, regular payment plan",
    "SETTLEMENT_REVIEW": "Very late: consider offering to settle for less",
    "DIGITAL_REMINDER": "Recently late: send a reminder with a payment link",
    "AGENT_CALL": "Call the customer to agree a payment",
    "NO_ACTION": "No collection action needed",
}
CONTACT_ACTIONS = {"OFFER_HARDSHIP", "PLAN_END_REVIEW", "MONITOR_PLAN", "PTP_REMINDER", "PTP_FOLLOW_UP",
                   "PAYMENT_PLAN", "SETTLEMENT_REVIEW", "DIGITAL_REMINDER", "AGENT_CALL", "RESOLVE_DISPUTE"}
PAYMENT_ASKS = {"PTP_FOLLOW_UP", "PAYMENT_PLAN", "SETTLEMENT_REVIEW", "DIGITAL_REMINDER", "AGENT_CALL"}


def promise_risk(latest):
    """The promise risk model's view of an open promise, or None (no open promise, model not trained, data not
    reachable, or switched off with C360_L3_PROMISE_MODEL=off). Never raises: the rules work without it."""
    if os.environ.get("C360_L3_PROMISE_MODEL", "on") == "off" or not latest or latest.get("status") != "open" \
            or not latest.get("ptp_id"):
        return None
    try:
        from c360.models import ptp_risk
        res = ptp_risk.score(latest["ptp_id"])
    except Exception:       # noqa: BLE001 - advice only; the recommendation stands without it
        return None
    return res if res and not res.get("error") else None


def pick_case(doc, case_id=None):
    cases = (doc.get("collections") or {}).get("cases") or []
    if case_id:
        for c in cases:
            if c.get("case_id") == case_id:
                return c
        return None
    open_cases = [c for c in cases if c.get("outcome") is None]
    pool = open_cases or cases
    return max(pool, key=lambda c: (c.get("current_dpd") or 0, c.get("total_overdue") or 0)) if pool else None


def channels(doc, case=None):
    """Which channels may be used now, and why not for the others."""
    sc = doc.get("servicing_constraints") or {}
    contacts = (doc.get("collections") or {}).get("contacts") or {}
    blocked = {}
    legal_stop = flag(sc.get("bankruptcy")) or flag(sc.get("consumer_proposal")) or flag((case or {}).get("insolvency_hold_flag"))
    if legal_stop:
        for ch in ("call", "sms", "email"):
            blocked[ch] = ("Insolvency filed: stay of proceedings, all collection contact stops.", "POL-COLL-001 §7.3")
    if flag(sc.get("deceased")) or flag((case or {}).get("deceased_hold_flag")):
        for ch in ("call", "sms", "email"):
            blocked.setdefault(ch, ("Customer recorded as deceased: estate process only.", "POL-COLL-004 §4.5"))
    if flag(sc.get("cease_communication")) or flag((case or {}).get("cease_contact_flag")):
        for ch in ("call", "sms", "email"):
            blocked.setdefault(ch, ("Written cease-communication request: calls, texts and emails stop; letters continue.", "POL-COLL-001 §5.4"))
    if any(flag(sc.get(k)) for k in ("credit_counselling_agency", "legal_representative")) or flag((case or {}).get("third_party_rep_flag")):
        for ch in ("call", "sms", "email"):
            blocked.setdefault(ch, ("Customer is represented: contact goes through the representative.", "POL-COLL-001 §5.4"))
    if not flag(sc.get("consent_call")):
        blocked.setdefault("call", ("No consent to call.", "POL-COLL-001 §5.3"))
    if flag(sc.get("internal_do_not_call")):
        blocked.setdefault("call", ("Internal do-not-call flag.", "POL-COLL-001 §5.3"))
    if (contacts.get("outbound_calls_7d") or 0) >= CALL_CAP_7D:
        blocked.setdefault("call", (f"{contacts['outbound_calls_7d']} call attempts in the last 7 days; the cap is {CALL_CAP_7D}.", "POL-COLL-001 §5.2"))
    if not flag(sc.get("consent_sms")):
        blocked.setdefault("sms", ("No consent to text.", "POL-COLL-001 §5.3"))
    if not flag(sc.get("consent_email")):
        blocked.setdefault("email", ("No consent to email.", "POL-COLL-001 §5.3"))
    allowed = [ch for ch in ("call", "sms", "email", "letter") if ch not in blocked]
    return allowed, [{"channel": ch, "reason": r, "cite": c} for ch, (r, c) in blocked.items()]


def next_window(doc, after=None):
    """Next permitted contact window in the customer's local time (POL-COLL-001 §5.1)."""
    contact = doc.get("contact") or {}
    contacts = (doc.get("collections") or {}).get("contacts") or {}
    start, end = (contact.get("permitted_call_window_local") or ["07:00", "21:00"])[:2]
    pref = contact.get("preferred_contact_window") or [None, None]
    lo, hi = int((pref[0] or start)[:2]), int((pref[1] or end)[:2])
    lo, hi = max(lo, int(start[:2])), min(hi, int(end[:2]))
    best = contacts.get("most_common_rpc_hour")
    for m in doc.get("model_scores") or []:
        if m.get("model_name") == "best_time_to_contact" and m.get("slot_label"):
            best = best or _hour(m["slot_label"])
    if best is not None and lo <= best < hi:
        lo, hi = best, min(best + 2, hi)
    day = (after or config.SNAPSHOT_DATE) + dt.timedelta(days=1)
    notes = []
    while True:
        if day in STAT_HOLIDAYS:
            notes.append(f"{short_date(day)} skipped: statutory holiday.")
        elif day.weekday() == 6 and not flag(contact.get("sunday_contact_permitted")):
            notes.append(f"{short_date(day)} skipped: no Sunday contact.")
        else:
            break
        day += dt.timedelta(days=1)
    if day.weekday() == 6:
        lo, hi = max(lo, 13), min(hi, 17)
    return {"date": day.isoformat(), "window_local": f"{lo:02d}:00-{hi:02d}:00", "time_zone": contact.get("time_zone"),
            "notes": notes, "why": "best right-party hour" if best is not None else "customer's preferred window"}


def _hour(label):
    import re
    m = re.search(r"(\d{1,2})", str(label))
    return int(m.group(1)) if m else None


def recommend(doc, case_id=None):
    case = pick_case(doc, case_id)
    if case is None:
        return {"case_id": case_id, "action": {"code": "NO_ACTION", "label": "No case found"}, "reasons": [], "conflicts": []}
    sc = doc.get("servicing_constraints") or {}
    contact = doc.get("contact") or {}
    contacts = (doc.get("collections") or {}).get("contacts") or {}
    allowed, blocked = channels(doc, case)
    reasons, safeguards = [], []
    rec = {"code": None, "channel": None, "amount": None, "program": None, "script": "SCR-MID-STD-EN",
           "assign_to": None, "approver_level": "agent"}

    def r(text, cite=None):
        reasons.append({"text": text, "cite": cite})

    dpd = case.get("current_dpd") or 0
    overdue = case.get("total_overdue") or 0
    promises = case.get("promises") or {}
    latest = promises.get("latest") or {}
    plan_active = any(o.get("plan_status") == "active" for o in case.get("offers") or []) or flag(case.get("hardship_program_accepted"))
    hardship_stated = flag(case.get("hardship_flag")) or (doc.get("hardship_indicators") or {}).get("financial_hardship_flag")
    risk = promise_risk(latest)
    risky = bool(risk) and risk["band"] == "high"

    if case.get("outcome") is not None:
        rec["code"] = "NO_ACTION"
        r(f"Case closed: outcome {case['outcome']} on {short_date(case.get('outcome_date'))}.")
    elif flag(sc.get("deceased")) or flag(case.get("deceased_hold_flag")):
        rec.update(code="HOLD_DECEASED", assign_to="special_handling", approver_level="team_lead")
        r("Customer is recorded as deceased. Collection contact stops; the estate is handled by special handling.", "POL-COLL-004 §4.5")
    elif flag(sc.get("bankruptcy")) or flag(sc.get("consumer_proposal")) or flag(case.get("insolvency_hold_flag")):
        rec.update(code="HOLD_INSOLVENCY", assign_to="insolvency_hold", approver_level="team_lead")
        r("A bankruptcy or consumer proposal is on file. The law stops all collection contact while it is in place.", "POL-COLL-001 §7.3")
    elif flag(sc.get("cease_communication")) or flag(case.get("cease_contact_flag")):
        rec.update(code="LETTERS_ONLY", channel="letter")
        r("The customer asked in writing to stop communication. Only required letters and statements continue.", "POL-COLL-001 §5.4")
    elif any(flag(sc.get(k)) for k in ("credit_counselling_agency", "legal_representative")) or flag(case.get("third_party_rep_flag")):
        rec.update(code="CONTACT_REPRESENTATIVE", channel="call", assign_to="representative")
        r("A credit counsellor, lawyer or trustee represents the customer; all contact goes through them.", "POL-COLL-001 §5.4")
        if flag(sc.get("credit_counselling_agency")):
            best, _ = hardship.best_option(doc, case)
            rec["program"] = "HP-DMP"
            r("A debt management plan (HP-DMP) through the counsellor is the usual route.", "POL-COLL-004 §4.2")
    elif flag(case.get("dispute_flag")) and (case.get("dispute_status") or "open") not in ("resolved", "closed", "rejected"):
        rec.update(code="RESOLVE_DISPUTE", assign_to="disputes", channel=_first(allowed, ["call", "email", "letter"]))
        r(f"Dispute open ({case.get('dispute_type') or 'type not recorded'}). Investigate before collecting the disputed amount.", "PRC-COLL-012 §6.1")
    elif hardship_stated and not plan_active:
        best, ev = hardship.best_option(doc, case)
        rec.update(code="OFFER_HARDSHIP", channel=_first(allowed, ["call", "email", "sms", "letter"]))
        reason = case.get("hardship_reason")
        r(f"Hardship recorded{' (' + hardship.REASON_LABEL.get(reason, reason) + ')' if reason else ''} and no plan in place. "
          "Offer at least one eligible option before asking for payment.", "POL-COLL-004 §4.2")
        if best:
            rec["program"] = best["program_id"]
            rec["approver_level"] = best["approver_level"]
            est = best.get("estimate") or {}
            if est.get("kind") == "monthly":
                rec["amount"] = est["amount"]
                r(f"Best eligible option: {best['name']} ({best['program_id']}), about {money(est['amount'])}/month "
                  f"for {est.get('months')} month(s) instead of {money(est.get('regular'))}.", best["cite"])
            else:
                r(f"Best eligible option: {best['name']} ({best['program_id']}).", best["cite"])
            if best["requires_income_evidence"]:
                r("This programme needs income and expense evidence.", "POL-COLL-004 §4.3")
        else:
            r("No programme is eligible on the current data; refer to a hardship specialist.", "POL-COLL-004 §4.4")
            rec["assign_to"] = "hardship_specialist"
    elif plan_active and _plan_ends_within(case, 10):
        rec.update(code="PLAN_END_REVIEW", channel=_first(allowed, ["call", "email", "sms"]), assign_to="hardship_specialist")
        r(f"Hardship plan ends {short_date(case.get('hardship_plan_end'))}. Specialists contact the customer 10 days before the end.", "POL-COLL-004 §4.4")
    elif latest.get("status") == "open" and as_date(latest.get("due_date")) and as_date(latest["due_date"]) >= config.SNAPSHOT_DATE:
        prefs = ["call", "sms", "email"] if risky and not plan_active else ["sms", "email", "call"]
        rec.update(code="PTP_REMINDER", channel=_first(allowed, prefs), amount=latest.get("amount"))
        r(f"Promise of {money(latest.get('amount'))} due {short_date(latest.get('due_date'))}. Send a reminder before the due date with a payment link.", "PRC-COLL-010 §3.2")
        if risky and not plan_active and rec["channel"] == "call":
            r("The promise is likely to fail, so the reminder is a call: check the amount is affordable and, if not, agree a "
              "smaller amount or a plan rather than let it break.")
        if plan_active:
            r("Customer is on a hardship plan: no collection call.", "POL-COLL-004 §4.4")
    elif plan_active:
        rec.update(code="MONITOR_PLAN", channel=_first(allowed, ["sms", "email", "letter"]))
        r("Hardship plan active and no promise due: send the instalment reminder only.", "POL-COLL-004 §4.4")
    elif latest.get("status") == "open":
        rec.update(code="PTP_FOLLOW_UP", channel=_first(allowed, ["call", "sms", "email"]), amount=latest.get("amount"))
        r(f"Promise of {money(latest.get('amount'))} was due {short_date(latest.get('due_date'))}; the 2 working days of grace have passed.", "PRC-COLL-010 §3.3")
    elif (promises.get("broken") or 0) >= 2 and dpd < 120:
        rec.update(code="PAYMENT_PLAN", channel=_first(allowed, ["call", "email"]))
        r(f"{promises['broken']} broken promises on this case. A smaller, structured plan is more likely to hold than another one-off promise.")
        best, _ = hardship.best_option(doc, case)
        if best:
            rec["program"] = best["program_id"]
            r(f"Eligible: {best['name']} ({best['program_id']}).", best["cite"])
    elif dpd >= 120:
        settle = [o for o in hardship.evaluate(doc, case)["options"] if o["program_id"] == "HP-SETTLE" and o["eligible"]]
        rec.update(code="SETTLEMENT_REVIEW" if settle else "AGENT_CALL", channel=_first(allowed, ["call", "letter"]))
        r(f"{dpd} days past due; accounts are written off at 181 days.")
        if settle:
            rec["approver_level"] = "team_lead"
            est = settle[0].get("estimate") or {}
            r(f"Settlement (HP-SETTLE) in range {money(est.get('low'))}-{money(est.get('high'))}; team lead approval.", settle[0]["cite"])
    elif dpd <= 30:
        rec.update(code="DIGITAL_REMINDER", channel=_first(allowed, ["sms", "email", "call"]), amount=overdue)
        r(f"Early stage ({dpd} days past due, {money(overdue)} overdue): a reminder with a payment link is the least intrusive first step.")
    else:
        rec.update(code="AGENT_CALL", channel=_first(allowed, ["call", "sms", "email"]), amount=overdue)
        r(f"{dpd} days past due, {money(overdue)} overdue, no promise in place: an agent call to agree a payment.")

    if risk and rec["code"] not in CONTACT_ACTIONS:
        risk = None                 # holds, letters only, representatives: a break probability is not relevant advice
    if risk:
        r(f"Promise risk model (advice): {risk['summary']}")
        for text in risk.get("safeguards") or []:
            if not any(text == x["text"] for x in safeguards):
                safeguards.append({"text": text, "cite": None})

    # safeguards that apply whatever the action
    if flag(case.get("vulnerable_customer_flag")):
        rec["script"] = "SCR-VUL-EN"
        safeguards.append({"text": "Vulnerable customer: a certified agent, the vulnerable-customer script, extra time, no pressure. "
                                   "Vulnerability never lowers priority for help.", "cite": "POL-COLL-004 §4.5"})
        ag = case.get("assigned_agent") or {}
        if ag and not flag(ag.get("vulnerable_customer_certified")):
            rec["assign_to"] = rec["assign_to"] or "certified_agent"
            safeguards.append({"text": f"Assigned agent {ag.get('agent_id')} is not certified for vulnerable customers; reassign.",
                               "cite": "POL-COLL-004 §4.5"})
    if (contact.get("preferred_language") or "").upper().startswith("FR"):
        safeguards.append({"text": "French-speaking customer: contact in French (Montréal site).", "cite": "POL-COLL-001 §5.5"})
    if flag(sc.get("national_dncl_telemarketing_only")):
        safeguards.append({"text": "On the National Do Not Call List, which covers telemarketing only; collection calls are allowed.",
                           "cite": "POL-COLL-001 §5.3"})
    if rec["code"] in CONTACT_ACTIONS and rec["channel"] is None:
        rec["channel"] = "letter"
        r("No electronic or phone channel is allowed right now; use a letter.")

    timing = next_window(doc) if rec["channel"] in ("call", "sms") else None
    out = {
        "case_id": case["case_id"], "c360_id": doc.get("_id"),
        "action": {"code": rec["code"], "label": ACTIONS[rec["code"]]},
        "channel": rec["channel"], "timing": timing, "amount": rec["amount"], "program": rec["program"],
        "script": rec["script"], "assign_to": rec["assign_to"], "approver_level": rec["approver_level"],
        "reasons": reasons, "safeguards": safeguards, "allowed_channels": allowed, "blocked_channels": blocked,
        "context": {"dpd": dpd, "bucket": case.get("current_bucket"), "overdue": overdue, "queue": case.get("queue"),
                    "promises": {k: promises.get(k) for k in ("total", "kept", "broken", "open")},
                    "outbound_calls_7d": contacts.get("outbound_calls_7d"), "hardship_stated": bool(hardship_stated),
                    "plan_active": plan_active},
        "promise_risk": risk,
        "engine": ENGINE_VERSION, "snapshot_date": config.SNAPSHOT_DATE.isoformat(),
    }
    out["conflicts"] = check(out, doc)
    return out


def check(rec, doc):
    """Conflicts between a (possibly edited) recommendation and the customer's record and policy.
    Approve is not allowed while this list is non-empty."""
    case = pick_case(doc, rec.get("case_id")) or {}
    allowed, blocked = channels(doc, case)
    code = (rec.get("action") or {}).get("code")
    ch = rec.get("channel")
    out = []
    if ch and ch not in allowed:
        b = next((x for x in blocked if x["channel"] == ch), None)
        out.append({"rule": "channel_blocked", "text": f"{ch.upper()} isn't allowed: {b['reason'] if b else 'not permitted'}",
                    "cite": b["cite"] if b else None})
    holds = {"HOLD_INSOLVENCY", "HOLD_DECEASED", "LETTERS_ONLY"}
    sc = doc.get("servicing_constraints") or {}
    if code in CONTACT_ACTIONS and ch in ("call", "sms", "email") and (
            flag(sc.get("bankruptcy")) or flag(sc.get("consumer_proposal")) or flag(case.get("insolvency_hold_flag"))):
        out.append({"rule": "insolvency_stay", "text": "Collection contact during a stay of proceedings.", "cite": "POL-COLL-001 §7.3"})
    hardship_stated = flag(case.get("hardship_flag"))
    plan_active = any(o.get("plan_status") == "active" for o in case.get("offers") or []) or flag(case.get("hardship_program_accepted"))
    if code in PAYMENT_ASKS and hardship_stated and not plan_active:
        out.append({"rule": "hardship_first", "text": "Hardship is recorded: offer a hardship option before asking for payment.",
                    "cite": "POL-COLL-004 §4.2"})
    t = rec.get("timing") or {}
    if t.get("date"):
        d = as_date(t["date"])
        if d in STAT_HOLIDAYS:
            out.append({"rule": "stat_holiday", "text": f"{short_date(d)} is a statutory holiday: no collection contact.", "cite": "POL-COLL-001 §5.1"})
        w = t.get("window_local") or ""
        try:
            lo, hi = int(w[:2]), int(w[6:8])
            if lo < 7 or hi > 21:
                out.append({"rule": "permitted_hours", "text": f"Window {w} is outside 07:00-21:00 local time.", "cite": "POL-COLL-001 §5.1"})
        except ValueError:
            pass
    pd = rec.get("promise_date")
    if pd and as_date(pd) and (as_date(pd) - config.SNAPSHOT_DATE).days > 21:
        out.append({"rule": "promise_window", "text": "A promise date should be within 21 days.", "cite": "PRC-COLL-010 §3.1"})
    if code not in holds and code in CONTACT_ACTIONS and rec.get("amount") and case.get("total_exposure") and rec["amount"] > case["total_exposure"] * 1.01:
        out.append({"rule": "amount", "text": "Amount is more than the total balance.", "cite": None})
    return out


def _first(allowed, prefs):
    for p in prefs:
        if p in allowed:
            return p
    return None


def _plan_ends_within(case, days):
    end = as_date(case.get("hardship_plan_end"))
    return bool(end) and 0 <= (end - config.SNAPSHOT_DATE).days <= days
