"""Rotate between several Gemini models so a long run keeps going under per-minute request limits.

    export C360_L2_MODELS="gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash"      # priority order
    export C360_L2_MODELS="gemini-3.8-flash:10,gemini-3.7-flash:10"                 # optional requests/minute per model

Each model has its own quota. The rotation keeps using the current model; when it answers HTTP 429
(or is overloaded), that model is put on a cool-down taken from the provider's retry delay and the
next available model in the list takes over:

  - per-minute limit   -> cool-down = the retry delay the API gives (default 60 s)
  - per-day limit      -> the model is parked for the rest of the run (or the retry delay, if longer)
  - overloaded / 5xx   -> short cool-down (20 s)

With a requests-per-minute figure (`name:rpm`) the rotation also paces itself and switches *before*
hitting the limit. If every model is cooling down, it waits when the soonest one is free within
C360_L2_MAX_WAIT seconds (default 75), otherwise it returns an error (the evaluation records it as a
model error and `--resume-from` asks the question again later).

Switching mid-question: Gemini 3 attaches thought signatures to its function calls, and a model only
accepts its own. When a conversation moves to another model, earlier model turns are replayed
without their thoughts and with the documented placeholder signature for history that comes from a
different model. Every answer records the model that produced it (Answer.meta.model).
"""
import collections
import re
import sys
import time

from c360.layer2.llm.base import LLMTurn, Message, ToolSpec

SKIP_SIGNATURE = b"skip_thought_signature_validator"   # Gemini docs: history transferred from another model
DEFAULT_COOLDOWN = 60.0
OVERLOAD_COOLDOWN = 20.0
DAY_COOLDOWN = 24 * 3600.0
TRANSIENT_NAMES = ("ServerError", "TimeoutException", "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError")


def parse_pool(text: str) -> list[tuple[str, int | None]]:
    """'a, b:10 ,c' -> [('a', None), ('b', 10), ('c', None)]; duplicates dropped, order kept."""
    out, seen = [], set()
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, rpm = item.partition(":")
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            out.append((name, int(rpm) if rpm.strip().isdigit() and int(rpm) > 0 else None))
    return out


