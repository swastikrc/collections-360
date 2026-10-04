"""Optional LLM adjudication of the identity review queue (design step 04: "LLM scores the shortlist").

The probabilistic matcher settles ~99.96% of keys. For the rest, the LLM reads the source
record next to each shortlisted CRM record and gives a match percentage per candidate, with a
one-line reason. Verdicts go to MongoDB (collections360.match_llm_verdicts); linkage.py uses a verdict >= 90% to
link (status 'llm_link'), which stays visible in the review queue for a person to confirm.

Runs only when an Anthropic credential is available:  python -m c360.layer1.llm_adjudicator
"""
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from typing import List

from pydantic import BaseModel

from c360 import config


class CandidateVerdict(BaseModel):
    c360_id: str
    match_pct: float
    reason: str


class Adjudication(BaseModel):
    verdicts: List[CandidateVerdict]


SYSTEM = """You match customer records across a bank's source systems. Each system stores names,
dates of birth, phones and postal codes in its own format (initials only, surname first, masked
phone digits like 416555xxx21, dates as dd-mm-yyyy or yyyymmdd). Judge whether the SOURCE record
and each CANDIDATE are the same person. Consider typos, nicknames, day/month swaps and people who
moved. Relatives often share surname, address and phone but differ in given name or date of birth.
Return a match percentage (0-100) and a short reason for every candidate."""


def _candidate_records(con, item):
    ids = [c["crm_customer_id"] for c in (item["top_candidates"] or [])]
    if not ids:
        return []
    rows = con.execute(f"""
        SELECT pm.c360_id, c.crm_customer_id, c.first_name, c.middle_name, c.last_name, c.preferred_name,
               c.date_of_birth, c.primary_phone_raw, c.secondary_phone_raw, c.postal_code, c.city, c.province_code_std
        FROM curated.customers c JOIN golden.crm_person_map pm USING (crm_customer_id)
        WHERE c.crm_customer_id IN ({','.join('?' * len(ids))})""", ids).fetchall()
    cols = ["c360_id", "crm_customer_id", "first_name", "middle_name", "last_name", "preferred_name",
            "date_of_birth", "primary_phone", "secondary_phone", "postal_code", "city", "province"]
    return [dict(zip(cols, [str(v) if v is not None else None for v in r])) for r in rows]


def _ask(client, item, candidates):
    prompt = json.dumps({
        "source_system": item["src_system"],
        "source_record": {"name": item["name_raw"], "dob": item["dob_raw"], "phone": item["phone_raw"],
                          "postal": item["postal_raw"]},
        "candidates": candidates,
        "matcher_evidence": [{"c360_id": c["c360_id"], "model_probability": c["p"], "evidence": c["evidence"]}
                             for c in item["top_candidates"]],
    }, default=str)
    kwargs = dict(model=config.LLM_MODEL, max_tokens=2000, system=SYSTEM,
                  output_config={"effort": "low"},
                  messages=[{"role": "user", "content": prompt}], output_format=Adjudication)
    try:  # server-side refusal fallback (opt-in); plain call if this SDK build lacks it
        resp = client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        resp = client.messages.parse(**kwargs)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        return []
    return resp.parsed_output.verdicts


def run(max_calls=None, workers=8):
    import anthropic
    client = anthropic.Anthropic()
    con = config.connect()
    from c360.layer1.linkage import ensure_review_tables
    ensure_review_tables(con)
    items = con.execute(f"""
        SELECT q.src_system, q.src_ref, q.top_candidates, q.name_raw, q.dob_raw, q.phone_raw, q.postal_raw
        FROM golden.match_review_queue q
        WHERE q.src_system IN ('cards', 'loans', 'core') AND q.top_candidates IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM golden.match_llm_verdicts v WHERE v.src_system = q.src_system AND v.src_ref = q.src_ref)  -- synced from MongoDB
        LIMIT {int(max_calls or config.LLM_MAX_CALLS)}""").fetchall()
    cols = ["src_system", "src_ref", "top_candidates", "name_raw", "dob_raw", "phone_raw", "postal_raw"]
    items = [dict(zip(cols, r)) for r in items]
    work = [(it, _candidate_records(con, it)) for it in items]

    def one(pair):
        it, cands = pair
        try:
            return it, _ask(client, it, cands)
        except anthropic.APIError as e:
            print(f"  LLM error on {it['src_system']}/{it['src_ref']}: {e}")
            return it, []

    now = dt.datetime.now()
    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for it, verdicts in ex.map(one, work):
            valid = {c["c360_id"] for c in it["top_candidates"]}
            rows += [(it["src_system"], it["src_ref"], v.c360_id, max(0.0, min(100.0, v.match_pct)), v.reason,
                      config.LLM_MODEL, now) for v in verdicts if v.c360_id in valid]
    if rows:
        from c360 import mongo
        keys = ["src_system", "src_ref", "c360_id", "llm_match_pct", "llm_reason", "model", "verdict_ts"]
        mongo.db().match_llm_verdicts.insert_many([dict(zip(keys, r)) for r in rows])
    print(f"  LLM adjudication: {len(items)} review items, {len(rows)} candidate verdicts")
    con.close()


if __name__ == "__main__":
    run()
