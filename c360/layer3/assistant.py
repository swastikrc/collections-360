"""Customer assistant: plain-English help about the customer's own account and options.

Deterministic intents over the customer's own record and the bank's current policies, so it works
without a model key and never invents numbers: every amount comes from the record, every rule from a
cited policy section. It only ever sees the signed-in customer's own document.

Guardrails:
  - hardship first: when a customer says they can't pay, it offers options and never asks for payment
    in the same reply (POL-COLL-004 §4.2);
  - requests that affect the account (hardship plan, stop contact, dispute) are recorded as pending
    for a person to action, never auto-approved;
  - out-of-scope questions get a short, polite redirect;
  - it never shows internal notes, scores, other customers or agent-only fields.
"""
import re

from c360.layer3 import hardship
from c360.layer3.nba import pick_case
from c360.layer3.policy import library
from c360.layer3.util import money, short_date

INTENTS = [
    ("insolvency", r"bankrupt|consumer proposal|insolvency|trustee|faillite"),
    ("hardship", r"lost my job|laid off|hours (were |got )?(cut|reduced)|reduced hours|sick|illness|hospital|separat|divorc|"
                 r"can't (afford|pay)|cannot (afford|pay)|unable to pay|struggling|hardship|tight|ei\b|employment insurance|"
                 r"evacuat|fire|flood|disabilit|passed away|lower (my )?payment|reduce (my )?payment|payment plan|help paying"),
    ("cease", r"stop (calling|contacting|texting|emailing)|don't (call|contact)|too many calls|harass|leave me alone"),
    ("dispute", r"dispute|don't owe|not my (charge|debt)|wrong (amount|balance)|late fee|already paid|fraud|charged twice"),
    ("promise", r"promise|pay (later|next|on|by)|payday|can i pay on|set (up )?a date|arrangement"),
    ("pay", r"pay (now|today)|make a payment|pay (it|the balance|in full)|how (do|can) i pay"),
    ("balance", r"how much|what do i owe|balance|owe|past due|overdue|due date|minimum"),
    ("human", r"(talk|speak) (to|with) (a |an )?(person|human|agent|someone)|call me back|callback|representative"),
    ("greeting", r"^(hi|hello|hey|bonjour|good (morning|afternoon|evening))\b"),
]
OFF_TOPIC = r"weather|sports|stock|bitcoin|recipe|movie|joke|news|election"


def _intent(msg):
    m = msg.lower()
    for name, pat in INTENTS:
        if re.search(pat, m):
            return name
    if re.search(OFF_TOPIC, m):
        return "off_topic"
    return "policy"


def _stated_reason(msg):
    m = msg.lower()
    for label, pat in (("job_loss", r"lost my job|laid off|unemployed"), ("reduced_income", r"hours|shifts|pay (was )?cut"),
                       ("illness", r"sick|illness|hospital|surgery"), ("separation", r"separat|divorc"),
                       ("ei_waiting", r"\bei\b|employment insurance"), ("disaster_evacuation", r"evacuat|fire|flood"),
                       ("disability", r"disabilit"), ("bereavement", r"passed away|died|funeral")):
        if re.search(pat, m):
            return label
    return "other"


def summary_for_customer(doc, case):
    accts = doc.get("accounts") or {}
    p = (case.get("promises") or {})
    latest = p.get("latest") or {}
    plan = next((o for o in case.get("offers") or [] if o.get("plan_status") == "active"), None)
    return {
        "case_id": case.get("case_id"), "product": case.get("primary_product"),
        "overdue": case.get("total_overdue"), "balance": case.get("total_exposure"),
        "days_past_due": case.get("current_dpd"),
        "next_promise": latest if latest.get("status") == "open" else None,
        "active_plan": plan, "accounts": [a.get("account_id") for g in ("cards", "loans", "deposits") for a in accts.get(g) or []],
    }