def classify(e) -> tuple[str, float | None]:
    """('minute' | 'day' | 'overload' | 'fatal', retry_delay_seconds_or_None) for a provider exception."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = str(getattr(e, "details", "") or "") + " " + str(getattr(e, "message", "") or e)
    delay = None
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text) or re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
    if m:
        delay = float(m.group(1))
    if code == 429:
        if re.search(r"PerDay|per day|daily", text, re.I):
            return "day", delay
        return "minute", delay
    if code in (500, 502, 503, 504, 408) or type(e).__name__ in TRANSIENT_NAMES:
        return "overload", delay
    return "fatal", delay


class _Slot:
    def __init__(self, llm, rpm):
        self.llm, self.rpm = llm, rpm
        self.cool_until = 0.0
        self.reason = ""
        self.calls = collections.deque()          # call times in the last minute (for pacing)
        self.used = 0

    def free_at(self, now) -> float:
        t = self.cool_until
        if self.rpm:
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
            if len(self.calls) >= self.rpm:
                t = max(t, self.calls[0] + 60)
        return t


class RotatingGeminiLLM:
    def __init__(self, pool, client=None, api_key=None, temperature=None, max_wait=75.0,
                 clock=time.monotonic, sleep=time.sleep, log=None):
        from c360.layer2.llm.gemini import GeminiLLM, LLMConfigError
        pool = parse_pool(pool) if isinstance(pool, str) else list(pool)
        if not pool:
            raise LLMConfigError("C360_L2_MODELS is empty.")
        if client is None:
            first = GeminiLLM(model=pool[0][0], api_key=api_key, temperature=temperature)
            client = first._client
        self.slots = [_Slot(GeminiLLM(model=n, client=client, temperature=temperature, max_retries=0), r) for n, r in pool]
        self.max_wait, self.clock, self.sleep = max_wait, clock, sleep
        self.log = log or (lambda msg: print(f"[gemini-rotation] {msg}", file=sys.stderr, flush=True))
        self.current = self.slots[0]
        self._made_by = {}                          # id(content) -> (content, model): who wrote each replayed turn

    @property
    def name(self) -> str:
        """The model used for the latest call (what Answer.meta.model records)."""
        return self.current.llm.name

    @property
    def label(self) -> str:
        return "rotation: " + ", ".join(s.llm.name for s in self.slots)

    def status(self) -> list[dict]:
        now = self.clock()
        return [{"model": s.llm.name, "calls": s.used, "free_in_s": max(0.0, round(s.free_at(now) - now, 1)), "reason": s.reason}
                for s in self.slots]

    # ---- choosing a model ----------------------------------------------------------------------------
    def _pick(self):
        now = self.clock()
        if self.current.free_at(now) <= now:
            return self.current, 0.0
        for s in self.slots:                        # priority order
            if s.free_at(now) <= now:
                return s, 0.0
        s = min(self.slots, key=lambda x: x.free_at(now))
        return s, s.free_at(now) - now

    # ---- history from another model ------------------------------------------------------------------
    def _portable(self, messages: list[Message], target: str) -> list[Message]:
        from google.genai import types
        out = []
        for m in messages:
            made = self._made_by.get(id(m.raw)) if m.raw is not None else None
            if m.role != "assistant" or made is None or made[0] is not m.raw or made[1] == target:
                out.append(m)
                continue
            parts = []
            for p in getattr(m.raw, "parts", None) or []:
                if getattr(p, "thought", False):
                    continue
                if getattr(p, "function_call", None) is not None:
                    parts.append(types.Part(function_call=p.function_call, thought_signature=SKIP_SIGNATURE))
                elif getattr(p, "text", None):
                    parts.append(types.Part(text=p.text))
            out.append(Message(role="assistant", text=m.text, tool_calls=m.tool_calls,
                               raw=types.Content(role="model", parts=parts or [types.Part(text=m.text or "")])))
        return out

    # ---- the call ------------------------------------------------------------------------------------
    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        last_error = None
        for _ in range(4 * len(self.slots) + 4):
            slot, wait = self._pick()
            if wait > 0:
                if wait > self.max_wait:
                    soon = ", ".join(f"{s['model']} in {s['free_in_s']:.0f}s ({s['reason'] or 'paced'})" for s in self.status())
                    return LLMTurn(error=f"all models in the rotation are rate-limited (HTTP 429): {soon}")
                self.log(f"all models busy; waiting {wait:.0f}s for {slot.llm.name}")
                self.sleep(wait)
            if slot is not self.current:
                self.log(f"switching {self.current.llm.name} -> {slot.llm.name}")
                self.current = slot
            slot.calls.append(self.clock())
            slot.used += 1
            try:
                turn = slot.llm.call_once(system, self._portable(messages, slot.llm.name), tools)
            except Exception as e:      # noqa: BLE001 - classified below; never crash the agent loop
                kind, delay = classify(e)
                last_error = e
                if kind == "fatal":
                    return LLMTurn(error=f"{type(e).__name__}" + (f" (HTTP {e.code})" if getattr(e, "code", None) else "")
                                         + f" on {slot.llm.name}: {_short(e)}")
                cool = {"minute": max(delay or DEFAULT_COOLDOWN, 5.0), "overload": delay or OVERLOAD_COOLDOWN,
                        "day": max(delay or 0.0, DAY_COOLDOWN)}[kind]
                slot.cool_until = self.clock() + cool
                slot.reason = {"minute": "per-minute limit", "day": "daily limit used up", "overload": "overloaded"}[kind]
                self.log(f"{slot.llm.name}: {slot.reason}; cooling down {cool:.0f}s")
                continue
            if turn.raw is not None:
                self._made_by[id(turn.raw)] = (turn.raw, slot.llm.name)
            turn.usage = dict(turn.usage or {}, model=slot.llm.name)
            return turn
        return LLMTurn(error=f"gave up after repeated provider errors (HTTP {getattr(last_error, 'code', '?')}): {_short(last_error)}")


def _short(e) -> str:
    from c360.layer2.llm.gemini import _safe_message
    return _safe_message(e) if e is not None else ""
