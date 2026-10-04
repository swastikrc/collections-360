"""Where Layer 3 reads data from.

`MongoSource` (C360_L3_SOURCE=mongo) reads Layer 1's golden Collections 360 document from MongoDB and
enriches it from the local store. `LocalSource` assembles a Collections 360 customer document from the Layer 3 DuckDB store
(`build_local.py`), in the same shape as Layer 1's MongoDB document (see
outputs/layer1/sample_document_CS-2026-614370.json), so every engine works unchanged when
`ApiSource` reads the real document from the Layer 2 API (`GET /customers/{key}`).

Portfolio aggregates and the worklist are not part of the Layer 2 contract, so they always come
from the local store.
"""
import datetime as dt
import functools
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import duckdb

from c360.layer3 import config
from c360.layer3.util import jsonable

SNAP = config.SNAPSHOT_DATE
OPEN = "outcome IS NULL"            # open case = no outcome yet (same rule as the benchmark's gold SQL)

# Industry (NAICS 2-digit sector, Statistics Canada names)
NAICS = {"11": "Agriculture, forestry, fishing", "21": "Mining, oil and gas", "22": "Utilities", "23": "Construction",
         "31": "Manufacturing", "32": "Manufacturing", "33": "Manufacturing", "41": "Wholesale trade",
         "44": "Retail trade", "45": "Retail trade", "48": "Transportation and warehousing",
         "49": "Transportation and warehousing", "51": "Information, culture, media", "52": "Finance and insurance",
         "53": "Real estate and rental", "54": "Professional, scientific, technical", "55": "Management of companies",
         "56": "Administrative and support services", "61": "Educational services", "62": "Health care and social assistance",
         "71": "Arts, entertainment, recreation", "72": "Accommodation and food services", "81": "Other services",
         "91": "Public administration"}
_NAICS_SQL = "CASE cu.industry_naics2 " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in NAICS.items()) + " END"
SECTOR_LABEL = {"sales_service": "Sales and service", "manufacturing_utilities": "Manufacturing and utilities",
                "trades_transport_equipment": "Trades, transport, equipment", "health": "Health",
                "education_law_social_government": "Education, law, social, government",
                "natural_applied_sciences": "Science and engineering", "business_finance_admin": "Business, finance, admin",
                "natural_resources_agriculture": "Natural resources and agriculture",
                "art_culture_recreation_sport": "Arts, culture, recreation, sport"}
_SECTOR_SQL = ("CASE " + " ".join(f"WHEN cu.occupation_category = '{k}' THEN '{v}'" for k, v in SECTOR_LABEL.items())
               + " WHEN cu.employment_status IN ('retired', 'student', 'unemployed') THEN 'No job: ' || cu.employment_status"
               + " ELSE NULL END")
# Ways to group customers in the Groups view: label, SQL expression on customers (cu)
GROUP_BY = {
    "sector": ("Job sector", _SECTOR_SQL),
    "industry": ("Industry", _NAICS_SQL),
    "employer": ("Employer", "cu.employer_name"),
    "area": ("Postal area", "upper(cu.fsa)"),
    "city": ("City", "cu.city || ', ' || cu.province_code"),
    "province": ("Province", "cu.province_code"),
}
JOB_LOSS = "('job_loss', 'reduced_income', 'ei_waiting', 'strike_lockout')"   # hardship reasons that mean lost income from work


def postal_area(text):
    """'M5V 2T6', 'm5v2t6' or 'M5V' -> 'M5V' (the forward sortation area); None if it is not a Canadian postal code."""
    m = re.match(r"^\s*([A-Za-z]\d[A-Za-z])", text or "")
    return m.group(1).upper() if m else None

KEY_PATTERNS = [
    (r"^C360-([A-Z]{3}-\d{4})$", "crm"),
    (r"^CS-\d{4}-\d+$", "case"),
    (r"^[A-Z]{3}-\d{4}$", "crm"),
    (r"^(C|L|CHQ|SAV|TFS|USD|OD)-\d+$", "account"),
]


class NotFound(Exception):
    pass


class BadKey(ValueError):
    pass


def key_kind(key):
    key = (key or "").strip().upper()
    for pat, kind in KEY_PATTERNS:
        m = re.match(pat, key)
        if m:
            return kind, (m.group(1) if kind == "crm" and key.startswith("C360-") else key)
    raise BadKey(f"'{key}' is not a recognised customer, case or account id")


def _rows(cur, sql, params=()):
    res = cur.execute(sql, params)
    cols = [d[0] for d in res.description]
    return [dict(zip(cols, r)) for r in res.fetchall()]


def _one(cur, sql, params=()):
    r = _rows(cur, sql, params)
    return r[0] if r else None


