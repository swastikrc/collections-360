"""LLM providers for Layer 2. `get_llm()` returns the configured provider (Gemini)."""


def get_llm():
    from c360.layer2.llm.gemini import GeminiLLM
    return GeminiLLM()
