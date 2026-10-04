"""Layer 2 HTTP API (SPEC_LAYER2_MULTIAGENT.md §9, single-agent mode).

    uvicorn c360.api.app:app --host 127.0.0.1 --port 8000          # base /api/v1

The Layer 3 server also mounts this app at /api/v1, so the agent terminal's ASK screen calls it from
the same address.

Routes: GET /health · POST /ask · POST /ask/stream (SSE: `started` first, `answer` or `error` last)
· GET /customers/{key} (trimmed, as the get_customer tool returns it) · GET /metrics · GET /examples
· GET /history?limit= · POST /feedback.

Operational rules: one answer at a time (the agent runs in a worker thread under a lock; the model
rotation's cool-downs are shared across requests); at most 5 questions waiting, then 429
`rate_limited` with Retry-After; 6 questions per minute per user; the model failing -> 502; the
serving database unavailable -> 503; validation -> 400. Error body: {error, code, request_id}.
Conversations are kept in memory for 2 hours (the last 5 turns, as summaries). Headers in:
X-C360-User, X-C360-Role; out: X-C360-Request-Id on every response.
"""
import collections
import datetime as dt
import json
import os
import threading
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from c360.layer2 import config

API_VERSION = "layer2-api-1.0"
MAX_WAITING = 5
PER_USER_PER_MIN = 6
CONV_TTL_S = 2 * 3600
CONV_TURNS = 5
EXAMPLES = Path(__file__).with_name("examples.yaml")

app = FastAPI(title="Collections 360 · Layer 2 API", version=API_VERSION,
              description="Ask the Collections 360 in plain English. Every answer shows its SQL or sources.")


class ApiError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    request.state.rid = rid
    response = await call_next(request)
    response.headers["X-C360-Request-Id"] = rid
    return response


def _err(request, status, code, message, retry_after=None):
    headers = {"Retry-After": str(int(retry_after))} if retry_after else None
    return JSONResponse({"error": message, "code": code, "request_id": getattr(request.state, "rid", None)},
                        status_code=status, headers=headers)


@app.exception_handler(ApiError)
async def _api_error(request: Request, e: ApiError):
    return _err(request, e.status, e.code, e.message, e.retry_after)


@app.exception_handler(Exception)
async def _unexpected(request: Request, e: Exception):
    return _err(request, 500, "internal", f"Unexpected error ({type(e).__name__}).")


from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def _invalid(request: Request, e: RequestValidationError):
    first = (e.errors() or [{}])[0]
    return _err(request, 400, "invalid_request", f"{'.'.join(str(x) for x in first.get('loc', []))}: {first.get('msg', 'invalid')}")


# ---- shared state: one tool context, one model (rotation state survives across requests) ----------
class _State:
    def __init__(self):
        self.lock = threading.Lock()            # one answer at a time
        self.ctx = None
        self.llm = None
        self.waiting = 0
        self.waiting_lock = threading.Lock()
        self.user_calls = collections.defaultdict(collections.deque)
        self.conversations = {}                 # id -> {"user", "history": [Message], "turns", "ts"}
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=200))

    def context(self):
        if self.ctx is None:
            from c360.layer2.tools.registry import ToolContext
            try:
                self.ctx = ToolContext.build()
            except Exception as e:      # noqa: BLE001
                raise ApiError(503, "serving_unavailable", f"The serving database is not available ({type(e).__name__}).")
        return self.ctx

    def model(self):
        if self.llm is None:
            from c360.layer2.llm import get_llm
            try:
                self.llm = get_llm()
            except Exception as e:      # noqa: BLE001 - e.g. no key / no model configured
                raise ApiError(503, "model_not_configured", str(e))
        return self.llm


STATE = _State()


def reset_state(ctx=None, llm=None):
    """For tests: inject a tool context and a model (e.g. the scripted fake)."""
    global STATE
    STATE = _State()
    STATE.ctx, STATE.llm = ctx, llm


def _model_info():
    pool = os.environ.get("C360_L2_MODELS", "").strip()
    llm = STATE.llm
    return {"single": config.settings().model, "rotation": [m.strip().split(":")[0] for m in pool.split(",") if m.strip()] or None,
            "last_used": getattr(llm, "name", None) if llm else None,
            "rotation_status": llm.status() if llm is not None and hasattr(llm, "status") else None}


# ---- routes ---------------------------------------------------------------------------------------
@app.get("/health")
def health():
    out = {"api": API_VERSION, "mode": "single", "capabilities": {"multi_agent": False, "stream": True},
           "models": _model_info(), "status": "ok"}
    try:
        ctx = STATE.context()
        out["serving"] = {"build_ts": ctx.serving.build_ts, "snapshot_date": str(ctx.serving.snapshot_date),
                          "tables": len(ctx.serving.tables)}
    except ApiError as e:
        out["status"], out["serving"] = "down", {"error": e.message}
        return out
    try:
        config.mongo_db().customers.find_one({}, {"_id": 1})
        out["mongo"] = "ok"
    except Exception as e:      # noqa: BLE001
        out["status"], out["mongo"] = "degraded", f"unreachable ({type(e).__name__})"
    if not (config.api_key() and (config.settings().model or os.environ.get("C360_L2_MODELS"))):
        out["status"] = "degraded"
        out["models"]["note"] = "no model key or model configured: /ask is unavailable"
    return out


