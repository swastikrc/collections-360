"""Input and output guards. Deterministic code on both sides of the model.

Input guard: cheap checks before any model call (length, and a few obvious forbidden asks).
The patterns are deliberately conservative; the model's `refuse` tool handles everything subtler.

Output guard: fills `sources` from the trace, redacts phone numbers, email addresses and
deny-listed field names from the answer, and warns about numbers that no tool returned.
"""
import json
import re
from dataclasses import dataclass

from c360.layer2 import config, deny_list
from c360.layer2.answer import Answer, SourceRef, Trace
from c360.layer2.tools import jsonable

# ---- input ---------------------------------------------------------------------------------------
_PERSONAL = (r"(phone numbers?|telephone numbers?|mobile numbers?|cell numbers?|e-?mail addresses|email ids|home addresses|"
             r"mailing addresses|street addresses|postal addresses|contact (details|info|information|lists?|numbers?)|"
             r"dates? of birth|birth ?dates?|social insurance numbers?|\bSINs?\b)")
_BULK_VERB = r"(list|export|give|show|send|pull|dump|download|extract|get|print|provide|share|fetch|retrieve|compile|generate)"
_PROTECTED = (r"(gender|sex|religion|religious|ethnicity|ethnic|race|racial|nationality|citizenship|marital status|"
              r"disabilit(?:y|ies)|sexual orientation|immigration status)")
_DIMENSION_CUE = (r"(by|per|across|between|versus|vs\.?|based on|according to|split|breakdown|broken down|grouped|group|"
                  r"segment(?:ed)?|compare|comparison|differ|difference|correlat\w*|which|for each|among)")

INPUT_PATTERNS = [
    ("bulk_contact_export", re.compile(rf"\b{_BULK_VERB}\b.{{0,80}}{_PERSONAL}", re.IGNORECASE | re.DOTALL),
     "Listing or exporting customers' contact or identity details is not allowed. I can give counts and rates instead."),
    ("bulk_contact_export", re.compile(rf"{_PERSONAL}.{{0,40}}\b(of|for)\b.{{0,40}}\b(all|every|each|these|those)\b", re.IGNORECASE | re.DOTALL),
     "Listing or exporting customers' contact or identity details is not allowed. I can give counts and rates instead."),
    ("protected_attributes", re.compile(rf"\b{_DIMENSION_CUE}\b.{{0,50}}\b{_PROTECTED}\b", re.IGNORECASE | re.DOTALL),
     "Analysis by protected attributes (or proxies for them) is not allowed in Collections 360."),
    ("protected_attributes", re.compile(rf"\b{_PROTECTED}\b.{{0,50}}\b{_DIMENSION_CUE}\b", re.IGNORECASE | re.DOTALL),
     "Analysis by protected attributes (or proxies for them) is not allowed in Collections 360."),
]


@dataclass(frozen=True)
class InputDecision:
    kind: str               # "ok" | "error" | "refuse"
    rule: str | None = None
    message: str | None = None


def input_guard(question) -> InputDecision:
    text = str(question or "").strip()
    if not text:
        return InputDecision("error", "invalid_input", "The question is empty.")
    if len(text) > config.MAX_QUESTION_CHARS:
        return InputDecision("error", "invalid_input",
                             f"The question is too long ({len(text):,} characters; the limit is {config.MAX_QUESTION_CHARS:,}).")
    for rule, pattern, message in INPUT_PATTERNS:
        if pattern.search(text):
            return InputDecision("refuse", rule, message)
    return InputDecision("ok")


# ---- output: redaction -------------------------------------------------------------------------
PHONE = re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w-])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_IDENTIFIER = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
REDACTED_PHONE, REDACTED_EMAIL, REDACTED_FIELD = "[redacted phone]", "[redacted email]", "[redacted field]"


def redact(text, counts):
    """Redact phones, emails and deny-listed field names in a string. `counts` is updated in place."""
    if not isinstance(text, str) or not text:
        return text

    def sub(pattern, replacement, key, s):
        out, n = pattern.subn(replacement, s)
        counts[key] = counts.get(key, 0) + n
        return out

    text = sub(EMAIL, REDACTED_EMAIL, "email", text)
    text = sub(PHONE, REDACTED_PHONE, "phone", text)

    def field(m):
        if deny_list.is_denied(m.group(0)):
            counts["field"] = counts.get("field", 0) + 1
            return REDACTED_FIELD
        return m.group(0)
    return _IDENTIFIER.sub(field, text)


# ---- output: number grounding ------------------------------------------------------------------
_NUMBER = re.compile(r"(?<![\w.,/-])(-?\$?\d[\d,]*(?:\.\d+)?)\s?(%|percent\b|k\b|m\b|bn\b|million\b|thousand\b|billion\b)?(?![\w/-]|\.\d)", re.IGNORECASE)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
SMALL_INT_MAX = 10      # "top 3", "2 queues": small whole numbers are not checked


def _collect(value, numbers, texts):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        numbers.append(float(value))
    elif isinstance(value, str):
        texts.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            texts.append(str(k))
            _collect(v, numbers, texts)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, numbers, texts)


