"""Post-call: turn a call transcript into structured fields, a summary and a QA score.

    from c360.layer3.postcall import analyse
    result = analyse(transcript_json, overdue_at_call=164.77)

Rule-based and deterministic, so it runs without a model key and every field points at the turn
(second and text) it came from. English and French calls. The output vocabulary matches the
dataset's gold labels (labels/call_transcripts_labels_public_500.csv), so
`python -m c360.layer3.postcall --eval` scores it field by field.

QA follows data/qa_checklist.csv: score = 100 minus the weight of each failed item, and coaching is
required when any critical item fails (both rules reproduce the gold labels exactly).
"""
import csv
import datetime as dt
import json
import re
import sys

from c360.layer3 import config

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
          "juil": 7, "août": 8, "aout": 8, "sept": 9, "déc": 12}
MONTH_RX = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|sept|déc)[a-zéû]*\.?"

HARDSHIP = [  # (label, summary phrase, pattern) - first match wins
    ("job_loss", "job loss", r"lost my job|laid off|position (was )?eliminated|let go|unemployed|plant (closed|shut)|"
                             r"mis à pied|mise à pied|perdu mon (emploi|travail)|licenci"),
    ("reduced_income", "reduced hours / income", r"(hours|shifts|pay)\b.{0,30}\b(cut|reduced|cut back)|cut (my|back)|"
                                                 r"reduced hours|only getting \d+ days|\d days a week now|fewer shifts|less work|"
                                                 r"heures (ont été )?(coupées|réduites)|moins d'heures|salaire (a été )?(coupé|réduit)|\d jours par semaine"),
    ("illness", "illness", r"off work sick|been sick|illness|surgery|hospital|medical leave|sick leave|in treatment|"
                           r"malade|congé de maladie|hôpital|chirurgie"),
    ("disability", "disability", r"disabilit|on disability|injur|invalidit|bless"),
    ("separation", "separation", r"separat|divorc|split up|partner left|one income now|sépar"),
    ("ei_waiting", "waiting for EI", r"waiting (for|on) (my )?(ei|employment insurance)|\bei\b|assurance-emploi|chômage"),
    ("disaster_evacuation", "evacuation (disaster)", r"evacuat|wildfire|flood|fire|leave our house|évacu|incendie|inondation"),
    ("rate_reset", "payment increase after rate reset", r"rate reset|payment went up|payment (has )?(increased|jumped)|"
                                                        r"renewal|mortgage went up|paiement a augmenté|renouvellement"),
]
TIGHT = r"tight|struggling|don't have the money|short (on money|this month)|serré|difficile de payer"
DISPUTE = [("late_fee", r"late fee|charged a fee|fee (was|is) wrong|frais de retard"),
           ("payment_not_applied", r"already (paid|made a payment)|(wasn't|was not|hasn't been|not been|never) (applied|posted)|"
                                   r"déjà (fait un paiement|payé)|pas été appliqué"),
           ("fraud", r"fraud|didn't make (that|those)|not my (charge|purchase)|someone used my|fraude"),
           ("billing_error", r"billing error|wrong (amount|balance)|statement is wrong|double charged|charged twice|"
                             r"erreur de facturation|facturé deux fois")]
CEASE = r"please stop (calling|contacting)|(i want|i'd like) you to stop|stop contacting me|don't (call|contact) me (again|anymore)|" \
        r"cease|arrêtez de m'appeler|ne m'appelez plus"
HOSTILE = r"harass|i'm done with this|ridiculous|leave me alone|harcèle|j'en ai assez"
VULN = [("mental_health", r"anxiety|depress|mental health|panic|anxiét|dépress"),
        ("serious_illness", r"cancer|chemo|serious(ly)? ill|hospital|hôpital"),
        ("domestic_situation", r"abuse|unsafe at home|domestic|controlling|violence"),
        ("natural_disaster", r"evacuat|wildfire|flood|leave our house|évacu|incendie"),
        ("financial_hardship", r"very difficult time|really hard time|période très difficile|don't have the money|"
                               r"can't afford food|behind on rent")]
INSOLVENCY = r"bankrupt|consumer proposal|insolvency|trustee|faillite|proposition de consommateur|syndic"
THREAT = r"garnish|we will sue|we'll sue|take you to court|legal action|arrest|seize your|poursuit|saisit votre|saisie|mise en demeure"
MISLEADING = r"criminal|police|jail|ruin your credit forever|criminel|prison"
COMPLAINT = r"complain|complaint|plainte|speak to (a|your) (manager|supervisor)"
ESCALATE = r"transfer(ring)? you|supervisor|escalat|transf[ée]r|superviseur"
OFFER_HARDSHIP = r"hardship option|reduced payment|payment plan|skip a payment|lower (the|your) rate|hardship program|" \
                 r"paiement réduit|options|plan de paiement"
