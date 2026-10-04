"""Protected and personal column names: the single source of truth for Layer 2.

A column whose name matches this list must never exist in the serving database, never be
allow-listed in l2_views.yaml, never be referenced in agent SQL and never appear in an answer.
Do not loosen a pattern to make something pass: if a real column looks personal and is not
caught here, add a pattern.
"""
import re

from c360.layer1.golden import PROTECTED_COLUMNS

# Personal data. Anchored on purpose: harmless names such as email_template_id,
# address_mismatch_flag, model_name and table_name must not match.
PII_PATTERNS = [
    r"^(first|middle|last|preferred|full|customer|employer|cardholder|agent)_name$",
    r"^subject_name_reported$",
    r"^date_of_birth$",
    r"^dob",
    r"^age$",
    r"^age_band$",
    r"phone",
    r"^email(_std|_raw)?$",
    r"^address_line",
    r"^postal_code",
    r"^national_id",
    r"_raw$",
    r"^salutation$",
    r"^cardholder_",
]

# Added on the DB machine after reviewing the real columns (doctor "unexpected columns"):
# personal names, customer vulnerability and accent (protected), free-text message bodies that can
# hold personal details, and an income index by postal area (a proxy).
REAL_DATA_PATTERNS = [
    r"_display_name$",                          # agents.agent_display_name: first name + initial
    r"^name_suffix$",
    r"^counterparty_name",                      # transactions: payee / payer names
    r"^vulnerab(le_customer_flag|ility_)",      # customer vulnerability (protected)
    r"accent",                                  # voice_samples accent regions (protected)
    r"^(sms_text|sms_reply_text|email_subject)$",
    r"income_index",                            # geo_reference.fsa_income_index
]

# Proxies for protected attributes named in the data contract (proxies_not_for_decisions).
# Stricter than the spec's list; province_code stays available for regional reporting.
PROXY_PATTERNS = [
    r"^city$",
    r"^fsa$",
    r"^geo_(lat|lon)",
]

_PROTECTED = {c.lower() for c in PROTECTED_COLUMNS}
_COMPILED = [(p, re.compile(p, re.IGNORECASE)) for p in PII_PATTERNS + REAL_DATA_PATTERNS + PROXY_PATTERNS]


def reason(name: str) -> str | None:
    """Why a column name is denied, or None if it is allowed."""
    n = (name or "").strip().strip('"').lower()
    if n in _PROTECTED:
        return "protected attribute"
    for pattern, rx in _COMPILED:
        if rx.search(n):
            return f"personal data pattern {pattern}"
    return None


def is_denied(name: str) -> bool:
    return reason(name) is not None


def find_denied(names) -> list[str]:
    return [n for n in names if is_denied(n)]
