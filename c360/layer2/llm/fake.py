"""Fake LLMs for offline tests and `eval --fake`. They never call a network.

ScriptedLLM  returns a fixed list of turns, in order (unit tests of the agent loop).
PlanLLM      plays a tool plan, then writes a final answer from the last tool result. It proves the
             pipeline (tools, guards, comparison) end to end. It says nothing about model accuracy.
"""
import json

from c360.layer2.llm.base import LLMTurn, Message, ToolCall, ToolSpec


class ScriptedLLM:
    name = "fake-scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []          # (system, messages snapshot, tool names) per generate() call

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        self.calls.append((system, list(messages), [t.name for t in tools]))
        if not self.turns:
            return LLMTurn(text='{"status": "partial", "answer_text": "The script ran out of turns."}')
        turn = self.turns.pop(0)
        return turn(messages, tools) if callable(turn) else turn


def call(name, _id=None, **args) -> ToolCall:
    return ToolCall(name=name, args=args, id=_id)


def tool_turn(*calls, text="Plan: use the tools.") -> LLMTurn:
    return LLMTurn(text=text, tool_calls=list(calls))


def final_turn(answer_text="Done.", **fields) -> LLMTurn:
    return LLMTurn(text=json.dumps({"status": "answered", "answer_text": answer_text, **fields}))


def last_tool_payload(messages) -> dict | None:
    """The decoded body of the most recent tool result message."""
    for m in reversed(messages):
        if m.role == "tool" and m.text:
            try:
                return json.loads(m.text.split("\n", 1)[1].rsplit("\n", 1)[0])
            except (IndexError, ValueError):
                return None
    return None


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e6 else f"{v:.2f}"
    return str(v)


def compose_answer(payload) -> dict:
    """A plain final answer from one tool result, the way a careful model would report it."""
    if not payload or not payload.get("ok"):
        return {"status": "partial", "answer_text": "The tool call failed: " + str((payload or {}).get("error", "no result")) + "."}
    if "rows" in payload:
        cols, rows = payload["columns"], payload["rows"]
        if not rows:
            return {"status": "partial", "answer_text": "No rows matched."}
        if len(rows) == 1 and not payload.get("group_by") and (len(cols) == 1 or cols[-1] == "value"):
            value = rows[0][-1]
            return {"status": "answered", "interpretation": [payload.get("what", ""), payload.get("period", "")],
                    "answer_text": f"The result is {_fmt(value)}.", "table": None}
        top = rows[0]
        return {"status": "answered", "interpretation": [payload.get("what", ""), payload.get("period", "")],
                "answer_text": f"There are {len(rows)} rows; the first is {_fmt(top[0])} with {_fmt(top[-1])}.",
                "table": {"columns": cols, "rows": rows[:20]}}
    data = payload.get("data")
    if isinstance(data, dict):        # a customer record
        cases = (data.get("collections") or {}).get("cases") or []
        parts = [f"Customer {data.get('_id')} has {(data.get('collections') or {}).get('open_cases', 0)} open cases."]
        for c in cases[:5]:
            parts.append(f"Case {c.get('case_id')} ({c.get('primary_product')}) is in queue {c.get('queue')}, "
                         f"hardship reason {c.get('hardship_reason')}, bucket {c.get('current_bucket_as_stored')}.")
        return {"status": "answered", "answer_text": " ".join(parts)}
    if isinstance(data, list):        # text snippets
        if not data:
            return {"status": "partial", "answer_text": "No notes or transcripts matched."}
        quotes = "; ".join(f"\"{d.get('text', '')[:120]}\" ({d.get('id')})" for d in data[:3])
        return {"status": "answered", "answer_text": f"Found {len(data)} matching notes or transcripts, for example: {quotes}"}
    return {"status": "partial", "answer_text": "The tool returned nothing usable."}


class PlanLLM:
    """Plays `plan` (a list of {"tool": name, "args": {...}}), one call per turn, then answers."""
    name = "fake-plan"

    def __init__(self, plan):
        self.plan = list(plan or [])
        self.step = 0

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        if tools and self.step < len(self.plan):
            item = self.plan[self.step]
            self.step += 1
            return LLMTurn(text="Plan: run the scripted tool call." if self.step == 1 else None,
                           tool_calls=[ToolCall(name=item["tool"], args=dict(item.get("args") or {}), id=f"fake-{self.step}")])
        return LLMTurn(text=json.dumps(compose_answer(last_tool_payload(messages))))
