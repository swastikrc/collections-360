"""search_text: retrieval over agent notes and call transcripts.

Full-text (BM25) when the serving database has the index, otherwise keyword matching. Either way
the model gets short snippets with their ids, wrapped and marked as data: text written by a
customer or an agent is evidence to quote, never an instruction to follow.
"""
import re

from c360.layer2 import periods
from c360.layer2.tools import ToolResult, failure, compute_metric as cm

DEFAULT_LIMIT, MAX_LIMIT, SNIPPET_CHARS, MAX_TERMS = 20, 30, 400, 8
SOURCES = {
    "notes": dict(table="agent_notes", alias="n", id="note_id", text="note_text", ts="note_ts", kind="note", extra="note_type"),
    "transcripts": dict(table="call_transcripts", alias="t", id="transcript_id", text="transcript_text", ts="call_start_ts",
                        kind="transcript", extra="direction"),
}
STOPWORDS = {"the", "and", "for", "are", "was", "were", "with", "that", "this", "from", "have", "has", "had", "not", "but",
             "what", "why", "who", "how", "when", "where", "which", "their", "they", "them", "about", "any", "all", "can",
             "customers", "customer", "say", "said", "saying", "says", "does", "did", "our", "you", "your", "its", "into",
             "notes", "note", "calls", "call", "transcripts", "transcript", "reason", "reasons", "give", "given"}


def terms_of(query) -> list[str]:
    words = re.findall(r"[a-z0-9']+", str(query).lower())
    out = []
    for w in words:
        if len(w) >= 3 and w not in STOPWORDS and w not in out:
            out.append(w)
    return out[:MAX_TERMS]


