"""Layer 3 HTTP API and the two web apps.

    uvicorn c360.layer3.api:app --host 127.0.0.1 --port 8300
    open http://127.0.0.1:8300/terminal      # employee terminal
    open http://127.0.0.1:8300/app           # customer app

Employee endpoints live under /api/l3 and take the hackathon auth headers X-C360-User / X-C360-Role
(the same scheme as SPEC_FRONTEND.md). Customer endpoints live under /api/me and need a bearer token
from POST /api/me/login (case id + last name; demo sign-in, no passwords).

Every decision that affects a customer is recorded with who, when and why, and nothing is approved
automatically: Approve is refused server-side while the recommendation has policy conflicts.
"""
import datetime as dt
import secrets
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from c360.layer3 import actions, assistant, config, conversation, hardship, nba, postcall
from c360.layer3.data.source import BadKey, NotFound, get_source
from c360.layer3.policy import library
from c360.layer3.util import as_date

WEB = Path(__file__).parent / "web"
app = FastAPI(title="Collections 360 - Layer 3", version="l3-1.0.0")


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = f"req_{uuid.uuid4().hex[:10]}"
    request.state.rid = rid
    try:
        resp = await call_next(request)
    except Exception as e:  # noqa: BLE001
        resp = JSONResponse({"error": "Something went wrong on the server.", "code": type(e).__name__, "request_id": rid},
                            status_code=500)
    resp.headers["X-C360-Request-Id"] = rid
    return resp


@app.exception_handler(NotFound)
async def _nf(request, e):
    return JSONResponse({"error": str(e), "code": "not_found", "request_id": request.state.rid}, status_code=404)


@app.exception_handler(BadKey)
async def _bk(request, e):
    return JSONResponse({"error": str(e), "code": "bad_key", "request_id": request.state.rid}, status_code=400)


def employee(x_c360_user: str = Header("demo.agent"), x_c360_role: str = Header("agent")):
    return {"id": x_c360_user, "role": x_c360_role}


def src():
    return get_source()


# ---- employee: health, portfolio, worklist --------------------------------------------------------
@app.get("/api/l3/health")
def health():
    s = src()
    ev = config.out_dir() / "postcall_eval.json"
    return {"status": "ok", "source": s.name, "snapshot_date": config.SNAPSHOT_DATE.isoformat(),
            "store_built": str((getattr(s, "meta", None) or {}).get("built_ts")),
            "actions": actions.store().counts(), "policy_sections": len(library().sections),
            "postcall_eval": ev.exists(), "engines": [nba.ENGINE_VERSION, "postcall-rules-1.1"]}


@app.get("/api/l3/portfolio")
def portfolio():
    p = src().portfolio()
    return p | {"pending_approvals": actions.store().counts().get("pending", 0)}


@app.get("/api/l3/worklist")
def worklist(queue: str | None = None, bucket: str | None = None, flag: str | None = None, q: str | None = None,
             limit: int = 60, group_by: str | None = None, group: str | None = None, area: str | None = None):
    return {"rows": src().worklist(queue=queue, bucket=bucket, flag=flag, q=q, limit=min(limit, 200),
                                   group_by=group_by, group=group, area=area),
            "queues": src().queues()}


@app.get("/api/l3/groups")
def groups(by: str = "sector", queue: str | None = None, flag: str | None = None, area: str | None = None, limit: int = 25):
    """Open cases grouped by job sector, industry, employer, postal area, city or province (Groups view)."""
    out = src().segments(by=by, queue=queue, flag=flag, area=area, limit=min(limit, 100))
    out["layoff_alerts"] = src().layoff_alerts()
    return out


@app.get("/api/l3/cases/{case_id}/peers")
def case_peers(case_id: str):
    """Similar customers: same employer, same job sector in the same postal area, same postal area (figures only)."""
    return src().peers(case_id)


