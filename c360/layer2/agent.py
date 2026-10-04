"""The Layer 2 agent loop.

The model decides; code executes and checks. Every limit here is enforced in code, not asked for
in the prompt: the tool-call budget, duplicate-call detection, the stop after two consecutive tool
errors, and the single repair attempt for a malformed final answer.
"""
import json
import time
from dataclasses import dataclass

from c360.layer2 import audit, guards, prompts
from c360.layer2.answer import ANSWER_SCHEMA_TEXT, Answer, Meta, Refusal, Trace, TraceStep, parse_llm_answer
from c360.layer2.llm.base import LLM, Message
from c360.layer2.tools import jsonable, registry

BUDGET_MSG = "Tool budget used. Answer now with what you have, and say what is uncertain. Reply with only the JSON answer."
ERROR_MSG = ("Two tool calls in a row failed. Stop calling tools. Explain what you could and could not find. "
             "Reply with only the JSON answer.")
REPAIR_MSG = "Return only valid JSON for this schema, with no other text:\n" + ANSWER_SCHEMA_TEXT
MAX_CONSECUTIVE_ERRORS = 2
MAX_HISTORY_TURNS = 6


@dataclass
class AgentResult:
    answer: Answer
    trace: Trace


def canonical(args) -> str:
    return json.dumps(jsonable(args), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Agent:
    def __init__(self, llm: LLM, ctx: registry.ToolContext, audit_path=None, write_audit=True):
        self.llm = llm
        self.ctx = ctx
        self.tools = registry.specs(ctx)
        self.system = prompts.render_system(ctx)
        self.audit_path = audit_path
        self.write_audit = write_audit

    # ---- public --------------------------------------------------------------------------------
    def ask(self, question, history=None) -> Answer:
        return self.run(question, history).answer

    def run(self, question, history=None) -> AgentResult:
        t0 = time.time()
        question = str(question or "")
        trace = Trace(question=question)
        answer = self._answer(question, history or [], trace)
        answer.meta = Meta(snapshot_date=str(self.ctx.serving.snapshot_date), build_ts=self.ctx.serving.build_ts,
                           model=getattr(self.llm, "name", None), tool_calls=trace.tool_calls,
                           latency_ms=int((time.time() - t0) * 1000))
        answer = guards.output_guard(answer, trace, question)
        if self.write_audit:
            audit.write(question, answer, trace, self.audit_path)
        return AgentResult(answer, trace)

    # ---- the loop --------------------------------------------------------------------------------
    def _answer(self, question, history, trace) -> Answer:
        decision = guards.input_guard(question)
        if decision.kind != "ok":
            trace.stopped = "input_guard"
            return Answer(status="refused", answer_text=decision.message, interpretation=[],
                          refusal=Refusal(rule=decision.rule, reason=decision.message))

        max_calls = self.ctx.settings.max_tool_calls
        messages = list(history)[-2 * MAX_HISTORY_TURNS:] + [Message(role="user", text=question)]
        calls, seen, consecutive_errors = 0, set(), 0

        while True:
            turn = self._generate(messages, self.tools, trace)
            if turn.error:
                return self._llm_failed(turn.error, trace)
            if not turn.tool_calls:
                return self._finalize(turn.text, messages, trace)

            if trace.plan is None and turn.text and turn.text.strip():
                trace.plan = turn.text.strip()
            messages.append(Message(role="assistant", text=turn.text, tool_calls=turn.tool_calls, raw=turn.raw))

            over_budget = False
            for call in turn.tool_calls:
                args = call.args if isinstance(call.args, dict) else {}
                if over_budget:
                    self._tool_reply(messages, call, _error_payload(call.name, "not executed: the tool budget is used"))
                    trace.steps.append(TraceStep(tool=call.name, args=args, executed=False, note="over tool budget"))
                    continue
                if call.name == "refuse":
                    result = registry.dispatch("refuse", args, self.ctx)
                    trace.steps.append(TraceStep(tool="refuse", args=args, result=result))
                    rule, reason = result.data["rule"], result.data["reason"]
                    return Answer(status="refused", answer_text=reason, refusal=Refusal(rule=rule, reason=reason))
                calls += 1
                if calls > max_calls:
                    over_budget = True
                    self._tool_reply(messages, call, _error_payload(call.name, "not executed: the tool budget is used"))
                    trace.steps.append(TraceStep(tool=call.name, args=args, executed=False, note="over tool budget"))
                    continue
                key = (call.name, canonical(args))
                if key in seen:
                    self._tool_reply(messages, call, _error_payload(call.name, "duplicate call — you already have this result"))
                    trace.steps.append(TraceStep(tool=call.name, args=args, executed=False, note="duplicate call"))
                    continue
                seen.add(key)
                result = registry.dispatch(call.name, args, self.ctx)        # validated, read-only, sanity-checked
                trace.steps.append(TraceStep(tool=call.name, args=args, result=result))
                consecutive_errors = consecutive_errors + 1 if not result.ok else 0
                self._tool_reply(messages, call, result.to_llm())

            if over_budget:
                trace.stopped = "tool_budget"
                return self._force_final(BUDGET_MSG, messages, trace,
                                         f"Stopped after {max_calls} tool calls (the limit); the answer uses what was found by then.")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                trace.stopped = "consecutive_errors"
                return self._force_final(ERROR_MSG, messages, trace,
                                         "Stopped after two tool errors in a row; the answer may be incomplete.")

    # ---- helpers ---------------------------------------------------------------------------------
    def _generate(self, messages, tools, trace):
        trace.llm_calls += 1
        return self.llm.generate(self.system, messages, tools)

    @staticmethod
    def _tool_reply(messages, call, text):
        messages.append(Message(role="tool", text=text, tool_call_id=call.id, name=call.name))

    def _llm_failed(self, error, trace) -> Answer:
        trace.stopped = "llm_error"
        found = [r for r in trace.results if r.ok]
        text = "The language model could not complete this request" + (
            ", although some data was retrieved (see the sources below)." if found else ".")
        return Answer(status="partial", answer_text=text, warnings=[f"model error: {str(error)[:300]}"])

    def _force_final(self, instruction, messages, trace, warning) -> Answer:
        messages.append(Message(role="user", text=instruction))
        turn = self._generate(messages, [], trace)          # tools disabled
        if turn.error:
            answer = self._llm_failed(turn.error, trace)
        else:
            answer = self._finalize(turn.text, messages, trace)
        if answer.status == "answered":
            answer.status = "partial"
        answer.warnings.insert(0, warning)
        return answer

    def _finalize(self, text, messages, trace) -> Answer:
        """Parse the model's final message; one repair attempt; then fall back to its raw text."""
        trace.raw_final = text
        try:
            return self._to_answer(parse_llm_answer(text or ""))
        except ValueError:
            pass
        trace.repaired = True
        messages.append(Message(role="assistant", text=text or ""))
        messages.append(Message(role="user", text=REPAIR_MSG))
        turn = self._generate(messages, [], trace)
        if not turn.error:
            try:
                return self._to_answer(parse_llm_answer(turn.text or ""))
            except ValueError:
                pass
        raw = (text or turn.text or "").strip()
        return Answer(status="partial", answer_text=raw or "The model did not produce an answer.",
                      warnings=["The model did not return a structured answer; its raw text is shown unformatted."])

    @staticmethod
    def _to_answer(parsed) -> Answer:
        answer = Answer(**parsed.model_dump())
        if answer.status == "refused" and answer.refusal is None:
            answer.refusal = Refusal(rule="out_of_scope_data", reason=answer.answer_text)
        if answer.refusal is not None and answer.status != "refused":
            answer.refusal = None
        return answer


def _error_payload(tool, message) -> str:
    from c360.layer2.tools import failure
    return failure(tool, message).to_llm()


def history_messages(question, answer) -> list[Message]:
    """Two messages summarising a finished exchange, for follow-up questions in the REPL."""
    summary = answer.answer_text
    if answer.interpretation:
        summary = f"[Interpreted as: {' · '.join(answer.interpretation)}] {summary}"
    return [Message(role="user", text=question), Message(role="assistant", text=summary)]
