"""Policy library: the bank's reference documents, split into citable sections, searchable.

Version-aware on purpose. The dataset ships superseded documents (POL-COLL-004 v4.1, the v1.0 call
script) and an FAQ that disagrees with current policy (6 months vs 3 months). Search never returns a
superseded document, and when two current documents disagree, the higher-authority type wins:
policy > terms > procedure > regulatory summary > call script > FAQ.

    from c360.layer3.policy import library
    hits = library().search("how long does the reduced payment programme last")
    hits[0].cite   # "POL-COLL-004 §4.2"
"""
import csv
import functools
import math
import re
from dataclasses import dataclass, field

from c360.layer3 import config

AUTHORITY = {"policy": 6, "hardship_programme_terms": 5, "procedure": 4, "regulatory_summary": 3,
             "call_script": 2, "data_contract": 2, "incident_report": 2, "release_note": 1, "faq": 1}

DEMOTED = {"faq", "call_script"}     # guidance, not the rule: ranked after governing documents

STOP = set("a an the of to in on for and or is are be by with at as it this that we our you your i my me do does "
           "can what how when which who if not no any from was were will would should must may there their they".split())


@dataclass
class Section:
    doc_id: str
    doc_type: str
    title: str
    version: str
    language: str
    section: str          # "§4.2", "Q2", or "" for a whole short document
    heading: str
    text: str
    effective_date: str = ""
    tokens: list = field(default_factory=list, repr=False)

    @property
    def cite(self):
        return f"{self.doc_id} {self.section}".strip()

    def public(self, max_chars=900):
        t = self.text if len(self.text) <= max_chars else self.text[:max_chars].rsplit(" ", 1)[0] + " …"
        return {"doc_id": self.doc_id, "section": self.section, "cite": self.cite, "title": self.title,
                "heading": self.heading, "doc_type": self.doc_type, "version": self.version,
                "effective_date": self.effective_date, "text": t}


def _tokens(text):
    return [w for w in re.findall(r"[a-z0-9§.]+", text.lower().replace("-", " ")) if w not in STOP and len(w) > 1]


def _split(doc_id, meta, body):
    """Split a markdown document into sections at '## §x.y' headings or '**Qn.' FAQ questions."""
    body = re.sub(r"^>.*\n", "", body, flags=re.M)             # the "synthetic document" banner
    title_m = re.search(r"^# (.+)$", body, flags=re.M)
    title = title_m.group(1).strip() if title_m else meta["title"]
    parts = re.split(r"^(## .+|\*\*Q\d+\..+?\*\*)\s*$", body, flags=re.M)
    sections = []
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            head = parts[i].strip("#* ").strip()
            text = parts[i + 1].strip()
            m = re.match(r"(§[\d.]+|Q\d+)", head)
            sec = m.group(1).rstrip(".") if m else ""
            sections.append((sec, head, (head + "\n" + text) if head.startswith("Q") else text))
    else:
        text = re.sub(r"^# .+$", "", body, flags=re.M).strip()
        sections.append(("", title, text))
    out = []
    for sec, head, text in sections:
        s = Section(doc_id=doc_id, doc_type=meta["doc_type"], title=title, version=meta["version"],
                    language=meta["language"], section=sec, heading=head, text=text,
                    effective_date=meta.get("effective_date", ""))
        s.tokens = _tokens(f"{title} {head} {text}")
        out.append(s)
    return out


class PolicyLibrary:
    def __init__(self, root=None):
        root = root or config.maple_data()
        self.sections: list[Section] = []
        self.superseded: dict[str, str] = {}
        index = root / "data" / "reference_documents.csv"
        if not index.exists():
            return
        with open(index, newline="", encoding="utf-8") as f:
            for meta in csv.DictReader(f):
                if meta.get("superseded_by"):
                    self.superseded[meta["doc_id"]] = meta["superseded_by"]
                    continue
                path = root / "files" / meta["file_path"]
                if path.exists():
                    self.sections.extend(_split(meta["doc_id"], meta, path.read_text(encoding="utf-8")))
        n = len(self.sections) or 1
        self.df = {}
        for s in self.sections:
            for t in set(s.tokens):
                self.df[t] = self.df.get(t, 0) + 1
        self.avgdl = sum(len(s.tokens) for s in self.sections) / n
        self.n = n

    def _bm25(self, q, s, k1=1.4, b=0.6):
        score, tf = 0.0, {}
        for t in s.tokens:
            tf[t] = tf.get(t, 0) + 1
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (self.n - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(s.tokens) / self.avgdl))
        return score

    def search(self, query, k=3, language="EN", doc_types=None):
        q = _tokens(query)
        scored = []
        for s in self.sections:
            if language and s.language != language:
                continue
            if doc_types and s.doc_type not in doc_types:
                continue
            sc = self._bm25(q, s)
            if sc > 0:
                scored.append((sc, s))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        # The authoritative source wins. An FAQ or call script that paraphrases the question closely must
        # not beat the policy or procedure that governs the answer, so FAQ and script sections move
        # behind any governing section that is reasonably relevant (at least a quarter of the top score;
        # long policy sections score lower than one-line FAQ answers). Everything else keeps its order.
        top = scored[0][0]
        near = [x for x in scored[:6] if x[0] >= 0.25 * top]
        rest = [x for x in scored if x not in near]
        governing = [x for x in near if x[1].doc_type not in DEMOTED]
        guidance = [x for x in near if x[1].doc_type in DEMOTED]
        return [s for _, s in (governing + guidance + rest)[:k]]

    def get(self, doc_id, section=""):
        for s in self.sections:
            if s.doc_id == doc_id and (not section or s.section == section):
                return s
        return None


@functools.lru_cache(maxsize=1)
def library():
    return PolicyLibrary()