class AskOptions(BaseModel):
    mode: str | None = None
    roles_off: list[str] | None = None
    allow_clarification: bool | None = None
    events: str | None = None


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    include_trace: bool = False
    options: AskOptions | None = None


def _admit(user):
    now = time.time()
    calls = STATE.user_calls[user]
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= PER_USER_PER_MIN:
        raise ApiError(429, "rate_limited", f"At most {PER_USER_PER_MIN} questions a minute per person.", retry_after=60 - (now - calls[0]) + 1)
    with STATE.waiting_lock:
        if STATE.waiting >= MAX_WAITING:
            raise ApiError(429, "rate_limited", "Too many questions are waiting; try again shortly.", retry_after=30)
        STATE.waiting += 1
    calls.append(now)


def _run(body: AskBody, user: str) -> dict:
    """Admit, then answer under the lock. Returns the response body."""
    from c360.layer2.agent import Agent, history_messages
    if body.options and body.options.mode not in (None, "single"):
        raise ApiError(400, "invalid_request", "Only mode 'single' is available (multi-agent mode is not built).")
    _admit(user)
    try:
        with STATE.lock:
            ctx, llm = STATE.context(), STATE.model()
            now = time.time()
            for cid in [c for c, v in STATE.conversations.items() if now - v["ts"] > CONV_TTL_S]:
                STATE.conversations.pop(cid, None)
            conv = STATE.conversations.get(body.conversation_id or "")
            if conv is None or conv["user"] != user:
                conv = {"user": user, "history": [], "turns": 0, "ts": now}
                cid = uuid.uuid4().hex[:16]
                STATE.conversations[cid] = conv
            else:
                cid = body.conversation_id
            result = Agent(llm, ctx).run(body.question, conv["history"])
            answer = result.answer
            if any(str(w).startswith("model error") for w in answer.warnings) and answer.status == "partial":
                raise ApiError(502, "model_unavailable", "The language model could not answer right now: "
                               + next(str(w) for w in answer.warnings if str(w).startswith("model error"))[:300])
            conv["history"] = (conv["history"] + history_messages(body.question, answer))[-2 * CONV_TURNS:]
            conv["turns"] += 1
            conv["ts"] = time.time()
            turn_id = f"{cid}-{conv['turns']}"
            STATE.history[user].append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "turn_id": turn_id,
                                        "question": body.question, "status": answer.status, "model": answer.meta.model,
                                        "latency_ms": answer.meta.latency_ms})
            out = {"conversation_id": cid, "turn_id": turn_id, "answer": answer.model_dump(mode="json")}
            if body.include_trace:
                out["trace"] = result.trace.model_dump(mode="json")
            return out
    finally:
        with STATE.waiting_lock:
            STATE.waiting -= 1


@app.post("/ask")
def ask(body: AskBody, x_c360_user: str = Header("anonymous")):
    return _run(body, x_c360_user)


@app.post("/ask/stream")
def ask_stream(body: AskBody, request: Request, x_c360_user: str = Header("anonymous")):
    rid = getattr(request.state, "rid", None)

    def events():
        yield _sse("started", {"question": body.question, "request_id": rid})
        try:
            yield _sse("answer", _run(body, x_c360_user))
        except ApiError as e:
            yield _sse("error", {"error": e.message, "code": e.code, "request_id": rid})
        except Exception as e:      # noqa: BLE001
            yield _sse("error", {"error": f"Unexpected error ({type(e).__name__}).", "code": "internal", "request_id": rid})
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/customers/{key}")
def customer(key: str):
    from c360.layer2.tools.get_customer import get_customer
    from c360.store import C360Store
    res = get_customer(STATE.context(), key)
    if not res.ok:
        msg = res.error or "not found"
        try:
            known = C360Store(database=STATE.context().mongo_db()).query_for(key) is not None
        except Exception:       # noqa: BLE001
            raise ApiError(503, "store_unavailable", msg)
        raise ApiError(404 if known else 400, "not_found" if known else "invalid_key", msg)
    return res.data


@app.get("/metrics")
def metrics():
    m = STATE.context().metrics
    return [{"key": k, "label": v.get("label"), "description": v.get("description"), "status": v.get("status"),
             "unit": v.get("unit"), "dimensions": sorted((v.get("dimensions") or {}).keys())} for k, v in sorted(m.items())]


@app.get("/examples")
def examples():
    return yaml.safe_load(EXAMPLES.read_text())


@app.get("/history")
def history(limit: int = 20, x_c360_user: str = Header("anonymous")):
    return list(STATE.history[x_c360_user])[-max(1, min(limit, 200)):][::-1]


class Feedback(BaseModel):
    turn_id: str | None = None
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=2000)


@app.post("/feedback")
def feedback(body: Feedback, x_c360_user: str = Header("anonymous")):
    path = config.settings().out_dir / "feedback.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "user": x_c360_user,
                            **body.model_dump()}) + "\n")
    return {"status": "recorded"}