def reply(doc, message, case_id=None):
    message = (message or "").strip()[:1000]
    case = pick_case(doc, case_id) or {}
    first = (doc.get("profile") or {}).get("preferred_name") or (doc.get("profile") or {}).get("first_name") or "there"
    intent = _intent(message) if message else "greeting"
    s = summary_for_customer(doc, case)
    out = {"intent": intent, "reply": "", "cards": [], "citations": [], "signals": []}

    if intent == "greeting":
        out["reply"] = (f"Hi {first}. I can help you understand what you owe, set up a payment date, look at options if "
                        f"money is tight, or change how we contact you. What would you like to do?")
        out["cards"] = [{"type": "quick_replies", "items": ["What do I owe?", "I can't pay right now",
                                                            "Set a payment date", "Stop calling me"]}]
    elif intent == "balance":
        parts = [f"Your account is {s['days_past_due']} days past due, with {money(s['overdue'])} overdue."]
        if s["next_promise"]:
            parts.append(f"You've promised {money(s['next_promise'].get('amount'))} by {short_date(s['next_promise'].get('due_date'))}.")
        if s["active_plan"]:
            name = next((p["program_name"] for p in hardship.programmes() if p["program_id"] == s["active_plan"].get("program_id")), "payment plan")
            parts.append(f"You're on our {name.lower()} at {money(s['active_plan'].get('proposed_payment_amount'))} a month.")
        parts.append("If that amount is hard to manage right now, tell me and I'll show you the options.")
        out["reply"] = " ".join(parts)
        out["cards"] = [{"type": "actions", "items": ["pay", "promise", "options"]}]
    elif intent == "hardship":
        reason = _stated_reason(message)
        ev = hardship.evaluate(doc, case, stated_reason=reason)
        elig = [o for o in ev["options"] if o["eligible"] and o["program_id"] != "HP-SETTLE"]
        label = hardship.REASON_LABEL.get(reason)
        out["reply"] = ((f"I'm sorry you're dealing with {label}. " if label else "Thanks for telling me. ")
                        + ("Here are the options that fit your account. Pick one and we'll send it to our hardship team; "
                           "a person reviews it and confirms with you, usually within 2 business days."
                           if elig else "I'll pass this to our hardship team so a person can look at what we can do for you."))
        out["cards"] = [{"type": "hardship_options", "reason": reason, "options": elig[:4]}]
        out["citations"] = ["POL-COLL-004 §4.2"]
        out["signals"] = [{"type": "hardship_mentioned", "reason": reason}]
    elif intent == "promise":
        out["reply"] = ("You can choose a date within the next 21 days, ideally on or after your next payday. We'll send a "
                        "reminder before the date. A payment counts as on time if it arrives within 2 business days of the "
                        "date you choose.")
        out["cards"] = [{"type": "promise_form", "suggested_amount": s["overdue"], "max_days": 21}]
        out["citations"] = ["PRC-COLL-010 §3.1", "PRC-COLL-010 §3.3"]
    elif intent == "pay":
        out["reply"] = f"You can pay any amount now. Paying {money(s['overdue'])} brings the account up to date."
        out["cards"] = [{"type": "pay_form", "suggested_amount": s["overdue"]}]
    elif intent == "cease":
        out["reply"] = ("You can choose how we contact you, or ask us in writing to stop calls, texts and emails. If you ask us to "
                        "stop, we'll still send letters and statements we're required to send, and the balance stays owing. "
                        "A person reviews the request and confirms it.")
        out["cards"] = [{"type": "contact_form"}]
        out["citations"] = ["POL-COLL-001 §5.4"]
    elif intent == "dispute":
        sec = library().search("dispute handling procedure investigate", k=1, doc_types={"procedure"})
        out["reply"] = ("Tell me what looks wrong and we'll investigate. While a dispute is open, we won't ask you to pay the "
                        "amount you're disputing.")
        if re.search(r"late fee", message.lower()):
            out["reply"] += (" If you were charged a late fee after an app or online payment around 14 September, that was our "
                             "error and the fee is being reversed on your next statement.")
            out["citations"].append("IR-2026-0914")
        out["cards"] = [{"type": "dispute_form"}]
        out["citations"] += [s_.cite for s_ in sec]
    elif intent == "insolvency":
        out["reply"] = ("If you've filed for bankruptcy or a consumer proposal, collection contact stops from the filing date. "
                        "Please share your Licensed Insolvency Trustee's name and the filing date, and a person will update "
                        "your file.")
        out["cards"] = [{"type": "callback_form", "topic": "insolvency"}]
        out["citations"] = ["POL-COLL-001 §7.3"]
    elif intent == "human":
        out["reply"] = "I can ask someone from our team to call you back. Choose a time that suits you."
        out["cards"] = [{"type": "callback_form", "topic": "general"}]
    elif intent == "off_topic":
        out["reply"] = "I can only help with your Maple Bank account and payment options. Is there something about your account I can help with?"
    else:
        hits = library().search(message, k=2)
        if hits:
            h = hits[0]
            text = re.sub(r"\s+", " ", re.sub(r"[*`#|]", "", h.text)).strip()
            out["reply"] = f"Here's what our {h.title} says: {text[:420]}{'…' if len(text) > 420 else ''}"
            out["citations"] = [h.cite]
        else:
            out["reply"] = "I'm not sure I understood. I can help with what you owe, payment dates, options if money is tight, or contact preferences."
            out["cards"] = [{"type": "quick_replies", "items": ["What do I owe?", "I can't pay right now", "Talk to a person"]}]
    return out
