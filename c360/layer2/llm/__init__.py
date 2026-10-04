"""LLM providers for Layer 2. `get_llm()` returns the configured provider (Gemini).

C360_L2_MODELS (comma-separated, priority order) turns on the model rotation (llm/rotation.py);
otherwise the single model in C360_L2_MODEL is used."""
import os


def get_llm():
    pool = os.environ.get("C360_L2_MODELS", "").strip()
    if pool:
        from c360.layer2.llm.rotation import RotatingGeminiLLM
        return RotatingGeminiLLM(pool, max_wait=float(os.environ.get("C360_L2_MAX_WAIT", "75")))
    from c360.layer2.llm.gemini import GeminiLLM
    return GeminiLLM()