# ---- employee: customer 360 + next best action ------------------------------------------------------
@app.get("/api/l3/customers/{key}")
def customer(key: str):
    doc = conversation.overlay(src().customer_doc(key), actions.store())
    doc["interactions"] = actions.store().list(c360_id=doc["_id"], limit=30)
    return doc


@app.get("/api/l3/cases/{case_id}/nba")
def case_nba(case_id: str):
    doc = conversation.overlay(src().customer_doc(case_id), actions.store())
    rec = nba.recommend(doc, case_id)
    rec["history"] = actions.store().list(case_id=case_id, types=["nba_decision"], limit=10)
    rec["program_name"] = src().program_names().get(rec.get("program")) if rec.get("program") else None
    return rec


class Edits(BaseModel):
    action_code: str | None = None
    channel: str | None = None
    date: str | None = None
    window_local: str | None = None
    amount: float | None = None
    program: str | None = None
    promise_date: str | None = None


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|edit|reject)$")
    reason: str | None = None
    reason_code: str | None = None
    edits: Edits | None = None


def _apply_edits(rec, e: Edits | None):
    if not e:
        return rec
    rec = dict(rec)
    if e.action_code:
        rec["action"] = {"code": e.action_code, "label": nba.ACTIONS.get(e.action_code, e.action_code)}
    if e.channel:
        rec["channel"] = e.channel
    if e.date or e.window_local:
        rec["timing"] = dict(rec.get("timing") or {}) | {k: v for k, v in (("date", e.date), ("window_local", e.window_local)) if v}
    if e.amount is not None:
        rec["amount"] = e.amount
    if e.program:
        rec["program"] = e.program
    if e.promise_date:
        rec["promise_date"] = e.promise_date
    return rec


@app.post("/api/l3/cases/{case_id}/check")
def check(case_id: str, body: Decision):
    doc = conversation.overlay(src().customer_doc(case_id), actions.store())
    rec = _apply_edits(nba.recommend(doc, case_id), body.edits)
    return {"conflicts": nba.check(rec, doc), "recommendation": rec}


@app.post("/api/l3/cases/{case_id}/decision")
def decide(case_id: str, body: Decision, user=Depends(employee)):
    doc = conversation.overlay(src().customer_doc(case_id), actions.store())
    original = nba.recommend(doc, case_id)
    final = _apply_edits(original, body.edits) if body.decision == "edit" else original
    conflicts = nba.check(final, doc)
    if body.decision in ("edit", "reject") and not (body.reason or "").strip():
        raise HTTPException(400, {"error": "A reason is required to edit or reject.", "field": "reason"})
    if body.decision in ("approve", "edit") and conflicts:
        raise HTTPException(409, {"error": "Resolve the conflicts before approving.", "conflicts": conflicts})
    level = {"agent": 0, "hardship_specialist": 1, "team_lead": 2, "admin": 3}
    needed = final.get("approver_level") or "agent"
    if body.decision != "reject" and level.get(user["role"], 0) < level.get(needed, 0):
        raise HTTPException(403, {"error": f"This action needs a {needed.replace('_', ' ')} to approve.", "needed": needed})
    status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[body.decision]
    item = actions.store().add("nba_decision", doc["_id"], case_id,
                               {"recommendation": original, "final": final if status != "rejected" else None,
                                "edits": body.edits.model_dump(exclude_none=True) if body.edits else None},
                               source="layer3.terminal", actor=user["id"], status="pending")
    return actions.store().decide(item["id"], status, user["id"], body.reason or body.reason_code,
                                  decision={"decision": body.decision, "reason_code": body.reason_code, "role": user["role"]})


# ---- employee: approvals queue (customer requests) ---------------------------------------------------
@app.get("/api/l3/approvals")
def approvals(status: str = "pending", limit: int = 50):
    return {"items": actions.store().list(status=status, limit=limit,
                                          types=["hardship_request", "cease_request", "dispute_request", "callback_request",
                                                 "contact_preference", "vulnerability_flag", "insolvency_check", "conduct_alert"])}


