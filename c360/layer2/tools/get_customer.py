"""get_customer: one customer's Collections 360 record, trimmed before the model sees it.

The trimming is declarative: edit the lists below, not the code. Contact details, date of birth,
age, match evidence and file paths never leave this function.
"""
import datetime as dt

from c360.layer2.tools import ToolResult, failure

# Paths removed from the document. `[]` means "every element of this list".
DROP = [
    "contact.primary_phone",
    "contact.email",
    "contact.address_line1",
    "contact.postal_code",
    "profile.date_of_birth",
    "profile.age",
    "profile.age_band",
    "identity.source_keys[].evidence",
    "collections.call_transcripts[].file_path",
    "interactions[].payload",
]

# Lists cut to their first N items after sorting newest-first by the given field.
KEEP_AT_MOST = {
    "collections.cases": (5, "case_open_date"),
    "collections.recent_notes": (5, "note_ts"),
    "model_scores": (5, "score_date"),
    "collections.call_transcripts": (5, "call_start_ts"),
    "interactions": (5, "occurred_ts"),
}

# Text fields cut to N characters.
TRUNCATE = {
    "collections.recent_notes[].note_text": 300,
}

NOT_A_KEY = ("not a customer/case/account ID — use compute_metric or run_sql for questions across customers. "
             "Accepted keys: a C360 id (C360-...), case id (CS-YYYY-...), card (C-...), loan (L-...) or deposit "
             "(CHQ-/SAV-/TFS-/USD-...) account id, CRM id (ABC-1234), card customer ref (CX...), borrower ref (B...) or CIF.")


def _walk(doc, path, fn):
    """Apply fn(parent_dict, key) at `path`, fanning out over lists marked with []."""
    head, _, rest = path.partition(".")
    fan = head.endswith("[]")
    key = head[:-2] if fan else head
    if not isinstance(doc, dict) or key not in doc:
        return
    if not rest:
        fn(doc, key)
        return
    child = doc[key]
    if fan and isinstance(child, list):
        for item in child:
            _walk(item, rest, fn)
    elif not fan:
        _walk(child, rest, fn)


def _sort_key(item, field):
    v = item.get(field) if isinstance(item, dict) else None
    return (v is not None, str(v) if v is not None else "")


def trim(doc: dict) -> dict:
    """A trimmed deep copy of a customer document."""
    import copy
    out = copy.deepcopy(doc)
    for path in DROP:
        _walk(out, path, lambda parent, key: parent.pop(key, None))
    for path, (n, order_field) in KEEP_AT_MOST.items():
        def cut(parent, key, n=n, order_field=order_field):
            if isinstance(parent[key], list):
                parent[key] = sorted(parent[key], key=lambda i: _sort_key(i, order_field), reverse=True)[:n]
        _walk(out, path, cut)
    for path, n in TRUNCATE.items():
        def shorten(parent, key, n=n):
            if isinstance(parent[key], str) and len(parent[key]) > n:
                parent[key] = parent[key][:n] + "…"
        _walk(out, path, shorten)
    return out


def get_customer(ctx, key="") -> ToolResult:
    from c360.store import C360Store
    key = str(key or "").strip()
    try:
        store = C360Store(database=ctx.mongo_db())
    except Exception as e:      # noqa: BLE001
        return failure("get_customer", f"The customer store is not reachable ({type(e).__name__}).")
    if not key or store.query_for(key) is None:
        return failure("get_customer", f"'{key}' is {NOT_A_KEY}")
    try:
        doc = store.get(key)
    except Exception as e:      # noqa: BLE001
        return failure("get_customer", f"The customer store is not reachable ({type(e).__name__}).")
    if doc is None:
        return failure("get_customer", f"No customer found for '{key}'. Check the id; do not guess another one.")
    total_cases = len((doc.get("collections") or {}).get("cases") or [])
    trimmed = trim(doc)
    cases = (trimmed.get("collections") or {}).get("cases") or []
    warnings = []
    if total_cases > len(cases):
        warnings.append(f"Only the {len(cases)} most recent of {total_cases} cases are shown.")
    built = (doc.get("lineage") or {}).get("built_ts")
    return ToolResult(tool="get_customer", data=trimmed, row_count=1, warnings=warnings,
                      source_ids=[str(doc["_id"])] + [str(c.get("case_id")) for c in cases if c.get("case_id")],
                      detail=f"Customer record {doc['_id']}",
                      meta={"key": key, "record_built": built.isoformat() if isinstance(built, (dt.date, dt.datetime)) else None})
