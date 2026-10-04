"""Hardship programme eligibility (POL-COLL-004 §4.2-4.3, HPT-2026, data/hardship_programs.csv).

Used by the next-best-action engine (agent side) and by the customer app (self-service). Every
decision carries its reasons, so an agent or a customer can see why a programme is or isn't offered.
Estimated payments are estimates; the plan is confirmed by a person (human in the loop).
"""
import csv
import datetime as dt
import functools
import json

from c360.layer3 import config
from c360.layer3.util import as_date, flag

LEVELS = {"none": 0, "possible": 1, "clear": 2}
PLAIN = {
    "HP-CARD-MIN": "Pay half your minimum payment for 3 months. Fees are waived.",
    "HP-CARD-RATE": "Your purchase interest rate drops to 9.99% for 6 months.",
    "HP-CARD-SKIP": "Skip one minimum payment (once in 12 months).",
    "HP-CARD-FPP": "A fixed monthly payment for 12 months at 0% interest. The card is paused while you're on the plan.",
    "HP-LOAN-RED3": "Pay 50% of your regular payment for 3 months. Interest keeps adding up, and the missed amount moves to the end of your loan.",
    "HP-LOAN-SKIP": "Move one payment to the end of your loan (once in 12 months).",
    "HP-LOAN-EXT": "Stretch your loan by up to 12 months so each payment is smaller.",
    "HP-OD-REPAY": "Repay your overdraft over 6 months. Fees are waived.",
    "HP-DMP": "A debt management plan arranged through a non-profit credit counsellor.",
    "HP-SETTLE": "A one-time settlement of part of the balance; the rest is written off.",
}
REASON_LABEL = {
    "reduced_income": "reduced hours or pay", "reduced_hours": "reduced hours or pay", "job_loss": "job loss",
    "illness": "illness", "disability": "disability", "separation": "separation", "bereavement": "bereavement",
    "ei_waiting": "waiting for Employment Insurance", "strike": "a strike or lockout",
    "disaster_evacuation": "a natural disaster or evacuation", "rate_reset": "a payment increase after a rate reset",
}


@functools.lru_cache(maxsize=1)
def programmes(on=None):
    """Programme versions in force on the snapshot date."""
    on = on or config.SNAPSHOT_DATE
    path = config.maple_data() / "data" / "hardship_programs.csv"
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if as_date(r["effective_from"]) <= on <= (as_date(r["effective_to"]) or dt.date.max):
                r["product_types"] = json.loads(r["product_types"] or "[]")
                for k in ("max_dpd", "min_months_on_book", "max_uses_12m", "duration_months"):
                    r[k] = int(r[k]) if r[k] not in ("", None) else None
                for k in ("payment_reduction_pct",):
                    r[k] = float(r[k]) if r[k] not in ("", None) else None
                for k in ("excludes_insolvency", "excludes_broken_plan_90d", "requires_income_evidence", "fees_waived_flag"):
                    r[k] = flag(r[k])
                out.append(r)
    return out


def hardship_level(case, doc=None, stated_reason=None):
    if stated_reason or flag(case.get("hardship_flag")):
        return "clear"
    if doc and ((doc.get("hardship_indicators") or {}).get("financial_hardship_flag")):
        return "possible"
    return "none"


def _primary_account(doc, case):
    accts = (doc.get("accounts") or {})
    pid = case.get("primary_account_id")
    for group in ("cards", "loans", "deposits"):
        for a in accts.get(group) or []:
            if a.get("account_id") == pid:
                return group, a
    return None, None


def regular_payment(doc, case):
    group, a = _primary_account(doc, case)
    if not a:
        return None
    if group == "cards":
        return a.get("minimum_payment_due")
    if group == "loans":
        return a.get("scheduled_payment_amount") or a.get("next_due_amount")
    return round((a.get("overdraft_used") or 0) / 6, 2) or None


def _months_on_book(doc, case):
    group, a = _primary_account(doc, case)
    if not a:
        return None
    if a.get("months_on_book") is not None:
        return a["months_on_book"]
    start = as_date(a.get("origination_date") or a.get("open_date"))
    if start:
        snap = config.SNAPSHOT_DATE
        return (snap.year - start.year) * 12 + snap.month - start.month
    return None