def snippet(text, terms, width=SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    low = text.lower()
    hits = [low.find(t) for t in terms if low.find(t) >= 0]
    start = max(0, (min(hits) if hits else 0) - width // 4)
    start = min(start, max(0, len(text) - width))
    piece = text[start:start + width]
    return ("…" if start > 0 else "") + piece[: width - 2 if start > 0 else width - 1] + ("…" if start + width < len(text) else "")


def _search_one(ctx, src, query, terms, where, params, limit, mode):
    a = src["alias"]
    join = f" LEFT JOIN l2.collections_cases c ON c.case_id = {a}.case_id" if any(w.startswith("c.") for w in where) else ""
    select = f"SELECT {a}.{src['id']} AS id, {a}.case_id, {a}.c360_id, {a}.{src['ts']} AS ts, {a}.{src['extra']} AS extra, {a}.{src['text']} AS body"
    conds = [f"{a}.{src['text']} IS NOT NULL"] + where
    p = dict(params)
    if mode == "fts":
        p["q"] = query
        sql = (f"{select}, fts_l2_{src['table']}.match_bm25({a}.{src['id']}, $q) AS score\nFROM l2.{src['table']} {a}{join}\n"
               f"WHERE score IS NOT NULL AND " + " AND ".join(conds) + f"\nORDER BY score DESC, ts DESC\nLIMIT {limit}")
    elif mode == "recent":
        sql = (f"{select}, 0 AS score\nFROM l2.{src['table']} {a}{join}\nWHERE " + " AND ".join(conds)
               + f"\nORDER BY ts DESC\nLIMIT {limit}")
    else:
        parts = []
        for i, t in enumerate(terms):
            p[f"t{i}"] = f"%{t}%"
            parts.append(f"CASE WHEN {a}.{src['text']} ILIKE $t{i} THEN 1 ELSE 0 END")
        score = " + ".join(parts)
        sql = (f"{select}, {score} AS score\nFROM l2.{src['table']} {a}{join}\n"
               f"WHERE ({score}) > 0 AND " + " AND ".join(conds) + f"\nORDER BY score DESC, ts DESC\nLIMIT {limit}")
    columns, rows = ctx.serving.execute(sql, p)
    return [dict(zip(columns, r)) for r in rows], cm.inline(sql, p)


def search_text(ctx, query="", source="both", case_id=None, c360_id=None, period=None, date_from=None, date_to=None,
                queue=None, product=None, limit=DEFAULT_LIMIT, filters=None) -> ToolResult:
    tool = "search_text"
    if isinstance(filters, dict):       # the nested form is accepted too
        case_id, c360_id = filters.get("case_id", case_id), filters.get("c360_id", c360_id)
        period, date_from, date_to = filters.get("period", period), filters.get("date_from", date_from), filters.get("date_to", date_to)
        queue, product, source = filters.get("queue", queue), filters.get("product", product), filters.get("source", source)
    query = str(query or "").strip()
    terms = terms_of(query)
    if not terms and not (case_id or c360_id):
        return failure(tool, "Give a few keywords to search for (for example: 'lost job', 'dispute late fee'), "
                             "or a case_id / c360_id to list that customer's most recent notes.")
    source = str(source or "both").lower()
    if source not in ("notes", "transcripts", "both"):
        return failure(tool, "source must be one of: notes, transcripts, both.")
    try:
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    start = end = None
    try:
        if period:
            start, end, _ = ctx.resolve_period(period)
        if date_from:
            start = ctx.resolve_period(date_from)[0]
        if date_to:
            end = ctx.resolve_period(date_to)[1]
    except periods.PeriodError as e:
        return failure(tool, str(e))

    wanted = [SOURCES[s] for s in (("notes", "transcripts") if source == "both" else (source,))
              if SOURCES[s]["table"] in ctx.serving.tables]
    hits, sqls, mode_used = [], [], ctx.serving.text_mode
    for src in wanted:
        a = src["alias"]
        where, params = [], {}
        if case_id:
            where.append(f"{a}.case_id = $case_id")
            params["case_id"] = str(case_id)
        if c360_id:
            where.append(f"{a}.c360_id = $c360_id")
            params["c360_id"] = str(c360_id)
        if start is not None:
            where.append(f"CAST({a}.{src['ts']} AS DATE) >= $date_from")
            params["date_from"] = start
        if end is not None:
            where.append(f"CAST({a}.{src['ts']} AS DATE) <= $date_to")
            params["date_to"] = end
        if queue:
            where.append("c.queue = $queue")
            params["queue"] = str(queue)
        if product:
            where.append("c.primary_product = $product")
            params["product"] = str(product)
        try:
            rows, shown = [], None
            if not terms:       # no keywords, but a case or customer was named: their most recent text
                rows, shown = _search_one(ctx, src, query, terms, where, params, limit, "recent")
                mode_used = "recent"
            elif ctx.serving.text_mode == "fts":
                rows, shown = _search_one(ctx, src, query, terms, where, params, limit, "fts")
            if not rows and terms:        # keyword fallback: no index, or the index found nothing
                rows, shown = _search_one(ctx, src, query, terms, where, params, limit, "like")
                if ctx.serving.text_mode == "fts" and rows:
                    mode_used = "fts+like"
        except Exception as e:      # noqa: BLE001
            return failure(tool, f"Text search failed: {str(e).splitlines()[0][:300]}")
        sqls.append(shown)
        hits.append([dict(r, kind=src["kind"]) for r in rows])

    # interleave the sources by rank so neither crowds the other out
    merged = []
    for i in range(max((len(h) for h in hits), default=0)):
        for h in hits:
            if i < len(h):
                merged.append(h[i])
    merged = merged[:limit]

    data, ids = [], []
    for r in merged:
        data.append({"source": r["kind"], "id": str(r["id"]), "case_id": r["case_id"],
                     "date": r["ts"].strftime("%Y-%m-%d") if r["ts"] else None, "type": r["extra"],
                     "text": snippet(r["body"], terms)})
        ids.append(str(r["id"]))
        if r["case_id"]:
            ids.append(str(r["case_id"]))
    warnings = [] if data else ["No notes or transcripts matched: try other keywords or fewer filters."]
    kinds = sorted({d["source"] + "s" for d in data}) or [s["kind"] + "s" for s in wanted]
    return ToolResult(tool=tool, data=data, row_count=len(data), sql="\n;\n".join(s for s in sqls if s),
                      source_ids=list(dict.fromkeys(ids)), warnings=warnings,
                      detail=f"Text search over {' and '.join(kinds)}: \"{query}\"",
                      meta={"query": query, "search_mode": mode_used, "sources": kinds,
                            "period": periods.label(start, end) if start and end else None})
