"""Log a conversation: turn a pasted transcript or an agent's notes into structured facts, proposed
follow-ups and checks, and reflect saved follow-ups on the customer document (SPEC_L3_CONVERSATION_EXTRACT.md).

Rules-based and deterministic: the transcript path calls `postcall.analyse` unchanged; notes get a few
extra third-person patterns. Nothing here writes anything except `save()`, which a person confirms.

  parse(...)      text -> turns (speaker-labelled lines, or sentences for notes)
  mask(text)      card numbers, SINs, phone numbers, emails, bank accounts (amounts and dates survive)
  extract(...)    the draft shown to the agent: fields, evidence, alerts, proposed updates, checks
  save(...)       re-runs extract on the server, applies the agent's corrections, writes the log + follow-ups
  overlay(...)    a copy of the customer document with the effects of logged follow-ups applied

Protective findings (stop contact, bankruptcy mention, vulnerability, a legal threat by the agent) create
follow-ups that cannot be skipped, and they hold contact straight away. Nothing that makes collection
more aggressive takes effect without the normal approval path.
"""
import copy
import datetime as dt
import hashlib
import re
import uuid

from c360.layer3 import config, nba, postcall

ENGINE = "conversation-rules-1.0 + postcall-rules-1.1"
SOURCE = "layer3.conversation"
MAX_TEXT = 20000
CHANNELS = ("call", "sms", "email", "chat", "in_person", "video")
PROTECTIVE = ("cease_request", "insolvency_mention", "vulnerability", "legal_threat_by_agent")
HARDSHIP_PHRASE = {label: phrase for label, phrase, _ in postcall.HARDSHIP}
ENUMS = {
    "hardship": ("clear", "possible", "none"),
    "hardship_reason": tuple(HARDSHIP_PHRASE) + (None,),
    "dispute_type": tuple(label for label, _ in postcall.DISPUTE) + (None,),
    "vulnerability_type": tuple(label for label, _ in postcall.VULN) + (None,),
    "sentiment": ("cooperative", "frustrated", "distressed", "hostile"),
    "next_step": ("stop_contact", "escalate_supervisor", "dispute_investigation", "hardship_plan_review",
                  "send_payment_link", "call_back"),
}
BOOLS = ("ptp_mentioned", "dispute", "complaint", "vulnerability", "third_party_present", "cease_request",
         "insolvency_mention", "legal_threat_by_agent")
FOLLOW_UP_TYPES = ("cease_request", "insolvency_check", "vulnerability_flag", "conduct_alert", "promise", "hardship_request",
                   "dispute_request", "contact_preference")


class ConvError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail.get("error", "error"))
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- masking

_MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), lambda m: "[email]"),
    (re.compile(r"(?i)((?:account|acct|transit|compte)\D{0,15})(\d{5,12})(?!\d)"), lambda m: m.group(1) + "[account]"),
    (re.compile(r"(?<![\d$.,-])(?:\d[ -]?){12,18}\d(?!\d|[.,]\d)"),
     lambda m: f"[card ••••{re.sub(r'[^0-9]', '', m.group(0))[-4:]}]"),
    (re.compile(r"(?<![\d$.,-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d.,]\d|\d)"), lambda m: "[phone]"),
    (re.compile(r"(?<![\d$.,-])\d{3}[\s-]?\d{3}[\s-]?\d{3}(?![\d.,]\d|\d)"), lambda m: "[SIN]"),
]


def mask(text):
    if not text:
        return text
    out = str(text)
    for rx, repl in _MASKS:
        out = rx.sub(repl, out)
    return out


# ---------------------------------------------------------------- parsing

AGENT_LABELS = {"agent", "a", "rep", "me", "collector", "conseiller", "conseillère"}
CUSTOMER_LABELS = {"customer", "cust", "c", "cx", "client", "cliente"}
_LINE = re.compile(r"^\s*(?:\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*)?([A-Za-zÀ-ÿ][\w À-ÿ'().-]{0,30}?)\s*(?::|\s-)\s+(.+)$")
_MONTH_ABBR = r"(?:jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|janv|févr|fevr|avr|juil|déc)"


