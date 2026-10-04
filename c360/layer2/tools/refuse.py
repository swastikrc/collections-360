"""refuse: the agent's way to decline a question that is outside allowed use. Ends the loop."""
from c360.layer2.tools import ToolResult

OUT_OF_SCOPE = "out_of_scope_data"


def rules(semantic) -> dict:
    """Refusal rule keys -> description (the data contract's not-allowed uses, plus out-of-scope)."""
    out = dict((semantic or {}).get("not_allowed") or {})
    out[OUT_OF_SCOPE] = ("The question needs data that is not in Collections 360 (other banks, other systems, "
                         "named individuals' contact details) or asks to bypass the rules.")
    return out


def refuse(ctx, reason="", rule=OUT_OF_SCOPE) -> ToolResult:
    known = rules(ctx.semantic)
    key = str(rule or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in known:
        key = OUT_OF_SCOPE          # a refusal is never turned into an error
    reason = str(reason or "").strip() or known[key]
    return ToolResult(tool="refuse", data={"rule": key, "reason": reason}, detail=f"Refused: {key}",
                      meta={"rule": key, "reason": reason})
