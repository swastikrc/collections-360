"""Provider interface for the Layer 2 agent. Swap the model provider by writing one file that
implements `LLM.generate`; nothing else in Layer 2 knows which provider is behind it."""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict            # JSON Schema (object). Keep it simple: strings, numbers, enums, arrays.


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Message:
    role: str                   # "user" | "assistant" | "tool"
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)    # assistant turns
    tool_call_id: str | None = None                              # tool turns
    name: str | None = None                                      # tool turns: the tool's name
    raw: Any = None             # provider-native content to replay verbatim (e.g. thought signatures)


@dataclass
class LLMTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    usage: dict = field(default_factory=dict)
    error: str | None = None    # provider-side failure (safety block, exhausted retries, ...)


class LLM(Protocol):
    name: str

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        """One model turn. An empty `tools` list means tool calling is disabled for this turn."""
        ...