def _secs(stamp):
    parts = [int(p) for p in stamp.split(":")]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def _speaker(label, first_name=None):
    key = re.sub(r"\s*\(.*\)\s*$", "", label.strip().lower())
    if key in AGENT_LABELS:
        return "agent"
    if key in CUSTOMER_LABELS or (first_name and key == str(first_name).strip().lower()):
        return "customer"
    return "third_party"


def _sentences(text):
    pieces = [p.strip() for p in re.split(r"(?<=[.;!?])\s+|\n+", text) if p and p.strip()]
    out = []
    for p in pieces:
        if out and re.search(_MONTH_ABBR + r"\.$", out[-1], re.I) and re.match(r"\d", p):
            out[-1] += " " + p                     # "Oct." + "14 ..." stays together
        else:
            out.append(p)
    return out


def detect_language(text):
    words = re.findall(r"[a-zà-ÿ']+", (text or "").lower())
    if not words:
        return "en"
    markers = sum(w in ("je", "vous", "paiement", "merci", "j'ai", "pas", "le", "de", "client", "a") for w in words)
    french = sum(w in ("je", "vous", "paiement", "merci", "j'ai", "pas", "emploi", "octobre", "payer", "perdu") for w in words)
    return "fr" if french and markers / len(words) > 0.3 else "en"


def parse(inp, first_name=None):
    """-> (turns, warnings, mode). Turn: {turn_index, speaker, start_sec, end_sec, text, sentiment_score}."""
    warnings, mode = [], inp.get("mode") or "transcript"
    tj = inp.get("transcript_json")
    if isinstance(tj, dict) and tj.get("turns"):
        turns = []
        for i, x in enumerate(tj["turns"]):
            if not (x.get("text") or "").strip():
                continue
            sp = str(x.get("speaker") or "").lower()
            turns.append({"turn_index": len(turns), "speaker": sp if sp in ("agent", "customer") else _speaker(sp, first_name),
                          "start_sec": x.get("start_sec", i * 10), "end_sec": x.get("end_sec"), "text": str(x["text"]).strip(),
                          "sentiment_score": x.get("sentiment_score")})
        return turns, warnings, "transcript"
    text = str(inp.get("text") or "")
    if mode == "transcript":
        turns, timed = [], True
        for line in text.splitlines():
            if not line.strip():
                continue
            m = _LINE.match(line)
            if m:
                stamp, label, body = m.groups()
                if stamp is None:
                    timed = False
                turns.append({"turn_index": len(turns), "speaker": _speaker(label, first_name),
                              "start_sec": _secs(stamp) if stamp else len(turns) * 10, "end_sec": None, "text": body.strip(),
                              "sentiment_score": None})
            elif turns:
                turns[-1]["text"] += " " + line.strip()
            else:
                turns = None
                break
        if turns:
            if not timed:
                for i, t in enumerate(turns):
                    t["start_sec"] = i * 10
                warnings.append("Times are approximate.")
            return turns, warnings, "transcript"
        warnings.append("No speaker labels found; treated as notes.")
        mode = "notes"
    turns = [{"turn_index": i, "speaker": "customer", "start_sec": i * 10, "end_sec": None, "text": s, "sentiment_score": None}
             for i, s in enumerate(_sentences(text))]
    return turns, warnings, "notes"


# ---------------------------------------------------------------- notes extras

_MONTHS = {"jan": 1, "feb": 2, "fév": 2, "fev": 2, "mar": 3, "apr": 4, "avr": 4, "may": 5, "mai": 5, "jun": 6, "jui": 6, "jul": 7,
           "aug": 8, "aoû": 8, "aou": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12, "déc": 12}
_MONTH_WORD = r"[A-Za-zéûÉ]{3,9}\.?"
_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_PTP_NOTE = re.compile(r"(?i)\b(?:promised|promise to pay|ptp|agreed to pay|will pay|can pay|a promis de payer|va payer)\s*\$?\s?"
                       r"([\d,]+(?:\.\d+)?)\s*\$?\s*(?:on|by|le|d'ici le)?\s*(.{0,24})")