def ungrounded_numbers(texts_to_check, trace: Trace, question="") -> list[str]:
    """Numbers stated in the answer that do not appear in any tool result (allowing for rounding to
    the displayed precision and for ratio <-> percentage)."""
    numbers, texts = [], [str(question)]
    for r in trace.results:
        payload = r.payload()
        _collect(payload, numbers, texts)
        _collect(r.meta, numbers, texts)
        numbers.append(float(r.row_count))
        if isinstance(r.data, list):
            numbers.append(float(len(r.data)))
    for step in trace.steps:
        _collect(jsonable(step.args), numbers, texts)
    corpus = " ".join(texts)
    known_tokens = set(re.findall(r"\d[\d,.]*\d|\d", corpus))

    missing = []
    for text in texts_to_check:
        if not text:
            continue
        for m in _NUMBER.finditer(text):
            token, suffix = m.group(1), (m.group(2) or "").lower()
            raw = token.replace("$", "").replace(",", "")
            try:
                x = float(raw)
            except ValueError:
                continue
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            if not suffix and decimals == 0 and abs(x) <= SMALL_INT_MAX:
                continue
            if raw.lstrip("-") in known_tokens or token.replace("$", "").lstrip("-") in known_tokens:
                continue        # the same digits appear in a result, the question or an argument (ids, dates, labels)
            scale = _SCALE.get(suffix, 1.0)
            tolerance = 0.5 * 10 ** (-decimals) * scale + 1e-9
            target = x * scale
            factors = (100.0, 1.0) if suffix in ("%", "percent") else (1.0, 100.0, 0.01)
            if any(abs(v * f - target) <= tolerance for v in numbers for f in factors):
                continue
            shown = m.group(0).strip()
            if shown not in missing:
                missing.append(shown)
    return missing


# ---- output: sources ---------------------------------------------------------------------------
def sources_from_trace(trace: Trace) -> list[SourceRef]:
    out = []
    for r in trace.results:
        if not r.ok:
            continue
        if r.tool == "compute_metric":
            out.append(SourceRef(kind="governed_metric", detail=r.detail or "Governed metric", ids=r.source_ids, sql=r.sql))
        elif r.tool == "run_sql":
            out.append(SourceRef(kind="sql", detail=r.detail or "Custom query", ids=r.source_ids, sql=r.sql))
        elif r.tool == "get_customer":
            out.append(SourceRef(kind="customer", detail=r.detail or "Customer record", ids=r.source_ids))
        elif r.tool == "search_text":
            hits = r.data or []
            for kind, label in (("note", "notes"), ("transcript", "transcripts")):
                ids = [h["id"] for h in hits if h.get("source") == kind]
                if ids:
                    cases = list(dict.fromkeys(h["case_id"] for h in hits if h.get("source") == kind and h.get("case_id")))
                    out.append(SourceRef(kind=label, detail=f"{len(ids)} {label} matching \"{r.meta.get('query', '')}\"",
                                         ids=ids + cases, sql=None))
            if not hits:
                out.append(SourceRef(kind="notes", detail=f"Text search found nothing for \"{r.meta.get('query', '')}\"", ids=[]))
        elif r.tool == "search_policy":
            out.append(SourceRef(kind="policy", detail=f"Policy sections matching \"{r.meta.get('query', '')}\"",
                                 ids=list(r.source_ids), sql=None))
    return out


def output_guard(answer: Answer, trace: Trace, question="") -> Answer:
    """Fill sources and warnings from the trace; redact; check that stated numbers are grounded."""
    answer.sources = sources_from_trace(trace)
    warnings = list(answer.warnings)
    for r in trace.results:
        for w in r.warnings:
            if w not in warnings:
                warnings.append(w)

    counts: dict = {}
    answer.answer_text = redact(answer.answer_text, counts)
    answer.insight = redact(answer.insight, counts)
    answer.follow_ups = [redact(f, counts) for f in answer.follow_ups]
    answer.interpretation = [redact(i, counts) for i in answer.interpretation]
    if answer.table:
        denied_cols = [i for i, c in enumerate(answer.table.columns)
                       if deny_list.is_denied(re.sub(r"[\s\-]+", "_", str(c).strip().lower()))]
        answer.table.rows = [[REDACTED_FIELD if i in denied_cols and cell is not None else redact(cell, counts)
                              for i, cell in enumerate(row)] for row in answer.table.rows]
        if denied_cols:
            counts["field"] = counts.get("field", 0) + len(denied_cols)
        answer.table.columns = [redact(str(c), counts) for c in answer.table.columns]
    if counts.get("phone"):
        warnings.append(f"Redacted {counts['phone']} phone number(s) from the answer.")
    if counts.get("email"):
        warnings.append(f"Redacted {counts['email']} email address(es) from the answer.")
    if counts.get("field"):
        warnings.append(f"Redacted {counts['field']} personal or protected field(s) from the answer.")

    if answer.status != "refused":
        for number in ungrounded_numbers([answer.answer_text, answer.insight], trace, question):
            warnings.append(f"number not found in results: {number}")
    answer.warnings = warnings
    return answer


def contains_personal_data(answer: Answer) -> list[str]:
    """Used by the evaluation harness: anything personal left in a final answer."""
    blob = json.dumps(answer.model_dump(include={"answer_text", "insight", "table", "follow_ups", "interpretation"}), default=str)
    found = [m.group(0) for m in EMAIL.finditer(blob)] + [m.group(0) for m in PHONE.finditer(blob)]
    found += [m.group(0) for m in _IDENTIFIER.finditer(blob) if deny_list.is_denied(m.group(0))]
    return found