class LocalSource:
    name = "local"

    def __init__(self, path=None):
        path = path or config.l3_db()
        if not path.exists():
            raise FileNotFoundError(f"Layer 3 store not found at {path}. Run: python -m c360.layer3.data.build_local")
        self.con = duckdb.connect(str(path), read_only=True)
        self._lock = threading.Lock()
        self.meta = _one(self.con.cursor(), "SELECT * FROM meta")

    def cur(self):
        with self._lock:
            return self.con.cursor()

    @functools.lru_cache(maxsize=1)
    def has_work_columns(self):
        """Stores built before the job / postal-area columns were added cannot group; rebuild with build_local."""
        cols = {r["column_name"] for r in _rows(self.cur(), "SELECT column_name FROM information_schema.columns WHERE table_name = 'customers'")}
        return {"fsa", "occupation_category", "industry_naics2", "employer_name"} <= cols

    # ---- keys ---------------------------------------------------------------------------------
    def resolve_crm(self, key):
        kind, k = key_kind(key)
        cur = self.cur()
        if kind == "crm":
            return k
        if kind == "case":
            r = _one(cur, "SELECT coll_customer_ref FROM cases WHERE case_id = ?", [k])
        else:
            r = _one(cur, "SELECT coll_customer_ref FROM case_accounts WHERE account_id = ? LIMIT 1", [k])
        if not r:
            raise NotFound(f"No customer found for '{k}'")
        return r["coll_customer_ref"]

    # ---- the customer document --------------------------------------------------------------
    def customer_doc(self, key):
        crm = self.resolve_crm(key)
        cur = self.cur()
        c = _one(cur, "SELECT * FROM customers WHERE crm_customer_id = ?", [crm]) or {"crm_customer_id": crm}
        cases = _rows(cur, "SELECT * FROM cases WHERE coll_customer_ref = ? ORDER BY case_open_date DESC", [crm])
        if not cases and len(c) == 1:
            raise NotFound(f"No customer found for '{key}'")
        case_ids = [x["case_id"] for x in cases]
        ph = ",".join("?" * len(case_ids)) or "NULL"
        promises = _rows(cur, f"SELECT * FROM promises WHERE case_id IN ({ph}) ORDER BY ptp_created_ts DESC", case_ids)
        offers = _rows(cur, f"SELECT * FROM offers WHERE case_id IN ({ph}) ORDER BY offer_ts DESC", case_ids)
        acct_ids = [r["account_id"] for r in _rows(cur, f"SELECT DISTINCT account_id FROM case_accounts WHERE case_id IN ({ph})", case_ids)]
        ah = ",".join("?" * len(acct_ids)) or "NULL"
        cards = _rows(cur, f"SELECT * FROM cards WHERE account_id IN ({ah})", acct_ids)
        loans = _rows(cur, f"SELECT * FROM loans WHERE account_id IN ({ah})", acct_ids)
        deposits = _rows(cur, f"SELECT * FROM deposits WHERE account_id IN ({ah})", acct_ids)
        notes = _rows(cur, f"SELECT note_id, note_ts, note_type, agent_id, case_id, note_text FROM notes "
                           f"WHERE case_id IN ({ph}) ORDER BY note_ts DESC LIMIT 8", case_ids)
        calls = _rows(cur, f"SELECT transcript_id, contact_id, case_id, call_start_ts, duration_sec, direction, language, "
                           f"voice_sample_id, agent_id FROM transcripts WHERE case_id IN ({ph}) ORDER BY call_start_ts DESC LIMIT 10", case_ids)
        contacts = _one(cur, f"""
            SELECT count(*) FILTER (WHERE direction = 'outbound' AND channel <> 'system') AS outbound_attempts_total,
                   count(*) FILTER (WHERE direction = 'outbound' AND channel <> 'system' AND contact_ts_local >= ?::DATE - 29) AS outbound_attempts_30d,
                   count(*) FILTER (WHERE direction = 'outbound' AND channel = 'call' AND contact_ts_local >= ?::DATE - 6) AS outbound_calls_7d,
                   count(*) FILTER (WHERE rpc_flag) AS rpc_total,
                   count(*) FILTER (WHERE rpc_flag AND contact_ts_local >= ?::DATE - 29) AS rpc_30d,
                   count(*) FILTER (WHERE direction = 'inbound') AS inbound_contacts_total,
                   count(*) FILTER (WHERE dispute_raised_flag) AS disputes_raised,
                   count(*) FILTER (WHERE complaint_raised_flag) AS complaints_raised,
                   count(*) FILTER (WHERE hardship_mentioned_flag) AS hardship_mentions,
                   count(*) FILTER (WHERE cease_request_flag) AS cease_requests,
                   max(contact_ts_local) FILTER (WHERE channel <> 'system') AS last_contact_ts,
                   max(contact_ts_local) FILTER (WHERE rpc_flag) AS last_rpc_ts,
                   arg_max(channel, contact_ts_local) FILTER (WHERE channel <> 'system') AS last_contact_channel,
                   arg_max(outcome_code, contact_ts_local) FILTER (WHERE channel <> 'system') AS last_outcome_code,
                   mode(contact_hour_local) FILTER (WHERE rpc_flag) AS most_common_rpc_hour
            FROM contacts WHERE case_id IN ({ph})""", [SNAP, SNAP, SNAP] + case_ids) or {}
        recent_contacts = _rows(cur, f"""
            SELECT contact_id, case_id, contact_ts_local, direction, channel, outcome_code, outcome_desc, rpc_flag,
                   ptp_made_flag, payment_amount, hardship_mentioned_flag, dispute_raised_flag, cease_request_flag,
                   within_permitted_hours_flag, transcript_id
            FROM contacts WHERE case_id IN ({ph}) AND channel <> 'system' ORDER BY contact_ts_local DESC LIMIT 12""", case_ids)
        scores = _rows(cur, "SELECT model_name, model_version, score_date, score, score_band, slot_label, recommended_action, "
                            "top_reason_1_text, top_reason_2_text, top_reason_3_text FROM model_scores WHERE crm_customer_id = ?", [crm])
        agents = {a["agent_id"]: a for a in _rows(cur, "SELECT agent_id, site, role, languages, vulnerable_customer_certified, "
                                                       "skill_hardship FROM agents")} if cases else {}

        doc = {
            "_id": f"C360-{crm}",
            "master_crm_id": crm,
            "identity": {"crm_ids": [crm], "link_source": "layer3.local",
                         "note": "Linked inside the collections domain by CRM id and case account ids; "
                                 "identity resolution across systems comes from Layer 1 (C360_L3_SOURCE=api)."},
            "profile": {k: c.get(k) for k in ("first_name", "last_name", "preferred_name", "segment", "tenure_months",
                                               "customer_since_date")} | {"crm_status": c.get("customer_status")},
            "work": {"sector": SECTOR_LABEL.get(c.get("occupation_category")) or c.get("occupation_category"),
                     "industry": NAICS.get(str(c.get("industry_naics2") or "")), "employer": c.get("employer_name"),
                     "employment_status": c.get("employment_status")},
            "contact": {
                "city": c.get("city"), "province_code": c.get("province_code"), "time_zone": c.get("time_zone"),
                "postal_area": (c.get("fsa") or "").upper() or None,
                "preferred_language": c.get("preferred_language"), "preferred_channel": c.get("preferred_channel"),
                "preferred_contact_window": [c.get("preferred_contact_window_start"), c.get("preferred_contact_window_end")],
                "permitted_call_window_local": [c.get("permitted_call_start_local") or "07:00", c.get("permitted_call_end_local") or "21:00"],
                "sunday_contact_permitted": c.get("sunday_contact_permitted"),
            },
            "servicing_constraints": {
                "consent_call": c.get("consent_call"), "consent_autodialer": c.get("consent_autodialer"),
                "consent_sms": c.get("consent_sms"), "consent_email": c.get("consent_email"),
                "voicemail_permitted": c.get("voicemail_permitted"), "contact_at_work_permitted": c.get("contact_at_work_permitted"),
                "internal_do_not_call": c.get("internal_do_not_call_flag"), "cease_communication": c.get("cease_communication_flag"),
                "third_party_authorised": c.get("third_party_authorised_flag"),
                "credit_counselling_agency": c.get("credit_counselling_agency_flag"),
                "power_of_attorney": c.get("power_of_attorney_flag"), "legal_representative": c.get("legal_representative_flag"),
                "deceased": c.get("deceased_flag"), "bankruptcy": c.get("bankruptcy_flag"),
                "consumer_proposal": c.get("consumer_proposal_flag"), "fraud_victim": c.get("fraud_victim_flag"),
                "military_or_reserve": c.get("military_or_reserve_flag"),
                "national_dncl_telemarketing_only": c.get("national_dncl_flag"),
            },
            "financial_profile": {k: c.get(k) for k in ("employment_status", "pay_frequency", "declared_annual_income",
                                                         "income_verified_flag", "income_source", "housing_status",
                                                         "monthly_housing_cost", "internal_risk_grade")},
            "accounts": {"cards": cards, "loans": [dict(l, product_code=l.get("product_code")) for l in loans],
                         "deposits": deposits},
            "collections": {
                "cases": [self._case(x, promises, offers, agents) for x in cases],
                "open_cases": sum(1 for x in cases if x.get("outcome") is None),
                "contacts": contacts,
                "recent_contacts": recent_contacts,
                "recent_notes": notes,
                "call_transcripts": calls,
            },
            "model_scores": scores,
            "lineage": {"source": "layer3.local", "snapshot_date": SNAP, "built_ts": (self.meta or {}).get("built_ts")},
        }
        doc["exposure"] = _exposure(cards, loans, deposits, cases)
        doc["hardship_indicators"] = _hardship(cases, contacts, deposits)
        return jsonable(doc)

    @staticmethod
    def _case(x, promises, offers, agents):
        p = [r for r in promises if r["case_id"] == x["case_id"]]
        o = [r for r in offers if r["case_id"] == x["case_id"]]
        latest = p[0] if p else None
        out = dict(x)
        out["account_ids"] = re.findall(r"[A-Z]{1,3}-\d+", x.get("account_ids") or "") or [x.get("primary_account_id")]
        out["is_open"] = x.get("outcome") is None
        out["promises"] = {
            "total": len(p), "kept": sum(r["ptp_status"] == "kept" for r in p),
            "partially_kept": sum(r["ptp_status"] == "partially_kept" for r in p),
            "broken": sum(r["ptp_status"] == "broken" for r in p), "open": sum(r["ptp_status"] == "open" for r in p),
            "amount_paid": round(sum(r.get("amount_paid_against") or 0 for r in p), 2),
            "latest": {"ptp_id": latest["ptp_id"], "amount": latest["ptp_amount"], "due_date": latest["ptp_due_date"],
                       "status": latest["ptp_status"], "created_ts": latest["ptp_created_ts"], "channel": latest["ptp_channel"]}
            if latest else None,
            "items": [{k: r.get(k) for k in ("ptp_id", "ptp_amount", "ptp_due_date", "ptp_status", "ptp_created_ts",
                                             "amount_paid_against", "paid_date", "ptp_channel", "grace_days")} for r in p[:10]],
        }
        out["offers"] = [{k: r.get(k) for k in ("offer_id", "program_id", "offer_ts", "offered_by", "proposed_payment_amount",
                                                "duration_months", "customer_response", "plan_status", "plan_payments_due",
                                                "plan_payments_made")} for r in o]
        ag = agents.get(x.get("assigned_agent_id"))
        out["assigned_agent"] = ag
        return out

    # ---- transcripts ------------------------------------------------------------------------
    def transcript(self, transcript_id):
        r = _one(self.cur(), "SELECT * FROM transcripts WHERE transcript_id = ?", [str(transcript_id)])
        if not r:
            raise NotFound(f"No transcript '{transcript_id}'")
        path = config.maple_data() / "files" / r["file_path"]
        if not path.exists():
            raise NotFound(f"Transcript file missing for '{transcript_id}' ({r['file_path']})")
        return json.loads(path.read_text(encoding="utf-8"))

    def case_row(self, case_id):
        return _one(self.cur(), "SELECT * FROM cases WHERE case_id = ?", [case_id])

    def contact_row(self, contact_id):
        return _one(self.cur(), "SELECT * FROM contacts WHERE contact_id = ?", [contact_id])

    # ---- portfolio --------------------------------------------------------------------------
    @functools.lru_cache(maxsize=1)
    def portfolio(self):
        cur = self.cur()
        tiles = _one(cur, f"""
            SELECT count(*) AS open_cases, sum(total_overdue) AS overdue_total, avg(current_dpd) AS avg_dpd,
                   avg(CASE WHEN hardship_flag THEN 1 ELSE 0 END) AS hardship_share,
                   count(*) FILTER (WHERE insolvency_hold_flag OR deceased_hold_flag) AS on_hold,
                   count(*) FILTER (WHERE vulnerable_customer_flag) AS vulnerable,
                   count(*) FILTER (WHERE dispute_flag) AS disputes_open
            FROM cases WHERE {OPEN}""")
        ptp = _one(cur, """
            SELECT avg(CASE WHEN ptp_status = 'kept' THEN 1.0 ELSE 0 END) AS kept_rate,
                   avg(CASE WHEN ptp_status = 'broken' THEN 1.0 ELSE 0 END) AS broken_rate, count(*) AS resolved
            FROM promises WHERE ptp_status IN ('kept','partially_kept','broken')
              AND ptp_due_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-28'""")
        rpc = _one(cur, """
            SELECT avg(CASE WHEN rpc_flag THEN 1.0 ELSE 0 END) AS rpc_rate, count(*) AS attempts
            FROM contacts WHERE channel = 'call' AND direction = 'outbound'
              AND contact_ts_local >= TIMESTAMP '2026-09-01' AND contact_ts_local < TIMESTAMP '2026-09-29'""")
        due = _one(cur, """SELECT count(*) AS promises_due_7d, sum(ptp_amount) AS amount_due_7d FROM promises
                           WHERE ptp_status = 'open' AND ptp_due_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-05'""")
        buckets = _rows(cur, f"""
            SELECT current_bucket AS bucket, count(*) AS cases, sum(total_overdue) AS overdue
            FROM cases WHERE {OPEN} GROUP BY 1""")
        order = ["current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "180+"]
        buckets.sort(key=lambda r: order.index(r["bucket"]) if r["bucket"] in order else 99)
        queues = _rows(cur, f"""
            SELECT queue, count(*) AS cases, avg(current_dpd) AS avg_dpd, sum(total_overdue) AS overdue,
                   avg(CASE WHEN hardship_flag THEN 1.0 ELSE 0 END) AS hardship_share,
                   count(*) FILTER (WHERE promises_open > 0) AS with_open_promise
            FROM cases WHERE {OPEN} GROUP BY 1 ORDER BY cases DESC""")
        daily = _rows(cur, """
            SELECT CAST(contact_ts_local AS DATE) AS day, count(*) AS calls, count(*) FILTER (WHERE rpc_flag) AS rpc
            FROM contacts WHERE channel = 'call' AND direction = 'outbound'
              AND contact_ts_local >= TIMESTAMP '2026-08-29' AND contact_ts_local < TIMESTAMP '2026-09-29'
            GROUP BY 1 ORDER BY 1""")
        kept_by_month = _rows(cur, """
            SELECT strftime(ptp_due_date, '%Y-%m') AS month,
                   avg(CASE WHEN ptp_status = 'kept' THEN 1.0 ELSE 0 END) AS kept_rate, count(*) AS promises
            FROM promises WHERE ptp_status IN ('kept','partially_kept','broken')
              AND ptp_due_date BETWEEN DATE '2025-10-01' AND DATE '2026-09-28' GROUP BY 1 ORDER BY 1""")
        events = _rows(cur, "SELECT * FROM ops_events ORDER BY start_ts DESC")
        hardship_reasons = _rows(cur, f"""
            SELECT coalesce(hardship_reason, 'not recorded') AS reason, count(*) AS cases
            FROM cases WHERE {OPEN} AND hardship_flag GROUP BY 1 ORDER BY 2 DESC""")
        products = _rows(cur, f"""
            SELECT primary_product AS product, count(*) AS cases, sum(total_overdue) AS overdue
            FROM cases WHERE {OPEN} GROUP BY 1 ORDER BY 2 DESC""")
        due_by_day = _rows(cur, """
            SELECT ptp_due_date AS day, count(*) AS promises, sum(ptp_amount) AS amount FROM promises
            WHERE ptp_status = 'open' AND ptp_due_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-11' GROUP BY 1 ORDER BY 1""")
        contact_outcomes = _rows(cur, """
            SELECT coalesce(outcome_desc, outcome_code) AS outcome, count(*) AS contacts FROM contacts
            WHERE channel = 'call' AND direction = 'outbound'
              AND contact_ts_local >= TIMESTAMP '2026-09-01' AND contact_ts_local < TIMESTAMP '2026-09-29'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
        sectors = _rows(cur, f"""
            SELECT {_SECTOR_SQL} AS sector, count(*) AS cases,
                   avg(CASE WHEN c.hardship_flag THEN 1.0 ELSE 0 END) AS hardship_share
            FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
            WHERE c.{OPEN} GROUP BY 1 HAVING sector IS NOT NULL ORDER BY 2 DESC""") if self.has_work_columns() else []
        return jsonable({"snapshot_date": SNAP, "tiles": tiles | ptp | rpc | due, "buckets": buckets, "queues": queues,
                         "daily_calls": daily, "kept_by_month": kept_by_month, "events": events,
                         "hardship_reasons": hardship_reasons, "products": products, "due_by_day": due_by_day,
                         "contact_outcomes": contact_outcomes, "sectors": sectors,
                         "definitions": {
                             "open_case": "collections_cases.outcome is null",
                             "kept_rate": "kept / (kept + partially kept + broken), promises due 1-28 Sep 2026 (PRC-COLL-010 §3.3)",
                             "rpc_rate": "right-party contacts / outbound call attempts, 1-28 Sep 2026",
                         }})

    # ---- worklist ---------------------------------------------------------------------------
    def worklist(self, queue=None, bucket=None, flag=None, q=None, limit=60, group_by=None, group=None, area=None):
        where, params = [OPEN], []
        if group_by in GROUP_BY and group:
            where.append(f"{GROUP_BY[group_by][1]} = ?"), params.append(group)
        if area and postal_area(area):
            where.append("upper(cu.fsa) = ?"), params.append(postal_area(area))
        if queue:
            where.append("c.queue = ?"), params.append(queue)
        if bucket:
            where.append("c.current_bucket = ?"), params.append(bucket)
        if flag == "hardship":
            where.append("hardship_flag")
        elif flag == "promise_due":
            where.append("current_ptp_due_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-01'")
        elif flag == "broken":
            where.append("promises_broken >= 2")
        elif flag == "dispute":
            where.append("dispute_flag")
        elif flag == "vulnerable":
            where.append("vulnerable_customer_flag")
        elif flag == "hold":
            where.append("(insolvency_hold_flag OR deceased_hold_flag OR cease_contact_flag OR third_party_rep_flag)")
        if q:
            where.append("(c.case_id ILIKE ? OR c.coll_customer_ref ILIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        # priority: how late, how much, a promise due now, repeated broken promises, hardship without a plan
        sql = f"""
            SELECT c.case_id, c.coll_customer_ref AS crm_id, cu.first_name, cu.last_name, c.queue, c.current_bucket,
                   c.current_dpd, c.total_overdue, c.primary_product, c.promises_broken, c.promises_open,
                   c.current_ptp_amount, c.current_ptp_due_date, c.hardship_flag, c.hardship_program_accepted,
                   c.dispute_flag, c.vulnerable_customer_flag, c.cease_contact_flag, c.insolvency_hold_flag,
                   c.deceased_hold_flag, c.third_party_rep_flag, c.next_scheduled_action, c.last_contact_ts,
                   c.hardship_reason, {_SECTOR_SQL} AS job_sector, upper(cu.fsa) AS postal_area, cu.employer_name AS employer,
                   ( least(c.current_dpd, 180) / 1.8
                   + least(ln(1 + coalesce(c.total_overdue, 0)) * 4, 40)
                   + CASE WHEN c.current_ptp_due_date BETWEEN DATE '2026-09-28' AND DATE '2026-10-01' THEN 25 ELSE 0 END
                   + least(coalesce(c.promises_broken, 0), 3) * 6
                   + CASE WHEN c.hardship_flag AND NOT coalesce(c.hardship_program_accepted, false) THEN 15 ELSE 0 END
                   - CASE WHEN c.insolvency_hold_flag OR c.deceased_hold_flag THEN 200 ELSE 0 END ) AS priority
            FROM cases c LEFT JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
            WHERE {' AND '.join(where)} ORDER BY priority DESC LIMIT {int(limit)}"""
        return jsonable(_rows(self.cur(), sql, params))

    # ---- groups (job sector, industry, employer, postal area, city, province) -----------------------
    def segments(self, by="sector", queue=None, flag=None, area=None, limit=25):
        if by not in GROUP_BY:
            raise BadKey(f"group by one of: {', '.join(GROUP_BY)}")
        label, expr = GROUP_BY[by]
        where, params = [f"c.{OPEN}"], []
        if queue:
            where.append("c.queue = ?"), params.append(queue)
        if flag == "hardship":
            where.append("c.hardship_flag")
        if area and postal_area(area):
            where.append("upper(cu.fsa) = ?"), params.append(postal_area(area))
        sql = f"""
            SELECT {expr} AS grp, count(*) AS cases, count(DISTINCT c.coll_customer_ref) AS customers,
                   sum(c.total_overdue) AS overdue, avg(c.current_dpd) AS avg_days_late,
                   avg(CASE WHEN c.hardship_flag THEN 1.0 ELSE 0 END) AS hardship_share,
                   avg(CASE WHEN c.hardship_reason IN {JOB_LOSS} THEN 1.0 ELSE 0 END) AS lost_income_share,
                   avg(CASE WHEN c.promises_broken > 0 THEN 1.0 ELSE 0 END) AS broken_promise_share,
                   sum(c.promises_kept) / nullif(sum(c.promises_kept + c.promises_broken), 0) AS kept_rate,
                   count(*) FILTER (WHERE c.case_open_date >= DATE '{SNAP}' - 60) AS opened_60d
            FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
            WHERE {' AND '.join(where)}
            GROUP BY 1 HAVING grp IS NOT NULL ORDER BY cases DESC LIMIT {int(limit)}"""
        cur = self.cur()
        rows = _rows(cur, sql, params)
        total = _one(cur, f"""SELECT count(*) AS cases, sum(c.total_overdue) AS overdue,
                                     avg(CASE WHEN c.hardship_flag THEN 1.0 ELSE 0 END) AS hardship_share,
                                     avg(CASE WHEN c.hardship_reason IN {JOB_LOSS} THEN 1.0 ELSE 0 END) AS lost_income_share,
                                     avg(c.current_dpd) AS avg_days_late
                              FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
                              WHERE {' AND '.join(where)}""", params)
        return jsonable({"by": by, "label": label, "groups": rows, "total": total,
                         "area": postal_area(area) if area else None,
                         "options": {k: v[0] for k, v in GROUP_BY.items()}})

    @functools.lru_cache(maxsize=1)
    def layoff_alerts(self, limit=8):
        """Employers where several customers opened a case recently because they lost work income: a hint that the
        employer cut jobs, so hardship options can be offered early. A prompt for a person, not a decision."""
        return jsonable(_rows(self.cur(), f"""
            SELECT cu.employer_name AS employer, {_SECTOR_SQL} AS job_sector,
                   count(*) FILTER (WHERE c.hardship_reason IN {JOB_LOSS} AND c.case_open_date >= DATE '{SNAP}' - 120) AS lost_income_recent,
                   count(*) AS open_cases, list(DISTINCT upper(cu.fsa)) AS postal_areas
            FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
            WHERE c.{OPEN} AND cu.employer_name IS NOT NULL
            GROUP BY 1, 2 HAVING lost_income_recent >= 2
            ORDER BY lost_income_recent DESC, open_cases DESC LIMIT {int(limit)}"""))

    def peers(self, case_id):
        """Other customers like this one: same employer; same job sector in the same postal area; same postal area.
        Case ids and figures only (no other customer's name or details)."""
        cur = self.cur()
        me = _one(cur, f"""SELECT c.coll_customer_ref AS crm, cu.employer_name, upper(cu.fsa) AS area, {_SECTOR_SQL} AS sector,
                                  cu.occupation_category
                           FROM cases c LEFT JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
                           WHERE c.case_id = ?""", [case_id])
        if not me:
            raise NotFound(f"No case '{case_id}'")

        def group(cond, params, title):
            stats = _one(cur, f"""
                SELECT count(*) AS open_cases, count(DISTINCT c.coll_customer_ref) AS customers,
                       count(*) FILTER (WHERE c.hardship_flag) AS hardship,
                       count(*) FILTER (WHERE c.hardship_reason IN {JOB_LOSS}) AS lost_income,
                       count(*) FILTER (WHERE c.case_open_date >= DATE '{SNAP}' - 60) AS opened_60d,
                       avg(c.current_dpd) AS avg_days_late
                FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
                WHERE c.{OPEN} AND c.coll_customer_ref <> ? AND {cond}""", [me["crm"]] + params)
            cases = _rows(cur, f"""
                SELECT c.case_id, c.current_dpd AS days_late, c.total_overdue AS overdue, c.hardship_reason, c.case_open_date
                FROM cases c JOIN customers cu ON cu.crm_customer_id = c.coll_customer_ref
                WHERE c.{OPEN} AND c.coll_customer_ref <> ? AND {cond}
                ORDER BY (c.hardship_reason IN {JOB_LOSS}) DESC, c.case_open_date DESC LIMIT 6""", [me["crm"]] + params)
            return {"title": title, **(stats or {}), "cases": cases}

        out = {"case_id": case_id, "employer": me["employer_name"], "sector": me["sector"], "area": me["area"], "groups": []}
        if me["employer_name"]:
            out["groups"].append(dict(group("cu.employer_name = ?", [me["employer_name"]], f"Same employer ({me['employer_name']})"),
                                      key="employer", group_by="employer", group=me["employer_name"]))
        if me["occupation_category"] and me["area"]:
            out["groups"].append(dict(group("cu.occupation_category = ? AND upper(cu.fsa) = ?", [me["occupation_category"], me["area"]],
                                            f"Same job sector ({me['sector']}) in postal area {me['area']}"),
                                      key="sector_area", group_by="sector", group=me["sector"], area=me["area"]))
        if me["area"]:
            out["groups"].append(dict(group("upper(cu.fsa) = ?", [me["area"]], f"Same postal area ({me['area']})"),
                                      key="area", group_by="area", group=me["area"]))
        emp = next((g for g in out["groups"] if g["key"] == "employer"), None)
        if emp and emp.get("lost_income", 0) >= 2:
            out["signal"] = (f"{emp['lost_income']} other customers at {me['employer_name']} are behind because they lost work "
                             "income. The employer may have cut jobs: check this customer for hardship before asking for payment.")
        return jsonable(out)

    @functools.lru_cache(maxsize=1)
    def program_names(self):
        return {r["program_id"]: r["program_name"] for r in _rows(self.cur(), "SELECT DISTINCT program_id, program_name FROM hardship_programs")}

    def queues(self):
        return [r["queue"] for r in _rows(self.cur(), f"SELECT DISTINCT queue FROM cases WHERE {OPEN} ORDER BY 1")]

    def agent(self, agent_id):
        return jsonable(_one(self.cur(), "SELECT * FROM agents WHERE agent_id = ?", [agent_id]))


class ApiSource:
    """Reads customer documents from the Layer 2 HTTP API (SPEC_FRONTEND.md §4.4); everything
    else falls back to the local store."""
    name = "api"

    def __init__(self, base_url=None, local=None):
        self.base = (base_url or config.api_base_url()).rstrip("/")
        self.local = local

    def customer_doc(self, key):
        key_kind(key)
        req = urllib.request.Request(f"{self.base}/customers/{urllib.parse.quote(key)}",
                                     headers={"X-C360-User": "layer3", "X-C360-Role": "service"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(f"No customer found for '{key}'") from e
            if e.code == 400:
                raise BadKey(f"'{key}' is not a recognised id") from e
            raise

    def __getattr__(self, item):
        if self.local is None:
            raise AttributeError(item)
        return getattr(self.local, item)


class MongoSource:
    """The Collections 360 golden record from Layer 1's MongoDB (`c360.store.C360Store`), enriched with the
    operational detail the golden record does not carry (promise history, recent contacts, assigned agent, full
    case columns) from the local store, matched by case id and account id.

    The MongoDB document wins wherever both have a value: its identity (merged CRM records, links from the card,
    loan and core-banking systems), recomputed delinquency buckets and account list are Layer 1's. Portfolio
    aggregates and the worklist come from the local store (they are not part of the customer document)."""
    name = "mongo"

    def __init__(self, local, store=None):
        if local is None:
            raise FileNotFoundError("The mongo source needs the local store for enrichment. "
                                    "Run: python -m c360.layer3.data.build_local")
        from c360.store import C360Store
        self.local = local
        self.store = store or C360Store()

    def customer_doc(self, key):
        try:
            key_kind(key)
        except BadKey:                                  # Layer 1 also resolves card refs, borrower refs and CIFs
            if self.store.query_for(key) is None:
                raise
        doc = self.store.get(key)
        if doc is None:
            raise NotFound(f"No customer found for '{key}'")
        golden = jsonable(doc)
        enrich = None
        for crm in (golden.get("identity") or {}).get("crm_ids") or []:
            try:
                part = self.local.customer_doc(crm)
            except (NotFound, BadKey):
                continue
            enrich = part if enrich is None else _merge_cases_into(enrich, part)
        merged = _merge(enrich, golden) if enrich else golden
        merged.setdefault("lineage", {})["layer3_source"] = "mongo (Layer 1 golden record) + local enrichment"
        return merged

    def __getattr__(self, item):
        return getattr(self.local, item)


_LIST_KEYS = {("collections", "cases"): "case_id", ("accounts", "cards"): "account_id",
              ("accounts", "loans"): "account_id", ("accounts", "deposits"): "account_id"}


def _merge(base, top, path=()):
    """Deep merge: `top` (the golden record) wins; lists of cases / accounts are merged item by item on their id."""
    if isinstance(base, dict) and isinstance(top, dict):
        out = dict(base)
        for k, v in top.items():
            out[k] = _merge(base.get(k), v, path + (k,)) if k in base else v
        return out
    if isinstance(base, list) and isinstance(top, list) and path in _LIST_KEYS:
        key = _LIST_KEYS[path]
        by_id = {b.get(key): b for b in base if isinstance(b, dict)}
        merged = [_merge(by_id.get(t.get(key), {}), t, path + ("item",)) for t in top if isinstance(t, dict)]
        seen = {t.get(key) for t in top if isinstance(t, dict)}
        return merged + [b for b in base if isinstance(b, dict) and b.get(key) not in seen]
    if top is None or top == [] or top == {}:
        return base if base is not None else top
    return top


def _merge_cases_into(a, b):
    """Two local documents for two CRM records of the same person: keep a, add b's cases and recent activity."""
    out = dict(a)
    ca, cb = dict(a.get("collections") or {}), b.get("collections") or {}
    ids = {c.get("case_id") for c in ca.get("cases") or []}
    ca["cases"] = (ca.get("cases") or []) + [c for c in cb.get("cases") or [] if c.get("case_id") not in ids]
    for k in ("recent_contacts", "recent_notes", "call_transcripts"):
        ca[k] = (ca.get(k) or []) + (cb.get(k) or [])
    out["collections"] = ca
    return out


@functools.lru_cache(maxsize=1)
def get_source():
    local = None
    try:
        local = LocalSource()
    except FileNotFoundError:
        if config.source() != "api":
            raise
    if config.source() == "mongo":
        return MongoSource(local=local)
    return ApiSource(local=local) if config.source() == "api" else local


def _exposure(cards, loans, deposits, cases):
    order = ["current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "180+"]
    accts = cards + loans
    buckets = [a.get("dpd_bucket") for a in accts if a.get("dpd_bucket") in order]
    open_cases = [c for c in cases if c.get("outcome") is None]
    return {
        "card_balance_total": round(sum(a.get("current_balance") or 0 for a in cards), 2),
        "loan_outstanding_total": round(sum(a.get("total_outstanding") or 0 for a in loans), 2),
        "deposit_balance_total": round(sum(a.get("current_balance") or 0 for a in deposits), 2),
        "past_due_total": round(sum(a.get("past_due_amount") or 0 for a in accts)
                                or sum(c.get("total_overdue") or 0 for c in open_cases), 2),
        "max_dpd": max([a.get("dpd") or 0 for a in accts] + [c.get("current_dpd") or 0 for c in open_cases] or [0]),
        "worst_bucket": max(buckets, key=order.index) if buckets else None,
        "delinquent_accounts": sum(1 for a in accts if (a.get("dpd") or 0) > 0),
        "product_count": len(accts) + len(deposits),
    }


def _hardship(cases, contacts, deposits):
    reasons = []
    for c in cases:
        if c.get("hardship_flag"):
            reasons.append(f"Case {c['case_id']} is coded as hardship ({c.get('hardship_reason') or 'reason not recorded'}).")
    if (contacts or {}).get("hardship_mentions"):
        reasons.append(f"Hardship mentioned on {contacts['hardship_mentions']} contact(s).")
    for d in deposits:
        if d.get("ei_deposit_flag"):
            reasons.append(f"Employment Insurance deposits on {d['account_id']}.")
        if (d.get("payroll_delay_days") or 0) > 7:
            reasons.append(f"Payroll {d['payroll_delay_days']} days late on {d['account_id']}.")
    return {"financial_hardship_flag": bool(reasons), "reasons": reasons,
            "hardship_mentions_total": (contacts or {}).get("hardship_mentions") or 0}
