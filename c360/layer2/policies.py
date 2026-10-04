"""The bank's reference documents (policies, procedures, terms, regulatory summaries, call scripts,
FAQ, incident reports, release notes), split into citable sections and searched by keyword.

Built once by `publish_l2` into `l2.policy_sections`; searched at run time by the `search_policy`
tool from that table, so the agent still only reads the serving database.

Version-aware: superseded documents are never stored. When current documents disagree, the one
with more authority wins (policy > terms > procedure > regulatory summary > call script / incident
> FAQ / release note): FAQ and call-script sections are ranked behind any governing section that
is reasonably relevant. Section splitting and ranking follow Layer 3's policy library (the two
copies are independent so neither layer depends on the other).
"""
import csv
import math
import re
from pathlib import Path

AUTHORITY = {"policy": 6, "hardship_programme_terms": 5, "procedure": 4, "regulatory_summary": 3,
             "call_script": 2, "data_contract": 2, "incident_report": 2, "release_note": 1, "faq": 1}
DEMOTED = {"faq", "call_script"}
STOP = set("a an the of to in on for and or is are be by with at as it this that we our you your i my me do does "
           "can what how when which who if not no any from was were will would should must may there their they".split())
COLUMNS = ["cite", "doc_id", "section", "doc_type", "title", "heading", "version", "language", "effective_date",
           "authority", "section_text"]
MAX_SECTION_CHARS = 4000


def tokens(text):
    return [w for w in re.findall(r"[a-z0-9§.]+", str(text or "").lower().replace("-", " ")) if w not in STOP and len(w) > 1]


def _split(doc_id, meta, body):
    """Sections at '## §x.y' / '## Qn' headings or '**Qn. …**' FAQ questions; a short document is one section."""
    body = re.sub(r"^>.*\n", "", body, flags=re.M)                 # the "synthetic document" banner
    title_m = re.search(r"^# (.+)$", body, flags=re.M)
    title = title_m.group(1).strip() if title_m else meta.get("title", doc_id)
    parts = re.split(r"^(## .+|\*\*Q\d+\..+?\*\*)\s*$", body, flags=re.M)
    sections = []
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            head = parts[i].strip("#* ").strip()
            text = parts[i + 1].strip()
            m = re.match(r"(§[\d.]+|Q\d+)", head)
            sections.append((m.group(1).rstrip(".") if m else "", head, (head + "\n" + text) if head.startswith("Q") else text))
    else:
        sections.append(("", title, re.sub(r"^# .+$", "", body, flags=re.M).strip()))
    out = []
    for sec, head, text in sections:
        doc_type = meta.get("doc_type", "")
        out.append({"cite": f"{doc_id} {sec}".strip(), "doc_id": doc_id, "section": sec, "doc_type": doc_type,
                    "title": title, "heading": head, "version": meta.get("version", ""),
                    "language": meta.get("language", ""), "effective_date": meta.get("effective_date", ""),
                    "authority": AUTHORITY.get(doc_type, 1), "section_text": text[:MAX_SECTION_CHARS]})
    return out


def load_sections(data_root, base_dirs=("files", "")):
    """Current (not superseded) document sections from <data_root>/data/reference_documents.csv.
    `file_path` is resolved under each of `base_dirs` in turn (the release keeps documents under files/).
    Returns (rows, stats)."""
    root = Path(data_root)
    index = root / "data" / "reference_documents.csv"
    stats = {"documents": 0, "superseded_skipped": 0, "missing_files": 0, "sections": 0}
    if not index.exists():
        stats["note"] = f"no reference_documents.csv under {root / 'data'}"
        return [], stats
    rows = []
    with open(index, newline="", encoding="utf-8") as f:
        for meta in csv.DictReader(f):
            if (meta.get("superseded_by") or "").strip():
                stats["superseded_skipped"] += 1
                continue
            rel = meta.get("file_path") or ""
            path = next((root / b / rel if b else root / rel for b in base_dirs if (root / b / rel if b else root / rel).exists()), None)
            if path is None:
                stats["missing_files"] += 1
                continue
            stats["documents"] += 1
            rows.extend(_split(meta["doc_id"], meta, path.read_text(encoding="utf-8")))
    stats["sections"] = len(rows)
    return rows, stats


class Index:
    """BM25 over section rows, with the authority rule applied to the top results."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.toks = [tokens(f"{r['title']} {r['heading']} {r['section_text']}") for r in self.rows]
        self.n = len(self.rows) or 1
        self.df = {}
        for t in self.toks:
            for w in set(t):
                self.df[w] = self.df.get(w, 0) + 1
        self.avgdl = (sum(len(t) for t in self.toks) / self.n) or 1.0

    def _bm25(self, q, toks, k1=1.4, b=0.6):
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        score = 0.0
        for w in q:
            if w in tf:
                idf = math.log(1 + (self.n - self.df.get(w, 0) + 0.5) / (self.df.get(w, 0) + 0.5))
                score += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(toks) / self.avgdl))
        return score

    def search(self, query, k=4, language="EN", doc_types=None):
        q = tokens(query)
        scored = []
        for r, t in zip(self.rows, self.toks):
            if language and r.get("language") and r["language"] != language:
                continue
            if doc_types and r.get("doc_type") not in doc_types:
                continue
            s = self._bm25(q, t)
            if s > 0:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        top = scored[0][0]
        near = [x for x in scored[:6] if x[0] >= 0.25 * top]
        rest = [x for x in scored if not any(x is y for y in near)]
        governing = [x for x in near if x[1].get("doc_type") not in DEMOTED]
        guidance = [x for x in near if x[1].get("doc_type") in DEMOTED]
        return [dict(r, score=round(s, 3)) for s, r in (governing + guidance + rest)[:k]]