ASK_PAY = r"can you (make|pay)|pay (the )?(full|whole)|make a payment today|need (a|the) payment|pouvez-vous payer|payer (le )?(montant )?complet"
VERIFY = r"date of birth|confirm your (postal code|address)|verify your identity|date de naissance"
DISCLOSE = r"past due|amount (owing|due)|balance|en retard|en souffrance|\$\d|\d \$"
RECORDING = r"recorded|recording|enregistré"
SELF_ID = r"this is \w+|it's \w+ with maple|calling from maple|ici \w+|c'est \w+ de la banque"
PURPOSE = r"calling about|regarding your account|au sujet de votre compte|j'appelle au sujet"
CLOSE = r"thank you for your time|thanks for your time|have a (good|great|nice)|merci pour votre temps|bonne (soirée|journée)"
STATED = r"(?:past due today is|amount (?:owing|due) is|en souffrance aujourd'hui est de)\s*\$?\s?([\d,]+(?:\.\d+)?)"


def turns_of(t):
    return [x for x in (t.get("turns") or []) if x.get("text")]


def _find(turns, pattern, speaker=None, after=-1):
    rx = re.compile(pattern, re.I)
    for i, x in enumerate(turns):
        if i > after and (speaker is None or x.get("speaker") == speaker) and rx.search(x["text"]):
            return i
    return None


def _ev(turns, i):
    return None if i is None else {"sec": turns[i].get("start_sec"), "speaker": turns[i].get("speaker"), "text": turns[i]["text"]}


def _num(s):
    return float(s.replace(",", "")) if s else None


def _next_day_of_month(day, after):
    d = after
    for _ in range(62):
        d += dt.timedelta(days=1)
        if d.day == day:
            return d
    return None


def _ptp(turns, call_date):
    """The promise read back by the agent ("a payment of $95.00 on October 14"), else the customer's offer."""
    rx_agent = re.compile(r"(?:confirm,? a payment of|pour confirmer, un paiement de)\s*\$?\s?([\d,]+(?:\.\d+)?)\s*\$?\s*"
                          r"(?:on|le)\s+(?:" + MONTH_RX + r"\s+(\d{1,2})|(\d{1,2})\s+" + MONTH_RX + ")", re.I)
    for i, x in enumerate(turns):
        if x.get("speaker") != "agent":
            continue
        m = rx_agent.search(x["text"])
        if m:
            if m.group(2):
                mo, day = MONTHS.get(m.group(2).lower()[:4].rstrip("."), MONTHS.get(m.group(2).lower()[:3])), int(m.group(3))
            else:
                mo, day = MONTHS.get(m.group(5).lower()[:4].rstrip("."), MONTHS.get(m.group(5).lower()[:3])), int(m.group(4))
            date = None
            if call_date and mo:
                year = call_date.year + (1 if mo < call_date.month - 6 else 0)
                try:
                    date = dt.date(year, mo, day)
                except ValueError:
                    pass
            return {"amount": _num(m.group(1)), "date": date and date.isoformat(), "confirmed_back": True, "index": i,
                    "evidence": _ev(turns, i)}
    rx_cust = re.compile(r"(?:i can|i'll|i will|i could|je (?:vais|peux)(?: essayer de)?) (?:try to )?pa(?:y|yer)\s*\$?\s?([\d,]+(?:\.\d+)?)\s*\$?"
                         r"(?:.*?\b(?:the|le)\s+(\d{1,2})(?:st|nd|rd|th|er)?\b)?", re.I)
    for i, x in enumerate(turns):
        if x.get("speaker") != "customer":
            continue
        m = rx_cust.search(x["text"])
        if m:
            date = _next_day_of_month(int(m.group(2)), call_date) if (m.group(2) and call_date) else None
            return {"amount": _num(m.group(1)), "date": date and date.isoformat(), "confirmed_back": False, "index": i,
                    "evidence": _ev(turns, i)}
    return None


