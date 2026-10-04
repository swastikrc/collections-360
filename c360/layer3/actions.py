"""Layer 3 write-back: every decision and customer request, append-only, with who and why.

Kept in `outputs/layer3/actions.sqlite` so the demo runs anywhere. When the Collections 360
MongoDB is reachable (C360_L3_WRITEBACK=mongo), each record is also appended to the customer's
document through `c360.store.C360Store.append_interaction`, the write-back path Layer 1 provides.

Statuses: recorded (nothing to approve), pending (waits for a person), approved, edited, rejected.
"""
import datetime as dt
import json
import os
import sqlite3
import threading
import uuid

from c360.layer3 import config

TYPES = {"nba_decision", "hardship_request", "promise", "payment", "cease_request", "callback_request",
         "dispute_request", "contact_preference", "assistant_message", "postcall_review",
         # logged from a conversation in the terminal (c360/layer3/conversation.py)
         "conversation_log", "vulnerability_flag", "insolvency_check", "conduct_alert",
         # AI next best step (c360/layer3/ai_nba.py): one record per click, and when an agent uses it as an edit
         "ai_nba_suggestion", "ai_nba_used"}


class ActionStore:
    def __init__(self, path=None):
        self.path = str(path or config.actions_db())
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY, ts TEXT, c360_id TEXT, case_id TEXT, type TEXT, source TEXT, status TEXT,
                actor TEXT, payload TEXT, decided_by TEXT, decided_ts TEXT, decision_reason TEXT, decision TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_c360 ON interactions(c360_id)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_status ON interactions(status)")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def add(self, type_, c360_id, case_id, payload, source, actor, status="recorded", mirror_exclude=()):
        """`mirror_exclude`: payload keys kept in the local store only, never sent to MongoDB (data minimisation)."""
        if type_ not in TYPES:
            raise ValueError(f"unknown interaction type {type_}")
        item = {"id": f"INT-{uuid.uuid4().hex[:12]}", "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "c360_id": c360_id, "case_id": case_id, "type": type_, "source": source, "status": status,
                "actor": actor, "payload": json.dumps(payload, default=str)}
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO interactions (id, ts, c360_id, case_id, type, source, status, actor, payload) "
                      "VALUES (:id, :ts, :c360_id, :case_id, :type, :source, :status, :actor, :payload)", item)
        _mirror(item, mirror_exclude)
        return self.get(item["id"])

    def decide(self, id_, status, by, reason=None, decision=None):
        if status not in ("approved", "edited", "rejected"):
            raise ValueError("status must be approved, edited or rejected")
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE interactions SET status = ?, decided_by = ?, decided_ts = ?, decision_reason = ?, "
                            "decision = ? WHERE id = ? AND status = 'pending'",
                            (status, by, dt.datetime.now().isoformat(timespec="seconds"), reason,
                             json.dumps(decision, default=str) if decision is not None else None, id_))
            if cur.rowcount == 0:
                return None
        return self.get(id_)

    def get(self, id_):
        with self._conn() as c:
            r = c.execute("SELECT * FROM interactions WHERE id = ?", (id_,)).fetchone()
        return _row(r) if r else None

    def list(self, c360_id=None, case_id=None, status=None, types=None, limit=50):
        where, params = [], []
        for col, val in (("c360_id", c360_id), ("case_id", case_id), ("status", status)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            params += list(types)
        sql = "SELECT * FROM interactions" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY ts DESC LIMIT ?"
        with self._conn() as c:
            return [_row(r) for r in c.execute(sql, params + [int(limit)]).fetchall()]

    def counts(self):
        with self._conn() as c:
            return {r["status"]: r["n"] for r in c.execute("SELECT status, count(*) AS n FROM interactions GROUP BY 1")}


def _row(r):
    d = dict(r)
    for k in ("payload", "decision"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def _mirror(item, exclude=()):
    if os.environ.get("C360_L3_WRITEBACK", "").lower() != "mongo":
        return
    try:
        from c360.store import C360Store
        payload = {k: v for k, v in json.loads(item["payload"]).items() if k not in set(exclude)}
        C360Store().append_interaction(item["c360_id"], item["type"], payload,
                                       source=item["source"], case_id=item["case_id"], written_by=item["actor"])
    except Exception:  # noqa: BLE001 - the local store is the record; Mongo is a mirror
        pass


_store = None


def store():
    global _store
    if _store is None:
        _store = ActionStore()
    return _store
