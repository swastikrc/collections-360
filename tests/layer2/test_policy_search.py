"""search_policy: the bank's reference documents in the serving database, version- and authority-aware.

The synthetic world ships four documents: a current policy (POL-TEST-001 v2.0), its superseded v1.0
(must never be returned), an FAQ that disagrees with the policy, and an incident report.
"""
import pytest

from c360.layer2 import policies
from c360.layer2.agent import Agent
from c360.layer2.llm.fake import ScriptedLLM, call, final_turn, tool_turn
from c360.layer2.tools.registry import dispatch


def test_policy_sections_are_published_without_superseded_versions(serving):
    assert "policy_sections" in serving.tables
    _, rows = serving.execute("SELECT cite, doc_type, section_text FROM l2.policy_sections ORDER BY cite")
    cites = [r[0] for r in rows]
    assert {"POL-TEST-001 §1.1", "POL-TEST-001 §1.2", "FAQ-TEST-001 Q1", "IR-0923"} <= set(cites)
    assert not any(c.startswith("POL-TEST-001-V1") for c in cites)
    assert not any("5 outbound" in r[2] for r in rows), "superseded text must not be stored"


def test_policy_outranks_faq_and_is_cited(ctx):
    r = dispatch("search_policy", {"query": "how many call attempts per week"}, ctx)
    assert r.ok and r.row_count >= 2
    cites = [h["cite"] for h in r.data]
    assert cites[0] == "POL-TEST-001 §1.1"                       # the governing policy, not the FAQ
    assert cites.index("FAQ-TEST-001 Q1") > cites.index("POL-TEST-001 §1.1")
    assert r.source_ids == cites and "3 outbound call attempts" in r.data[0]["text"]
    assert '<tool_result tool="search_policy" trust="data">' in r.to_llm()


def test_search_policy_errors_and_no_match(ctx):
    empty = dispatch("search_policy", {"query": "  "}, ctx)
    assert not empty.ok and "keywords" in empty.error
    none = dispatch("search_policy", {"query": "zzqx flibbertigibbet"}, ctx)
    assert none.ok and none.row_count == 0 and none.warnings
    clipped = dispatch("search_policy", {"query": "call attempts", "limit": 99, "language": "xx"}, ctx)
    assert clipped.ok and clipped.row_count <= 6 and clipped.meta["language"] == "EN"


def test_agent_cites_policy_sections(ctx, tmp_path):
    llm = ScriptedLLM([
        tool_turn(call("search_policy", query="call attempts per week"), text="Plan: look up the contact policy."),
        final_turn("At most 3 outbound call attempts in any rolling 7 days (POL-TEST-001 §1.1).",
                   interpretation=["contact policy", "call attempt limit"]),
    ])
    a = Agent(llm, ctx, audit_path=tmp_path / "audit.jsonl").run("How many call attempts can we make in a week?").answer
    assert a.status == "answered"
    pol = [s for s in a.sources if s.kind == "policy"]
    assert pol and "POL-TEST-001 §1.1" in pol[0].ids and pol[0].sql is None
    assert not [w for w in a.warnings if "not found in results" in w]    # "3" and "7" come from the section text


def test_load_sections_counts_superseded_and_missing(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "files" / "docs").mkdir(parents=True)
    (tmp_path / "files" / "docs" / "a.md").write_text("# Doc A\n\n## §2.1 Rule\n\nBe kind.\n\n## §2.2 Other\n\nBe fair.\n")
    (tmp_path / "data" / "reference_documents.csv").write_text(
        "doc_id,doc_type,title,language,version,effective_date,superseded_by,file_path\n"
        "A,policy,Doc A,EN,2,2026-01-01,,docs/a.md\n"
        "A-OLD,policy,Doc A,EN,1,2025-01-01,A,docs/old.md\n"
        "B,faq,Doc B,EN,1,2026-01-01,,docs/missing.md\n")
    rows, stats = policies.load_sections(tmp_path)
    assert [r["cite"] for r in rows] == ["A §2.1", "A §2.2"]
    assert stats == {"documents": 1, "superseded_skipped": 1, "missing_files": 1, "sections": 2}
    assert policies.load_sections(tmp_path / "nowhere")[0] == []


@pytest.mark.parametrize("query,first", [("incident dialer outage", "IR-0923"), ("hardship option before payment", "POL-TEST-001 §1.2")])
def test_index_ranking(serving, query, first):
    cols, rows = serving.execute("SELECT * FROM l2.policy_sections", [])
    hits = policies.Index([dict(zip(cols, r)) for r in rows]).search(query)
    assert hits and hits[0]["cite"] == first