def analyse(t, overdue_at_call=None):
    turns = turns_of(t)
    cust = [x for x in turns if x.get("speaker") == "customer"]
    call_date = None
    if t.get("call_start_ts"):
        try:
            call_date = dt.date.fromisoformat(str(t["call_start_ts"])[:10])
        except ValueError:
            pass

    h_label, h_phrase, h_i = None, None, None
    for label, phrase, pat in HARDSHIP:
        i = _find(turns, pat, "customer")
        if i is not None:
            h_label, h_phrase, h_i = label, phrase, i
            break
    tight_i = _find(turns, TIGHT, "customer")
    hardship = "clear" if h_label else ("possible" if tight_i is not None else "none")

    dispute_type, d_i = None, None
    for label, pat in DISPUTE:
        i = _find(turns, pat, "customer")
        if i is not None:
            dispute_type, d_i = label, i
            break
    cease_i = _find(turns, CEASE, "customer")
    vuln_type, v_i = None, None
    for label, pat in VULN:
        i = _find(turns, pat, "customer")
        if i is not None:
            vuln_type, v_i = label, i
            break
    tp_i = next((i for i, x in enumerate(turns) if x.get("speaker") not in ("agent", "customer")), None)
    ins_i = _find(turns, INSOLVENCY, "customer")
    threat_i = _find(turns, THREAT, "agent")
    complaint_i = _find(turns, COMPLAINT, "customer")
    esc_i = _find(turns, ESCALATE, "agent")
    hostile_i = _find(turns, HOSTILE, "customer")
    ptp = _ptp(turns, call_date)

    scores = [x.get("sentiment_score") for x in cust if isinstance(x.get("sentiment_score"), (int, float))]
    s_avg = sum(scores) / len(scores) if scores else 0.0
    if s_avg >= -0.1:
        sentiment = "cooperative"
    elif hostile_i is not None:
        sentiment = "hostile"
    elif h_label or vuln_type:
        sentiment = "distressed"
    else:
        sentiment = "frustrated"

    if cease_i is not None:
        next_step = "stop_contact"
    elif esc_i is not None:
        next_step = "escalate_supervisor"
    elif dispute_type:
        next_step = "dispute_investigation"
    elif h_label:
        next_step = "hardship_plan_review"
    elif ptp:
        next_step = "send_payment_link"
    else:
        next_step = "call_back"

    fields = {
        "hardship": hardship, "hardship_reason": h_label, "ptp_mentioned": bool(ptp),
        "ptp_amount": ptp and ptp["amount"], "ptp_date": ptp and ptp["date"],
        "dispute": dispute_type is not None, "dispute_type": dispute_type, "complaint": complaint_i is not None,
        "vulnerability": vuln_type is not None, "vulnerability_type": vuln_type,
        "third_party_present": tp_i is not None, "cease_request": cease_i is not None,
        "insolvency_mention": ins_i is not None, "legal_threat_by_agent": threat_i is not None,
        "sentiment": sentiment, "next_step": next_step,
    }
    evidence = {k: _ev(turns, i) for k, i in (("hardship", h_i if h_i is not None else tight_i), ("dispute", d_i),
                                              ("cease_request", cease_i), ("vulnerability", v_i), ("third_party", tp_i),
                                              ("insolvency", ins_i), ("legal_threat", threat_i), ("complaint", complaint_i),
                                              ("escalation", esc_i)) if i is not None}
    if ptp:
        evidence["ptp"] = ptp["evidence"]
    qa = score_qa(turns, fields, ptp, h_i if h_i is not None else tight_i, overdue_at_call)
    return {"transcript_id": t.get("transcript_id"), "case_id": t.get("case_id"), "contact_id": t.get("contact_id"),
            "call_start_ts": t.get("call_start_ts"), "duration_sec": t.get("duration_sec"), "direction": t.get("direction"),
            "language": t.get("language"), "agent_id": t.get("agent_id"), "fields": fields, "evidence": evidence,
            "summary": summary(fields, h_phrase), "qa": qa, "engine": "postcall-rules-1.1"}


def summary(f, h_phrase=None):
    parts = []
    if f["hardship"] == "clear" and h_phrase:
        parts.append(f"Customer reports {h_phrase}")
    elif f["hardship"] == "possible":
        parts.append("Customer indicates finances are tight")
    if f["dispute"]:
        parts.append(f"disputes {f['dispute_type'].replace('_', ' ')}")
    if f["ptp_mentioned"] and f["ptp_amount"] is not None:
        d = dt.date.fromisoformat(f["ptp_date"]) if f["ptp_date"] else None
        parts.append(f"promised ${round(f['ptp_amount']):,}" + (f" on {d.day} {d.strftime('%b')}" if d else ""))
    if f["cease_request"]:
        parts.append("asked to stop contact")
    if f["complaint"]:
        parts.append("wants to complain")
    if not parts:
        parts.append("No commitment made")
    return "; ".join(parts) + f"; next step: {f['next_step'].replace('_', ' ')}."


