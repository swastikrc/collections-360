"""Google Gemini provider (google-genai SDK). The only file that knows about Gemini.

    python -m c360.layer2.llm.gemini        # list the models this API key can use

The model name comes from C360_L2_MODEL and the key from GEMINI_API_KEY. Neither is hard-coded,
and the key is never printed or logged. Automatic function calling is disabled: the agent runs
the loop itself so every tool call passes through the validator.
"""
import time

from c360.layer2 import config
from c360.layer2.llm.base import LLMTurn, Message, ToolCall, ToolSpec

RETRY_CODES = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3


class LLMConfigError(RuntimeError):
    pass


class GeminiLLM:
    def __init__(self, model=None, api_key=None, temperature=None, client=None, max_retries=MAX_RETRIES, backoff=2.0):
        s = config.settings()
        self.name = model or s.model
        if not self.name:
            raise LLMConfigError("No model set. Export C360_L2_MODEL (list what your key can use with "
                                 "`python -m c360.layer2.llm.gemini`).")
        self.temperature = s.temperature if temperature is None else temperature
        self.max_retries = max_retries
        self.backoff = backoff
        self._declared: list[ToolSpec] = []
        if client is not None:
            self._client = client
        else:
            key = api_key or config.api_key()
            if not key:
                raise LLMConfigError("No API key. Export GEMINI_API_KEY.")
            from google import genai
            self._client = genai.Client(api_key=key)

    # ---- translation -------------------------------------------------------------------------------
    @staticmethod
    def _contents(messages: list[Message]):
        from google.genai import types
        contents, pending = [], []

        def flush():
            if pending:
                contents.append(types.Content(role="user", parts=list(pending)))
                pending.clear()

        for m in messages:
            if m.role == "tool":
                pending.append(types.Part(function_response=types.FunctionResponse(
                    name=m.name or "tool", id=m.tool_call_id, response={"result": m.text or ""})))
                continue
            flush()
            if m.role == "assistant":
                if isinstance(m.raw, types.Content):
                    contents.append(m.raw)        # replay verbatim: keeps thought signatures intact
                    continue
                parts = []
                if m.text:
                    parts.append(types.Part(text=m.text))
                for c in m.tool_calls:
                    parts.append(types.Part(function_call=types.FunctionCall(name=c.name, args=c.args, id=c.id)))
                contents.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
            else:
                contents.append(types.Content(role="user", parts=[types.Part(text=m.text or "")]))
        flush()
        return contents

    def _config(self, system: str, tools: list[ToolSpec]):
        from google.genai import types
        kwargs = dict(system_instruction=system, temperature=self.temperature,
                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        if tools:
            self._declared = list(tools)
        declared = tools or self._declared
        if declared:
            kwargs["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in declared])]
            # No tools passed = tool calling disabled for this turn. The declarations stay so the
            # earlier function calls in the conversation remain valid.
            mode = types.FunctionCallingConfigMode.AUTO if tools else types.FunctionCallingConfigMode.NONE
            kwargs["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=mode))
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _turn(response) -> LLMTurn:
        usage = {}
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage = {"input_tokens": getattr(meta, "prompt_token_count", None),
                     "output_tokens": getattr(meta, "candidates_token_count", None),
                     "total_tokens": getattr(meta, "total_token_count", None)}
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            return LLMTurn(error=f"request blocked by the provider's safety filter ({_name(feedback.block_reason)})", raw=response, usage=usage)
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return LLMTurn(error="the provider returned no candidates", raw=response, usage=usage)
        cand = candidates[0]
        content = getattr(cand, "content", None)
        parts = (getattr(content, "parts", None) or []) if content is not None else []
        texts, calls = [], []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(ToolCall(name=fc.name, args=dict(fc.args or {}), id=getattr(fc, "id", None)))
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                texts.append(part.text)
        if not texts and not calls:
            reason = _name(getattr(cand, "finish_reason", None)) or "empty response"
            return LLMTurn(error=f"the provider returned no content (finish reason: {reason})", raw=content, usage=usage)
        return LLMTurn(text="".join(texts) or None, tool_calls=calls, raw=content, usage=usage)

    # ---- the call ----------------------------------------------------------------------------------
    def call_once(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        """One request, no retries; provider errors are raised (the model rotation classifies them)."""
        return self._turn(self._client.models.generate_content(model=self.name, contents=self._contents(messages),
                                                               config=self._config(system, tools)))

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        contents = self._contents(messages)
        cfg = self._config(system, tools)
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._turn(self._client.models.generate_content(model=self.name, contents=contents, config=cfg))
            except Exception as e:      # noqa: BLE001 - classify below; never let a provider error crash the loop
                last = e
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                transient = code in RETRY_CODES or type(e).__name__ in ("ServerError", "TimeoutException", "ConnectError",
                                                                         "ReadTimeout", "ConnectTimeout", "RemoteProtocolError")
                if not transient or attempt == self.max_retries:
                    break
                time.sleep(self.backoff * (2 ** attempt))
        return LLMTurn(error=f"{type(last).__name__}" + (f" (HTTP {getattr(last, 'code', '')})" if getattr(last, "code", None) else "")
                       + f": {_safe_message(last)}")

    # ---- model discovery ---------------------------------------------------------------------------
    def list_models(self) -> list[dict]:
        out = []
        for m in self._client.models.list():
            actions = list(getattr(m, "supported_actions", None) or [])
            if "generateContent" in actions:
                out.append({"name": (m.name or "").removeprefix("models/"), "display_name": getattr(m, "display_name", None),
                            "input_token_limit": getattr(m, "input_token_limit", None)})
        return out


def _name(enum_value) -> str:
    return getattr(enum_value, "name", None) or (str(enum_value) if enum_value is not None else "")


def _safe_message(e) -> str:
    """A short provider error message with anything key-like removed."""
    text = str(getattr(e, "message", None) or e).splitlines()[0][:240]
    key = config.api_key()
    return text.replace(key, "[key]") if key else text


def main():
    try:
        llm = GeminiLLM(model=config.settings().model or "unset")
    except LLMConfigError as e:
        raise SystemExit(str(e))
    print("Models this key can use with generateContent (pick the strongest Pro-tier one for C360_L2_MODEL):")
    for m in sorted(llm.list_models(), key=lambda m: m["name"]):
        print(f"  {m['name']:<40} {m['display_name'] or '':<32} input limit {m['input_token_limit']}")


if __name__ == "__main__":
    main()