_NOTE_HARDSHIP = [
    ("job_loss", r"lost (?:his|her|their|my)? ?job|job loss|laid off|perdu son emploi"),
    ("reduced_income", r"hours (?:were )?(?:cut|reduced)|reduced hours"),
    ("illness", r"on sick leave|in hospital|en arrêt maladie"),
    ("separation", r"\bseparated\b"),
    ("ei_waiting", r"\bon EI\b|waiting for EI"),
    ("disaster_evacuation", r"\bevacuated\b"),
]
_NOTE_CEASE = r"asked (?:us )?(?:not to|to stop) (?:call|contact)|do not call\b|no more calls|ne veut plus être appelé"
_NOTE_DISPUTE = r"says (?:they|she|he) already paid|\bdisputes\b|not (?:his|her|their) charge"
_RESTRICTION = r"(?:please )?don'?t call (?:me |her |him )?at work|only (?:text|email)\b[^.;]*|call after \d[^.;]*|pas au travail"


def parse_date(text, ref):
    """A date from 'Oct 14', '14 Oct', 'October 14th', '14 octobre', '2026-10-14', 'next Friday'. None if not understood
    (numeric slash dates are ambiguous and are left for the agent to enter)."""
    t = (text or "").strip()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.search(r"\b\d{1,2}/\d{1,2}\b", t):
        return None
    m = re.search(r"(?i)\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        delta = (_DAYS[m.group(1).lower()] - ref.weekday() - 1) % 7 + 1
        return ref + dt.timedelta(days=delta)
    month = day = None
    m = re.search(rf"(?i)\b({_MONTH_WORD})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m and m.group(1).lower()[:3] in _MONTHS:
        month, day = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
    else:
        m = re.search(rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s+({_MONTH_WORD})", t)
        if m and m.group(2).lower()[:3] in _MONTHS:
            month, day = _MONTHS[m.group(2).lower()[:3]], int(m.group(1))
    if month is None:
        return None
    try:
        return dt.date(ref.year + (1 if month < ref.month - 6 else 0), month, day)
    except ValueError:
        return None


def _ev(turns, i):
    return {"turn_index": i, "sec": turns[i].get("start_sec"), "speaker": turns[i].get("speaker"), "text": turns[i]["text"]}


def _first(turns, pattern):
    rx = re.compile(pattern, re.I)
    return next((i for i, x in enumerate(turns) if rx.search(x["text"])), None)


def _notes_extras(turns, fields, evidence, ref, warnings):
    changed = False
    if not fields["ptp_mentioned"] or fields.get("ptp_date") is None:
        for i, x in enumerate(turns):
            m = _PTP_NOTE.search(x["text"])
            if m:
                amount = float(m.group(1).replace(",", ""))
                date = parse_date(m.group(2), ref)
                if date is None:
                    warnings.append("Promise date not understood; enter it.")
                fields.update(ptp_mentioned=True, ptp_amount=fields.get("ptp_amount") or amount,
                              ptp_date=fields.get("ptp_date") or (date and date.isoformat()))
                evidence.setdefault("ptp", _ev(turns, i))
                changed = True
                break
    if fields["hardship"] != "clear":
        for label, pat in _NOTE_HARDSHIP:
            i = _first(turns, pat)
            if i is not None:
                fields.update(hardship="clear", hardship_reason=label)
                evidence["hardship"] = _ev(turns, i)
                changed = True
                break
    for key, ev_key, pat in (("cease_request", "cease_request", _NOTE_CEASE), ("dispute", "dispute", _NOTE_DISPUTE),
                             ("insolvency_mention", "insolvency", postcall.INSOLVENCY)):
        if not fields[key]:
            i = _first(turns, pat)
            if i is not None:
                fields[key] = True
                if key == "dispute" and not fields.get("dispute_type"):
                    fields["dispute_type"] = "payment_not_applied" if re.search(r"already paid", turns[i]["text"], re.I) else "billing_error"
                evidence[ev_key] = _ev(turns, i)
                changed = True
    return changed


def _next_step(fields, evidence):
    if fields["cease_request"]:
        return "stop_contact"
    if evidence.get("escalation"):
        return "escalate_supervisor"
    if fields["dispute"]:
        return "dispute_investigation"
    if fields["hardship"] == "clear":
        return "hardship_plan_review"
    if fields["ptp_mentioned"]:
        return "send_payment_link"
    return "call_back"


def summarise(fields):
    return postcall.summary(fields, HARDSHIP_PHRASE.get(fields.get("hardship_reason")))


# ---------------------------------------------------------------- input, extraction

def clean_input(body) -> dict:
    """Validate the capture form. Raises ConvError(400, {error, field})."""
    def bad(field, msg):
        raise ConvError(400, {"error": msg, "field": field})
    mode = body.get("mode") or "transcript"
    if mode not in ("transcript", "notes"):
        bad("mode", "Choose TRANSCRIPT or MY NOTES.")
    tj = body.get("transcript_json")
    text = str(body.get("text") or "")
    if not (isinstance(tj, dict) and tj.get("turns")):
        tj = None
        if not text.strip():
            bad("text", "Paste the transcript or your notes first.")
        if len(text) > MAX_TEXT:
            bad("text", f"Too long: {MAX_TEXT:,} characters at most.")
    if body.get("channel") not in CHANNELS:
        bad("channel", "Choose how the conversation happened.")
    if body.get("direction") not in ("inbound", "outbound"):
        bad("direction", "Choose inbound or outbound.")
    try:
        started = dt.datetime.fromisoformat(str(body.get("started_at"))[:19])
    except (TypeError, ValueError):
        bad("started_at", "Give the date and time of the conversation.")
    if started > dt.datetime.now() + dt.timedelta(minutes=5):
        bad("started_at", "The conversation can't be in the future.")
    lang = body.get("language") or "auto"
    if lang not in ("auto", "en", "fr"):
        bad("language", "Language must be auto, en or fr.")
    dur = body.get("duration_sec")
    if dur is not None and not (isinstance(dur, (int, float)) and 0 <= dur <= 14400):
        bad("duration_sec", "Duration must be between 0 and 240 minutes.")
    return {"mode": mode, "text": text, "transcript_json": tj, "channel": body["channel"], "direction": body["direction"],
            "started_at": started.isoformat(timespec="seconds"), "language": lang, "duration_sec": dur}


def input_sha(inp, case_id):
    norm = " ".join(str(inp.get("text") or "").split()).lower() if not inp.get("transcript_json") else repr(inp["transcript_json"])
    return hashlib.sha256(f"{case_id}|{inp['mode']}|{inp['channel']}|{norm}".encode()).hexdigest()


def extract(inp, doc, case) -> dict:
    """The draft for the review screen. Reads only; writes nothing."""
    first = (doc.get("profile") or {}).get("first_name")
    turns, warnings, mode = parse(inp, first)
    if not turns:
        raise ConvError(400, {"error": "Nothing to read in that text.", "field": "text"})
    raw_text = " ".join(t["text"] for t in turns)
    language = inp["language"] if inp["language"] != "auto" else detect_language(raw_text)
    started = dt.datetime.fromisoformat(inp["started_at"])
    t = {"transcript_id": None, "case_id": case["case_id"], "call_start_ts": inp["started_at"], "direction": inp["direction"],
         "language": language, "duration_sec": inp.get("duration_sec"), "turns": turns}
    a = postcall.analyse(t, overdue_at_call=case.get("total_overdue"))        # unchanged post-call rules
    fields, evidence = dict(a["fields"]), {}
    for k, ev in (a["evidence"] or {}).items():
        if ev:
            idx = next((i for i, x in enumerate(turns) if x["text"] == ev["text"] and x.get("start_sec") == ev.get("sec")), None)
            evidence[k] = dict(ev, turn_index=idx)
    qa = a["qa"]
    if mode == "notes":
        _notes_extras(turns, fields, evidence, started.date(), warnings)
        fields["legal_threat_by_agent"] = None          # agent-side findings are not assessed from notes
        qa = None
    i = _first(turns, _RESTRICTION)
    fields["contact_restriction"] = re.search(_RESTRICTION, turns[i]["text"], re.I).group(0).strip() if i is not None else None
    if i is not None:
        evidence["contact_restriction"] = _ev(turns, i)
    fields["next_step"] = _next_step(fields, evidence)
    masked_turns = [dict(x, text=mask(x["text"])) for x in turns]
    masked_ev = {k: dict(v, text=mask(v["text"])) for k, v in evidence.items()}
    updates = propose(fields, case, masked_ev, inp["channel"])
    draft = {"draft_id": input_sha(inp, case["case_id"])[:16], "case_id": case["case_id"], "c360_id": doc.get("_id"), "mode": mode,
             "language": language, "channel": inp["channel"], "direction": inp["direction"], "started_at": inp["started_at"],
             "duration_sec": inp.get("duration_sec"), "turns": masked_turns, "warnings": warnings,
             "times_approximate": mode == "notes" or "Times are approximate." in warnings,
             "fields": fields, "evidence": masked_ev, "summary": summarise(fields), "qa": qa,
             "alerts": alerts(fields, masked_ev), "proposed_updates": updates,
             "checks": validate(updates, [u["id"] for u in updates if u["default_selected"] or u["mandatory"]], fields, case, started.date()),
             "engine": ENGINE}
    return draft


def alerts(fields, evidence):
    out = []

    def add(level, code, text, cite, ev_key):
        out.append({"level": level, "code": code, "text": text, "cite": cite, "evidence": evidence.get(ev_key)})
    if fields.get("cease_request"):
        add("stop", "stop_contact", "Customer asked us to stop contacting them.", "POL-COLL-001 §5.4", "cease_request")
    if fields.get("insolvency_mention"):
        add("stop", "insolvency", "Bankruptcy or a consumer proposal was mentioned: verify before any contact.", "POL-COLL-001 §7.3", "insolvency")
    if fields.get("legal_threat_by_agent"):
        add("stop", "conduct", "The agent threatened legal action.", "POL-COLL-001 §5.2", "legal_threat")
    if fields.get("vulnerability"):
        add("care", "vulnerability", "The customer may need extra care.", "POL-COLL-004 §4.5", "vulnerability")
    if fields.get("hardship") in ("clear", "possible"):
        phrase = HARDSHIP_PHRASE.get(fields.get("hardship_reason"))
        add("care", "hardship", f"Customer reports {phrase}." if phrase else "Customer indicates finances are tight.", "POL-COLL-004 §4.2", "hardship")
    if fields.get("complaint"):
        add("care", "complaint", "The customer wants to complain: record and escalate it.", "POL-COLL-007 §8.1", "complaint")
    return out


# ---------------------------------------------------------------- proposals and checks

def propose(fields, case, evidence, channel) -> list[dict]:
    ups = []

    def add(kind, creates, mandatory, selected, needs, why, cite, ev_key, value=None):
        ups.append({"id": f"u{len(ups) + 1}", "kind": kind, "creates_type": creates, "mandatory": mandatory,
                    "default_selected": selected, "needs": needs, "why": why, "cite": cite, "value": value or {},
                    "evidence": evidence.get(ev_key)})
    if fields.get("cease_request"):
        add("stop_contact", "cease_request", True, True, "team_lead", "Customer asked us to stop contact. Contact is held now.", "POL-COLL-001 §5.4", "cease_request")
    if fields.get("insolvency_mention"):
        add("insolvency_check", "insolvency_check", True, True, "team_lead", "Bankruptcy or consumer proposal mentioned. Contact is held until checked.", "POL-COLL-001 §7.3", "insolvency")
    if fields.get("vulnerability"):
        add("vulnerability", "vulnerability_flag", True, True, "team_lead", "Possible vulnerability: safeguards apply.", "POL-COLL-004 §4.5", "vulnerability",
            {"subtype": fields.get("vulnerability_type")})
    if fields.get("legal_threat_by_agent"):
        add("conduct", "conduct_alert", True, True, "team_lead", "Agent conduct: a legal threat was made on the call.", "POL-COLL-001 §5.2", "legal_threat")
    if fields.get("ptp_mentioned") and fields.get("ptp_amount") and fields.get("ptp_date"):
        add("promise", "promise", False, True, "agent", f"Promise of ${fields['ptp_amount']:,.2f} on {fields['ptp_date']}.", "PRC-COLL-010 §3.1", "ptp",
            {"amount": round(float(fields["ptp_amount"]), 2), "date": fields["ptp_date"], "channel": channel,
             "covers_overdue": float(fields["ptp_amount"]) >= (case.get("total_overdue") or 0)})
    if fields.get("hardship") in ("clear", "possible"):
        phrase = HARDSHIP_PHRASE.get(fields.get("hardship_reason"))
        add("hardship", "hardship_request", False, fields["hardship"] == "clear", "hardship_specialist",
            f"Customer reports {phrase}." if phrase else "Finances may be tight.", "POL-COLL-004 §4.2", "hardship",
            {"reason": fields.get("hardship_reason") or "possible"})
    if fields.get("dispute"):
        add("dispute", "dispute_request", False, True, "agent", f"Dispute: {(fields.get('dispute_type') or 'type not stated').replace('_', ' ')}.",
            "PRC-COLL-012 §6.1", "dispute", {"dispute_type": fields.get("dispute_type")})
    if fields.get("contact_restriction"):
        add("contact_preference", "contact_preference", False, True, "agent", f'Contact wish: "{fields["contact_restriction"]}".',
            None, "contact_restriction", {"note": fields["contact_restriction"]})
    return ups


def validate_promise(amount, date, case, conversation_date=None) -> list[dict]:
    """The same rules as the customer app's promise (/api/me/promise). Each failure blocks the save."""
    out = []
    snap = config.SNAPSHOT_DATE
    d = date if isinstance(date, dt.date) else (dt.date.fromisoformat(str(date)[:10]) if date else None)
    if d is None or d <= snap or (conversation_date and d < conversation_date):
        out.append({"rule": "promise_date_past", "text": "Choose a date after today.", "cite": None})
    elif (d - snap).days > 21:
        out.append({"rule": "promise_too_far", "text": "The promise date must be within the next 21 days.", "cite": "PRC-COLL-010 §3.1"})
    elif d in nba.STAT_HOLIDAYS:
        out.append({"rule": "promise_holiday", "text": "That's a statutory holiday; choose another date.", "cite": None})
    bal = case.get("total_exposure") or case.get("total_overdue") or 0
    if amount is None or amount <= 0:
        out.append({"rule": "promise_amount", "text": "The promise needs an amount above zero.", "cite": None})
    elif bal and amount > bal * 1.01:
        out.append({"rule": "promise_over_balance", "text": "That's more than the balance.", "cite": None})
    return out


def validate(updates, selected, fields, case, conversation_date=None) -> list[dict]:
    checks = []
    chosen = [u for u in updates if u["id"] in set(selected)]
    for u in chosen:
        if u["kind"] == "promise":
            for c in validate_promise(u["value"].get("amount"), u["value"].get("date"), case, conversation_date):
                checks.append(dict(c, blocking=True, update_id=u["id"]))
            if fields.get("hardship") in ("clear", "possible"):
                checks.append({"rule": "hardship_with_promise", "blocking": False, "update_id": u["id"], "cite": "POL-COLL-004 §4.2",
                               "text": "Hardship was mentioned: make sure the customer heard the hardship options before agreeing this promise."})
            if fields.get("cease_request"):
                checks.append({"rule": "cease_with_promise", "blocking": False, "update_id": u["id"], "cite": "POL-COLL-001 §5.4",
                               "text": "Customer asked to stop contact; the promise is recorded but no reminder calls will be made."})
    return checks


# ---------------------------------------------------------------- save

def preview(body, doc, case) -> dict:
    """extract() plus the agent's corrections so far: proposals, checks and summary recomputed. Writes nothing."""
    inp = clean_input(body)
    draft = extract(inp, doc, case)
    fields = dict(draft["fields"])
    diff = _apply_corrections(fields, body.get("corrections"), body.get("correction_reasons"), require_reason=False)
    if diff:
        if not any(d["field"] == "next_step" for d in diff):
            fields["next_step"] = _next_step(fields, draft["evidence"])
        updates = propose(fields, case, draft["evidence"], inp["channel"])
        kinds = body.get("selected_kinds")
        chosen = [u["id"] for u in updates if u["mandatory"] or (u["kind"] in kinds if isinstance(kinds, list) else u["default_selected"])]
        draft.update(fields_final=fields, corrections=diff, proposed_updates=updates, summary=summarise(fields),
                     alerts=alerts(fields, draft["evidence"]),
                     checks=validate(updates, chosen, fields, case, dt.datetime.fromisoformat(inp["started_at"]).date()))
    elif isinstance(body.get("selected_kinds"), list):
        chosen = [u["id"] for u in draft["proposed_updates"] if u["mandatory"] or u["kind"] in body["selected_kinds"]]
        draft["checks"] = validate(draft["proposed_updates"], chosen, fields, case, dt.datetime.fromisoformat(inp["started_at"]).date())
    return draft


def _apply_corrections(fields, corrections, reasons, require_reason=True):
    diff = []
    for name, value in (corrections or {}).items():
        if name not in fields:
            raise ConvError(400, {"error": f"Unknown field '{name}'.", "field": name})
        if name in ENUMS and value not in ENUMS[name]:
            raise ConvError(400, {"error": f"'{value}' is not a valid value for {name.replace('_', ' ')}.", "field": name})
        if name in BOOLS and not isinstance(value, bool):
            raise ConvError(400, {"error": f"{name.replace('_', ' ')} must be yes or no.", "field": name})
        if name == "ptp_amount" and value is not None:
            try:
                value = round(float(value), 2)
            except (TypeError, ValueError):
                raise ConvError(400, {"error": "The promise amount must be a number.", "field": name})
        if name == "ptp_date" and value is not None:
            try:
                value = dt.date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                raise ConvError(400, {"error": "The promise date must be a date (YYYY-MM-DD).", "field": name})
        if name == "contact_restriction" and value is not None:
            value = mask(str(value))[:200]
        if fields[name] == value:
            continue
        reason = ((reasons or {}).get(name) or "").strip()
        if require_reason and name in PROTECTIVE and fields[name] is True and value is False and not reason:
            raise ConvError(400, {"error": f"Say why '{name.replace('_', ' ')}' is not right before changing it.", "field": name})
        diff.append({"field": name, "from": fields[name], "to": value, "reason": reason[:500] or None})
        fields[name] = value
    return diff


def save(body, doc, case, user, store) -> dict:
    """Re-run extraction on the server, apply corrections, check, then write the log and the chosen follow-ups."""
    if body.get("confirm") is not True:
        raise ConvError(400, {"error": "Confirm before saving.", "field": "confirm"})
    inp = clean_input(body)
    draft = extract(inp, doc, case)                           # the client's own 'fields' are never trusted
    extracted = dict(draft["fields"])
    fields = dict(draft["fields"])
    diff = _apply_corrections(fields, body.get("corrections"), body.get("correction_reasons"))
    if diff:
        fields["next_step"] = fields["next_step"] if any(d["field"] == "next_step" for d in diff) else _next_step(fields, draft["evidence"])
    updates = propose(fields, case, draft["evidence"], inp["channel"])
    selected = [str(x) for x in (body.get("selected_updates") or [])]
    for u in updates:
        if u["mandatory"] and u["id"] not in selected:
            raise ConvError(400, {"error": f"This update can't be skipped: {u['why']}", "field": "selected_updates", "update_id": u["id"]})
    started = dt.datetime.fromisoformat(inp["started_at"])
    checks = validate(updates, selected, fields, case, started.date())
    if any(c["blocking"] for c in checks):
        raise ConvError(409, {"error": "Fix these before saving.", "checks": checks})
    note = (body.get("agent_note") or "").strip()
    if len(note) > 1000:
        raise ConvError(400, {"error": "The note is too long (1,000 characters at most).", "field": "agent_note"})
    sha = input_sha(inp, case["case_id"])
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
    for it in store.list(case_id=case["case_id"], types=["conversation_log"], limit=20):
        if (it["payload"] or {}).get("input_sha256") == sha and it["ts"] >= cutoff:
            raise ConvError(409, {"error": "This conversation was already saved.", "id": (it["payload"] or {}).get("conversation_id")})

    conv_id = "CNV-" + uuid.uuid4().hex[:12]
    payload = {"conversation_id": conv_id, "channel": inp["channel"], "direction": inp["direction"], "started_at": inp["started_at"],
               "language": draft["language"], "mode": draft["mode"], "duration_sec": inp.get("duration_sec"), "turn_count": len(draft["turns"]),
               "text_masked": mask(inp["text"]) if not inp.get("transcript_json") else None, "turns_masked": draft["turns"],
               "fields_extracted": extracted, "fields_final": fields, "corrections": diff, "evidence": draft["evidence"],
               "summary": summarise(fields), "qa_score": (draft["qa"] or {}).get("score"),
               "qa_coaching_required": (draft["qa"] or {}).get("coaching_required"), "alerts": alerts(fields, draft["evidence"]),
               "agent_note": mask(note) or None, "warnings": draft["warnings"], "engine": ENGINE, "input_sha256": sha}
    log = store.add("conversation_log", doc["_id"], case["case_id"], payload, source=SOURCE, actor=user["id"], status="recorded",
                    mirror_exclude=("text_masked", "turns_masked"))
    created = []
    for u in updates:
        if u["id"] not in selected:
            continue
        quote = (u.get("evidence") or {}).get("text")
        common = {"conversation_id": conv_id, "logged_by": user["id"], "evidence": quote, "cite": u["cite"]}
        kind = u["kind"]
        if kind == "promise":
            p = dict(u["value"], confirmed_back=bool(draft["qa"]) and fields.get("ptp_mentioned", False), **common)
            status = "recorded"
        elif kind == "hardship":
            p = dict(program_id=None, program_name=None, reason=u["value"]["reason"], note="Stated in conversation",
                     approver_level="hardship_specialist", requires_income_evidence=None, **common)
            status = "pending"
        elif kind == "stop_contact":
            p = dict(channels=None, note=quote, approver_level="team_lead", **common)
            status = "pending"
        elif kind == "dispute":
            p = dict(dispute_type=u["value"].get("dispute_type"), details=quote, **common)
            status = "pending"
        elif kind == "contact_preference":
            p = dict(channels=None, note=u["value"].get("note"), **common)
            status = "pending"
        else:                                           # insolvency_check, vulnerability, conduct
            p = dict(subtype=u["value"].get("subtype"), quote=quote, approver_level="team_lead", **common)
            status = "pending"
        created.append(store.add(u["creates_type"], doc["_id"], case["case_id"], p, source=SOURCE, actor=user["id"], status=status))
    return {"conversation": log, "created": created}


# ---------------------------------------------------------------- overlay

def overlay(doc, store):
    """The customer document with the effects of follow-ups logged from conversations applied to the matching case.
    With nothing logged the document is returned unchanged (the same object)."""
    try:
        items = [i for i in store.list(c360_id=doc.get("_id"), types=list(FOLLOW_UP_TYPES), limit=200)
                 if i.get("source") == SOURCE and i.get("status") != "rejected"]
    except Exception:       # noqa: BLE001 - the customer window must never fail because of the log
        return doc
    if not items:
        return doc
    out = copy.deepcopy(doc)
    cases = {c.get("case_id"): c for c in (out.get("collections") or {}).get("cases") or []}
    effects = []
    for it in sorted(items, key=lambda i: i["ts"]):
        case, p, t = cases.get(it.get("case_id")), it.get("payload") or {}, it["type"]
        if case is None:
            continue

        def eff(field, value):
            effects.append({"case_id": case["case_id"], "field": field, "value": value, "interaction_id": it["id"],
                            "conversation_id": p.get("conversation_id"), "type": t, "status": it["status"],
                            "logged_by": it.get("actor"), "ts": it["ts"]})
        live = it["status"] in ("pending", "approved")
        if t == "cease_request" and live:
            case["cease_contact_flag"] = True
            eff("cease_contact_flag", True)
        elif t == "insolvency_check" and live:
            case["insolvency_hold_flag"] = True
            eff("insolvency_hold_flag", True)
        elif t == "vulnerability_flag" and live:
            case["vulnerable_customer_flag"] = True
            eff("vulnerable_customer_flag", True)
        elif t == "hardship_request" and live:
            if not nba.flag(case.get("hardship_flag")):
                case["hardship_flag"] = True
                case["hardship_reason"] = None if p.get("reason") == "possible" else p.get("reason")
            eff("hardship_flag", True)
        elif t == "dispute_request" and live:
            if not nba.flag(case.get("dispute_flag")):
                case.update(dispute_flag=True, dispute_type=p.get("dispute_type"), dispute_status="open")
            eff("dispute_flag", True)
        elif t == "promise" and it["status"] == "recorded":
            pr = case.setdefault("promises", {})
            pr.setdefault("logged", []).append({"amount": p.get("amount"), "date": p.get("date"), "channel": p.get("channel"),
                                                "interaction_id": it["id"], "conversation_id": p.get("conversation_id"),
                                                "logged_by": it.get("actor"), "ts": it["ts"]})
            pr["open"] = (pr.get("open") or 0) + 1
            pr["total"] = (pr.get("total") or 0) + 1
            eff("promises.logged", {"amount": p.get("amount"), "date": p.get("date")})
        elif t == "contact_preference" and live:
            eff("contact_preference", p.get("note"))
        elif t == "conduct_alert" and live:
            eff("conduct_alert", True)
    out["_overlay"] = effects
    return out