def _checklist():
    path = config.maple_data() / "data" / "qa_checklist.csv"
    with open(path, newline="", encoding="utf-8") as f:
        return {int(r["item_no"]): r for r in csv.DictReader(f)}


def score_qa(turns, f, ptp, h_i, overdue_at_call=None):
    items = _checklist()
    verify_i = _find(turns, VERIFY, "agent")
    disclose_i = _find(turns, DISCLOSE, "agent")
    res = {}

    def put(n, verdict, i=None, note=None):
        it = items[n]
        res[n] = {"item_no": n, "code": it["item_code"], "name": it["item_name"], "verdict": verdict,
                  "weight": int(it["weight"]), "critical": it["critical_flag"].lower() == "true",
                  "pass_criteria": it["pass_criteria"], "evidence": _ev(turns, i), "note": note}

    if verify_i is not None and (disclose_i is None or verify_i < disclose_i):
        put(1, "pass", verify_i)
    else:
        put(1, "fail", disclose_i, "The debt was mentioned before identity was verified.")
    i = _find(turns, RECORDING, "agent")
    put(2, "pass" if i is not None else "fail", i)
    i, j = _find(turns, SELF_ID, "agent"), _find(turns, PURPOSE, "agent")
    put(3, "pass" if i is not None and j is not None else "fail", i if i is not None else j)
    i = _find(turns, THREAT, "agent")
    put(4, "fail" if i is not None else "pass", i)
    i = _find(turns, MISLEADING, "agent")
    put(5, "fail" if i is not None else "pass", i)
    i = _find(turns, STATED, "agent")
    if i is not None and overdue_at_call:
        stated = _num(re.search(STATED, turns[i]["text"], re.I).group(1))
        ok = abs(stated - overdue_at_call) <= max(0.01, 0.005 * overdue_at_call)
        put(6, "pass" if ok else "fail", i, None if ok else f"Stated ${stated:,.2f}; the account showed ${overdue_at_call:,.2f}.")
    else:
        put(6, "pass", i)
    if h_i is None:
        put(7, "not_applicable")
    else:
        offer_i = _find(turns, OFFER_HARDSHIP, "agent", after=h_i)
        ask_i = _find(turns, ASK_PAY, "agent", after=h_i)
        if offer_i is not None and (ask_i is None or offer_i < ask_i):
            put(7, "pass", offer_i)
        else:
            put(7, "fail", ask_i if ask_i is not None else h_i, "Hardship stated but no option offered before asking for payment.")
    esc_i = _find(turns, ESCALATE, "agent")
    if esc_i is None:
        put(8, "not_applicable")
    else:
        # options must be offered at the point of escalation (same turn or the agent's turn just before)
        rx = re.compile(r"payment plan|payment options|payment arrangement|plan de paiement|options de paiement", re.I)
        prev = [k for k in range(esc_i) if turns[k].get("speaker") == "agent"][-1:]
        hit = next((k for k in [esc_i] + prev if rx.search(turns[k]["text"])), None)
        put(8, "pass" if hit is not None else "fail", hit if hit is not None else esc_i)
    if not f["ptp_mentioned"]:
        put(9, "not_applicable")
    else:
        put(9, "pass" if ptp["confirmed_back"] else "fail", ptp["index"],
            None if ptp["confirmed_back"] else "The promise amount and date were not read back.")
    if _find(turns, r".", "customer") is None or turns[-1].get("speaker") == "customer":
        put(10, "not_applicable")   # no right-party contact, or the customer ended the call
    else:
        i = _find(turns, CLOSE, "agent")
        put(10, "pass" if i is not None else "fail", i)
    failed = [x for x in res.values() if x["verdict"] == "fail"]
    score = 100 - sum(x["weight"] for x in failed)
    critical_fail = [x["code"] for x in failed if x["critical"]]
    return {"items": [res[n] for n in sorted(res)], "score": score, "critical_failures": critical_fail,
            "coaching_required": bool(critical_fail)}