class Approval(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    reason: str | None = None


@app.post("/api/l3/approvals/{item_id}")
def approve_request(item_id: str, body: Approval, user=Depends(employee)):
    if body.status == "rejected" and not (body.reason or "").strip():
        raise HTTPException(400, {"error": "A reason is required to reject.", "field": "reason"})
    item = actions.store().get(item_id)
    if not item:
        raise HTTPException(404, {"error": "No such request."})
    if (item["payload"] or {}).get("approver_level") and body.status == "approved":
        needed = (item["payload"] or {}).get("approver_level") or "agent"
        level = {"agent": 0, "hardship_specialist": 1, "team_lead": 2, "admin": 3}
        if level.get(user["role"], 0) < level.get(needed, 0):
            raise HTTPException(403, {"error": f"This needs a {needed.replace('_', ' ')} to approve.", "needed": needed})
    out = actions.store().decide(item_id, body.status, user["id"], body.reason)
    if out is None:
        raise HTTPException(409, {"error": "This request was already decided."})
    return out


# ---- employee: log a conversation (paste a transcript or notes -> facts, follow-ups, checks) ----------
def _conv_case(case_id):
    doc = conversation.overlay(src().customer_doc(case_id), actions.store())
    case = nba.pick_case(doc, case_id)
    if case is None:
        raise NotFound(f"No case '{case_id}'")
    return doc, case


@app.post("/api/l3/cases/{case_id}/conversations/extract")
def conversation_extract(case_id: str, body: dict = Body(...), user=Depends(employee)):
    """Read the pasted text and return a draft for the agent to review. Writes nothing."""
    doc, case = _conv_case(case_id)
    try:
        return conversation.preview(body, doc, case)
    except conversation.ConvError as e:
        raise HTTPException(e.status, e.detail)


@app.post("/api/l3/cases/{case_id}/conversations")
def conversation_save(case_id: str, body: dict = Body(...), user=Depends(employee)):
    """Save a reviewed conversation: the server re-runs the extraction, applies the agent's corrections and
    writes the log plus the chosen follow-ups. Needs confirm: true."""
    doc, case = _conv_case(case_id)
    try:
        out = conversation.save(body, doc, case, user, actions.store())
    except conversation.ConvError as e:
        raise HTTPException(e.status, e.detail)
    after = conversation.overlay(src().customer_doc(case_id), actions.store())
    out["effects"] = [e for e in after.get("_overlay", []) if e["case_id"] == case_id]
    out["nba"] = nba.recommend(after, case_id)
    return out


def _conv_public(item, full=False):
    p = dict(item.get("payload") or {})
    if not full:
        p.pop("turns_masked", None)
        p.pop("text_masked", None)
    return dict(item, payload=p)


def _followups(case_id):
    out = {}
    for it in actions.store().list(case_id=case_id, types=list(conversation.FOLLOW_UP_TYPES), limit=200):
        cid = (it.get("payload") or {}).get("conversation_id")
        if cid and it.get("source") == conversation.SOURCE:
            out.setdefault(cid, []).append({"id": it["id"], "type": it["type"], "status": it["status"], "decided_by": it.get("decided_by"),
                                            "payload": {k: (it["payload"] or {}).get(k) for k in ("amount", "date", "reason", "note", "dispute_type", "approver_level")}})
    return out


@app.get("/api/l3/cases/{case_id}/conversations")
def conversation_list(case_id: str, limit: int = 10):
    items = actions.store().list(case_id=case_id, types=["conversation_log"], limit=min(limit, 50))
    return {"items": [_conv_public(i) for i in items], "followups": _followups(case_id)}


@app.get("/api/l3/conversations/{conversation_id}")
def conversation_get(conversation_id: str):
    for it in actions.store().list(types=["conversation_log"], limit=500):
        if (it.get("payload") or {}).get("conversation_id") == conversation_id:
            return {"conversation": _conv_public(it, full=True), "followups": _followups(it["case_id"]).get(conversation_id, [])}
    raise HTTPException(404, {"error": "No such conversation."})


# ---- employee: calls (post-call summary and QA) ------------------------------------------------------
@app.get("/api/l3/calls/{transcript_id}")
def call(transcript_id: str):
    s = src()
    t = s.transcript(transcript_id)
    analysis = postcall.analyse_by_id(s, transcript_id)
    turns = [{k: x.get(k) for k in ("turn_index", "speaker", "start_sec", "end_sec", "text", "sentiment_score")}
             for x in t.get("turns") or []]
    reviews = actions.store().list(case_id=t.get("case_id"), types=["postcall_review"], limit=10)
    return {"analysis": analysis, "turns": turns,
            "reviews": [r for r in reviews if (r["payload"] or {}).get("transcript_id") == str(transcript_id)]}


class Review(BaseModel):
    verdict: str = Field(pattern="^(accept|correct)$")
    corrections: dict | None = None
    note: str | None = None


@app.post("/api/l3/calls/{transcript_id}/review")
def review_call(transcript_id: str, body: Review, user=Depends(employee)):
    s = src()
    analysis = postcall.analyse_by_id(s, transcript_id)
    doc_id = s.customer_doc(analysis["case_id"])["_id"] if analysis.get("case_id") else None
    return actions.store().add("postcall_review", doc_id, analysis.get("case_id"),
                               {"transcript_id": str(transcript_id), "verdict": body.verdict, "corrections": body.corrections,
                                "note": body.note, "summary": analysis["summary"], "qa_score": analysis["qa"]["score"]},
                               source="layer3.postcall", actor=user["id"], status="recorded")


@app.get("/api/l3/postcall/eval")
def postcall_eval():
    import json
    p = config.out_dir() / "postcall_eval.json"
    if not p.exists():
        raise HTTPException(404, {"error": "Run: python -m c360.layer3.postcall --eval"})
    return json.loads(p.read_text())


@app.get("/api/l3/policy")
def policy_search(q: str, k: int = 3):
    return {"hits": [s.public() for s in library().search(q, k=min(k, 8))]}


# ---- customer app ---------------------------------------------------------------------------------
SESSIONS: dict[str, dict] = {}
CUSTOMER_TYPES = ["hardship_request", "promise", "payment", "cease_request", "dispute_request", "callback_request",
                  "contact_preference"]


class Login(BaseModel):
    case_id: str
    last_name: str


@app.post("/api/me/login")
def login(body: Login):
    try:
        doc = src().customer_doc(body.case_id.strip().upper())
    except (NotFound, BadKey):
        raise HTTPException(401, {"error": "We couldn't match those details. Check the reference number on your letter."})
    if (doc.get("profile") or {}).get("last_name", "").strip().lower() != body.last_name.strip().lower():
        raise HTTPException(401, {"error": "We couldn't match those details. Check the reference number on your letter."})
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = {"case_id": body.case_id.strip().upper(), "c360_id": doc["_id"], "ts": dt.datetime.now()}
    return {"token": token, "first_name": (doc.get("profile") or {}).get("first_name")}


def me(authorization: str = Header("")):
    sess = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not sess:
        raise HTTPException(401, {"error": "Please sign in again."})
    return sess


def _my(sess):
    doc = src().customer_doc(sess["case_id"])
    case = nba.pick_case(doc, sess["case_id"])
    return doc, case


@app.get("/api/me")
def my_account(sess=Depends(me)):
    doc, case = _my(sess)
    s = assistant.summary_for_customer(doc, case)
    p = case.get("promises") or {}
    sc = doc.get("servicing_constraints") or {}
    reqs = actions.store().list(c360_id=doc["_id"], types=CUSTOMER_TYPES, limit=20)
    return {
        "first_name": (doc.get("profile") or {}).get("preferred_name") or (doc.get("profile") or {}).get("first_name"),
        "case_id": case.get("case_id"), "product": case.get("primary_product"), "overdue": s["overdue"],
        "balance": s["balance"], "days_past_due": s["days_past_due"], "bucket": case.get("current_bucket"),
        "next_promise": s["next_promise"], "active_plan": s["active_plan"],
        "promises": [{k: x.get(k) for k in ("ptp_amount", "ptp_due_date", "ptp_status", "amount_paid_against")}
                     for x in (p.get("items") or [])[:6]],
        "accounts": _my_accounts(doc),
        "contact_preferences": {"call": sc.get("consent_call"), "sms": sc.get("consent_sms"), "email": sc.get("consent_email")},
        "requests": [{k: r[k] for k in ("id", "ts", "type", "status", "decision_reason")} | {"payload": _public_payload(r)}
                     for r in reqs],
        "snapshot_date": config.SNAPSHOT_DATE.isoformat(),
    }


def _my_accounts(doc):
    out = []
    for g, label in (("cards", "Credit card"), ("loans", "Loan"), ("deposits", "Account")):
        for a in (doc.get("accounts") or {}).get(g) or []:
            out.append({"type": label, "account": "•••• " + str(a.get("account_id", ""))[-4:],
                        "product": a.get("product_code") or a.get("product_type"),
                        "balance": a.get("current_balance") if g != "loans" else a.get("total_outstanding"),
                        "past_due": a.get("past_due_amount"), "minimum_due": a.get("minimum_payment_due") or a.get("next_due_amount"),
                        "due_date": a.get("payment_due_date") or a.get("next_due_date")})
    return out


def _public_payload(r):
    p = r.get("payload") or {}
    keep = ("program_id", "program_name", "amount", "date", "reason", "channels", "topic", "time", "dispute_type", "estimate")
    return {k: p[k] for k in keep if k in p}


@app.get("/api/me/options")
def my_options(reason: str | None = None, sess=Depends(me)):
    doc, case = _my(sess)
    ev = hardship.evaluate(doc, case, stated_reason=reason or "other")
    return {"regular_payment": ev["regular_payment"],
            "options": [o for o in ev["options"] if o["program_id"] != "HP-SETTLE"],
            "policy": "POL-COLL-004 §4.2"}


class HardshipReq(BaseModel):
    program_id: str
    reason: str = "other"
    note: str | None = Field(None, max_length=1000)
    monthly_income: float | None = None
    monthly_expenses: float | None = None


@app.post("/api/me/hardship")
def my_hardship(body: HardshipReq, sess=Depends(me)):
    doc, case = _my(sess)
    ev = hardship.evaluate(doc, case, stated_reason=body.reason)
    opt = next((o for o in ev["options"] if o["program_id"] == body.program_id), None)
    if not opt or not opt["eligible"]:
        raise HTTPException(400, {"error": "That option isn't available for this account.", "why_not": (opt or {}).get("why_not")})
    return actions.store().add("hardship_request", doc["_id"], case["case_id"],
                               {"program_id": opt["program_id"], "program_name": opt["name"], "reason": body.reason,
                                "note": body.note, "monthly_income": body.monthly_income,
                                "monthly_expenses": body.monthly_expenses, "estimate": opt["estimate"],
                                "approver_level": opt["approver_level"], "requires_income_evidence": opt["requires_income_evidence"],
                                "cite": opt["cite"]},
                               source="layer3.customer_app", actor="customer", status="pending")


class PromiseReq(BaseModel):
    amount: float = Field(gt=0)
    date: str


@app.post("/api/me/promise")
def my_promise(body: PromiseReq, sess=Depends(me)):
    doc, case = _my(sess)
    d = as_date(body.date)
    snap = config.SNAPSHOT_DATE
    if not d or d <= snap:
        raise HTTPException(400, {"error": "Choose a date after today.", "field": "date"})
    if (d - snap).days > 21:
        raise HTTPException(400, {"error": "Choose a date within the next 21 days.", "field": "date", "cite": "PRC-COLL-010 §3.1"})
    if d in nba.STAT_HOLIDAYS:
        raise HTTPException(400, {"error": "That's a statutory holiday; choose another date.", "field": "date"})
    bal = case.get("total_exposure") or case.get("total_overdue") or 0
    if bal and body.amount > bal * 1.01:
        raise HTTPException(400, {"error": "That's more than the balance.", "field": "amount"})
    return actions.store().add("promise", doc["_id"], case["case_id"],
                               {"amount": round(body.amount, 2), "date": d.isoformat(), "channel": "self_service",
                                "covers_overdue": body.amount >= (case.get("total_overdue") or 0)},
                               source="layer3.customer_app", actor="customer", status="recorded")


class PaymentReq(BaseModel):
    amount: float = Field(gt=0)


@app.post("/api/me/payment")
def my_payment(body: PaymentReq, sess=Depends(me)):
    doc, case = _my(sess)
    return actions.store().add("payment", doc["_id"], case["case_id"],
                               {"amount": round(body.amount, 2), "method": "demo", "note": "Demo only: no money moves."},
                               source="layer3.customer_app", actor="customer", status="recorded")


class ContactReq(BaseModel):
    kind: str = Field(pattern="^(preferences|cease)$")
    channels: dict | None = None
    note: str | None = Field(None, max_length=500)


@app.post("/api/me/contact")
def my_contact(body: ContactReq, sess=Depends(me)):
    doc, case = _my(sess)
    t = "cease_request" if body.kind == "cease" else "contact_preference"
    return actions.store().add(t, doc["_id"], case["case_id"], {"channels": body.channels, "note": body.note,
                                                                 "cite": "POL-COLL-001 §5.4" if t == "cease_request" else None},
                               source="layer3.customer_app", actor="customer", status="pending")


class DisputeReq(BaseModel):
    dispute_type: str
    details: str = Field(max_length=1000)


@app.post("/api/me/dispute")
def my_dispute(body: DisputeReq, sess=Depends(me)):
    doc, case = _my(sess)
    return actions.store().add("dispute_request", doc["_id"], case["case_id"], body.model_dump(),
                               source="layer3.customer_app", actor="customer", status="pending")


class CallbackReq(BaseModel):
    time: str
    topic: str = "general"


@app.post("/api/me/callback")
def my_callback(body: CallbackReq, sess=Depends(me)):
    doc, case = _my(sess)
    return actions.store().add("callback_request", doc["_id"], case["case_id"], body.model_dump(),
                               source="layer3.customer_app", actor="customer", status="pending")


class Chat(BaseModel):
    message: str = Field(max_length=1000)


@app.post("/api/me/chat")
def my_chat(body: Chat, sess=Depends(me)):
    doc, case = _my(sess)
    out = assistant.reply(doc, body.message, case["case_id"])
    if out["signals"]:
        actions.store().add("assistant_message", doc["_id"], case["case_id"],
                            {"intent": out["intent"], "signals": out["signals"]},
                            source="layer3.assistant", actor="customer", status="recorded")
    return out


# ---- Layer 2 (plain-English questions) under /api/v1, for the terminal's ASK screen ---------------
try:
    from c360.api.app import app as layer2_api
    app.mount("/api/v1", layer2_api)
except Exception as _e:     # noqa: BLE001 - Layer 3 works without Layer 2 (the ASK screen then says so)
    layer2_api = None

# ---- web apps -----------------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(WEB / "index.html")


@app.get("/terminal", include_in_schema=False)
def terminal():
    return FileResponse(WEB / "terminal" / "index.html")


@app.get("/app", include_in_schema=False)
def customer_app():
    return FileResponse(WEB / "customer" / "index.html")


@app.get("/docs/layer3", include_in_schema=False)
def docs_redirect():
    return RedirectResponse("/docs")