def _estimate(p, base, case):
    pid = p["program_id"]
    if pid == "HP-SETTLE":
        bal = case.get("total_exposure") or case.get("total_overdue")
        return {"kind": "one_time", "low": round(bal * 0.4, 2), "high": round(bal * 0.7, 2)} if bal else None
    if base is None:
        return None
    if pid in ("HP-CARD-SKIP", "HP-LOAN-SKIP"):
        return {"kind": "monthly", "amount": 0.0, "months": 1, "regular": base}
    if pid == "HP-CARD-RATE":
        return {"kind": "rate", "apr": 0.0999, "months": 6, "regular": base}
    pct = p.get("payment_reduction_pct")
    if pct is None:
        return None
    return {"kind": "monthly", "amount": round(base * (1 - pct), 2), "months": p.get("duration_months"), "regular": base}


def evaluate(doc, case, stated_reason=None):
    """Every programme in force, with eligible True/False and the reasons."""
    sc = doc.get("servicing_constraints") or {}
    product = case.get("primary_product")
    dpd = case.get("current_dpd") or 0
    level = hardship_level(case, doc, stated_reason)
    mob = _months_on_book(doc, case)
    insolvent = flag(sc.get("bankruptcy")) or flag(sc.get("consumer_proposal")) or flag(case.get("insolvency_hold_flag"))
    snap = config.SNAPSHOT_DATE
    offers = case.get("offers") or []
    base = regular_payment(doc, case)
    out = []
    for p in programmes():
        why_not = []
        if product not in p["product_types"]:
            continue
        if p["excludes_insolvency"] and insolvent:
            why_not.append("Not available during a bankruptcy or consumer proposal (POL-COLL-004 §4.2).")
        if LEVELS.get(level, 0) < LEVELS.get(p["min_hardship_level"], 0):
            why_not.append(f"Needs a {p['min_hardship_level']} hardship to be recorded first (currently {level}).")
        if p["max_dpd"] is not None and dpd > p["max_dpd"]:
            why_not.append(f"Account is {dpd} days past due; this programme allows up to {p['max_dpd']}.")
        if mob is not None and p["min_months_on_book"] and mob < p["min_months_on_book"]:
            why_not.append(f"Account open {mob} months; needs {p['min_months_on_book']}.")
        recent = [o for o in offers if o.get("program_id") == p["program_id"] and o.get("customer_response") == "accepted"
                  and (snap - (as_date(o.get("offer_ts")) or dt.date.min)).days <= 365]
        active = [o for o in recent if o.get("plan_status") == "active"]
        if active:
            why_not.append("Already on this plan.")
        elif p["max_uses_12m"] is not None and len(recent) >= p["max_uses_12m"]:
            why_not.append(f"Used {len(recent)} time(s) in the last 12 months; limit {p['max_uses_12m']}.")
        if p["excludes_broken_plan_90d"] and any(
                o.get("plan_status") in ("broken", "cancelled", "defaulted")
                and (snap - (as_date(o.get("offer_ts")) or dt.date.min)).days <= 90 for o in offers):
            why_not.append("A hardship plan was broken in the last 90 days.")
        out.append({
            "program_id": p["program_id"], "name": p["program_name"], "version": p["version"],
            "description": p["description"], "plain": PLAIN.get(p["program_id"], p["description"]),
            "eligible": not why_not, "why_not": why_not, "approver_level": p["approver_level"],
            "requires_income_evidence": p["requires_income_evidence"], "duration_months": p["duration_months"],
            "estimate": _estimate(p, base, case), "cite": f"POL-COLL-004 {p['policy_section']}, HPT-2026",
        })
    # agents can approve these on the call; then specialist; settlement last
    rank = {"agent": 0, "hardship_specialist": 1, "team_lead": 2}
    out.sort(key=lambda r: (not r["eligible"], rank.get(r["approver_level"], 3), r["program_id"] == "HP-SETTLE"))
    return {"hardship_level": level, "regular_payment": base, "months_on_book": mob, "options": out}


def best_option(doc, case, stated_reason=None):
    ev = evaluate(doc, case, stated_reason)
    elig = [o for o in ev["options"] if o["eligible"] and o["program_id"] != "HP-SETTLE"]
    return (elig[0] if elig else None), ev