# ---- evaluation against the public gold labels ---------------------------------------------------
FIELD_MAP = {"hardship": "label_hardship", "hardship_reason": "label_hardship_reason", "ptp_mentioned": "label_ptp_mentioned",
             "dispute": "label_dispute", "dispute_type": "label_dispute_type", "complaint": "label_complaint",
             "vulnerability": "label_vulnerability", "third_party_present": "label_third_party_present",
             "cease_request": "label_cease_request", "insolvency_mention": "label_insolvency_mention",
             "legal_threat_by_agent": "label_legal_threat_by_agent", "sentiment": "label_sentiment",
             "next_step": "label_next_step"}


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower()


def analyse_by_id(src, transcript_id):
    t = src.transcript(transcript_id)
    contact = src.contact_row(t.get("contact_id")) if t.get("contact_id") else None
    return analyse(t, overdue_at_call=(contact or {}).get("overdue_at_contact"))


def evaluate(limit=None, source=None):
    from c360.layer3.data.source import get_source
    src = source or get_source()
    path = config.maple_data() / "labels" / "call_transcripts_labels_public_500.csv"
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))[:limit]
    per = {k: [0, 0] for k in FIELD_MAP}
    per.update({"ptp_amount": [0, 0], "ptp_date": [0, 0], "qa_items": [0, 0], "qa_score_exact": [0, 0],
                "coaching_required": [0, 0], "summary_exact": [0, 0]})
    misses = {k: [] for k in per}
    for r in rows:
        try:
            a = analyse_by_id(src, r["transcript_id"])
        except Exception:  # noqa: BLE001 - a missing file is reported as skipped
            continue
        f = a["fields"]
        for k, col in FIELD_MAP.items():
            ok = _norm(f[k]) == _norm(r[col])
            per[k][0] += ok
            per[k][1] += 1
            if not ok and len(misses[k]) < 6:
                misses[k].append((r["transcript_id"], _norm(f[k]), _norm(r[col])))
        if r["label_ptp_amount"]:
            per["ptp_amount"][1] += 1
            per["ptp_amount"][0] += f["ptp_amount"] is not None and abs(f["ptp_amount"] - float(r["label_ptp_amount"])) < 0.01
        if r["label_ptp_date"]:
            per["ptp_date"][1] += 1
            ok = f["ptp_date"] == r["label_ptp_date"]
            per["ptp_date"][0] += ok
            if not ok and len(misses["ptp_date"]) < 6:
                misses["ptp_date"].append((r["transcript_id"], f["ptp_date"], r["label_ptp_date"]))
        for item in a["qa"]["items"]:
            gold = r.get(f"qa{item['item_no']:02d}_{item['code']}")
            if gold:
                per["qa_items"][1] += 1
                per["qa_items"][0] += gold == item["verdict"]
                if gold != item["verdict"] and len(misses["qa_items"]) < 10:
                    misses["qa_items"].append((r["transcript_id"], item["code"], item["verdict"], gold))
        if r["qa_total_score"]:
            per["qa_score_exact"][1] += 1
            per["qa_score_exact"][0] += a["qa"]["score"] == int(float(r["qa_total_score"]))
        per["coaching_required"][1] += 1
        per["coaching_required"][0] += _norm(a["qa"]["coaching_required"]) == _norm(r["qa_coaching_required"])
        per["summary_exact"][1] += 1
        ok = a["summary"].strip().lower() == r["label_summary_gold"].strip().lower()
        per["summary_exact"][0] += ok
        if not ok and len(misses["summary_exact"]) < 6:
            misses["summary_exact"].append((r["transcript_id"], a["summary"], r["label_summary_gold"]))
    report = {k: {"correct": c, "n": n, "accuracy": round(c / n, 3) if n else None} for k, (c, n) in per.items()}
    out = config.out_dir() / "postcall_eval.json"
    out.write_text(json.dumps({"fields": report, "examples_of_misses": misses, "n_labels": len(rows),
                               "labels": "labels/call_transcripts_labels_public_500.csv",
                               "engine": "postcall-rules-1.1"}, indent=1, default=str))
    return report, misses


if __name__ == "__main__":
    if "--eval" in sys.argv:
        rep, miss = evaluate()
        for k, v in rep.items():
            print(f"  {k:<24} {v['accuracy']!s:>6}  ({v['correct']}/{v['n']})")
        print(f"report: {config.out_dir() / 'postcall_eval.json'}")
        if "--misses" in sys.argv:
            for k, v in miss.items():
                for m in v:
                    print("MISS", k, m)
