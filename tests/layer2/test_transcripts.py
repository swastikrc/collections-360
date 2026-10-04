"""The transcript parser is tolerant: the real file format is unknown until the DB machine."""
import json

import pytest

from c360.layer2 import transcripts

CASES = [
    ({"turns": [{"speaker": "agent", "text": "Hello"}, {"speaker": "customer", "text": "I lost my job"}]},
     "agent: Hello\ncustomer: I lost my job"),
    ({"segments": [{"role": "AGENT", "utterance": "Hello"}, {"role": "CUSTOMER", "utterance": "Hi"}]}, "AGENT: Hello\nCUSTOMER: Hi"),
    ([{"speaker": "A", "content": "one"}, {"speaker": "B", "content": "two"}], "A: one\nB: two"),
    ({"meta": {"id": 7}, "call": {"messages": [{"who": "agent", "transcript": "Line one"}, {"transcript": "Line two"}]}},
     "agent: Line one\nLine two"),
    ({"transcript_id": "5010384", "transcript": "Agent: hello. Customer: hi."}, "Agent: hello. Customer: hi."),
    ({"text": "  just text  "}, "just text"),
    ({"a": {"b": ["first", {"c": "second"}]}, "n": 3}, "first second"),
    ({"turns": [{"speaker": "agent", "text": ""}, {"speaker": "customer", "text": "only me"}]}, "customer: only me"),
]


@pytest.mark.parametrize("payload,expected", CASES)
def test_shapes(tmp_path, payload, expected):
    path = tmp_path / "t.json"
    path.write_text(json.dumps(payload))
    assert transcripts.parse(path) == expected


def test_missing_unreadable_and_empty_files_give_none(tmp_path):
    assert transcripts.parse(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert transcripts.parse(bad) is None
    for payload in ({}, [], {"turns": []}, {"n": 1}):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps(payload))
        assert transcripts.parse(empty) is None


def test_top_level_keys(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"turns": [], "language": "en-CA"}))
    assert transcripts.top_level_keys(path) == ["language", "turns"]
    path.write_text(json.dumps([1, 2]))
    assert transcripts.top_level_keys(path) == ["<list>"]
    assert transcripts.top_level_keys(tmp_path / "nope.json") is None


# ---- DB machine: real release layout and structure ------------------------------------------
# Keys copied from a real transcript file; every value is invented (no customer text in the repo).
REAL_SHAPE_SAMPLE = {
    "transcript_id": "T-0001", "contact_id": "CT-0001", "call_recording_id": "R-0001", "voice_sample_id": None,
    "case_id": "CS-2026-000001", "crm_customer_id": "AAA-0000", "agent_id": "AG-0001",
    "call_start_ts": "2026-09-01 10:00:00", "call_end_ts": "2026-09-01 10:02:00", "duration_sec": 120,
    "direction": "outbound", "language": "en-CA", "code_switch_flag": False, "asr_engine": "engine",
    "asr_model_version": "v1", "asr_confidence_avg": 0.9, "wer_estimate": 0.1, "diarization_confidence": 0.9,
    "speaker_count": 2, "customer_talk_ratio": 0.5, "agent_talk_ratio": 0.5, "silence_ratio": 0.0,
    "overtalk_count": 0, "hold_count": 0, "pci_redaction_applied": True, "pii_redaction_applied": True,
    "src_system": "X", "batch_id": "B1", "file_path": "transcripts/2026/09/T-0001.json",
    "turns": [
        {"turn_index": 0, "speaker": "agent", "start_sec": 0.0, "end_sec": 3.0, "text": "Hello, this is the bank.",
         "asr_confidence": 0.95, "sentiment_score": 0.1, "redacted_spans": []},
        {"turn_index": 1, "speaker": "customer", "start_sec": 3.0, "end_sec": 6.0, "text": "I can pay on Friday.",
         "asr_confidence": 0.9, "sentiment_score": 0.0, "redacted_spans": []},
    ],
}


def test_real_shape_gives_turn_text_only():
    text = transcripts.text_of(REAL_SHAPE_SAMPLE)
    assert text.splitlines() == ["agent: Hello, this is the bank.", "customer: I can pay on Friday."]
    assert "engine" not in text and "CS-2026-000001" not in text    # metadata is not mixed into the text


def test_resolve_tries_base_dirs_in_order(tmp_path):
    (tmp_path / "files" / "transcripts").mkdir(parents=True)
    (tmp_path / "files" / "transcripts" / "a.json").write_text(json.dumps(REAL_SHAPE_SAMPLE))
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "b.json").write_text(json.dumps(REAL_SHAPE_SAMPLE))
    dirs = ["files", ""]
    assert transcripts.resolve(tmp_path, "transcripts/a.json", dirs) == tmp_path / "files" / "transcripts" / "a.json"
    assert transcripts.resolve(tmp_path, "transcripts/b.json", dirs) == tmp_path / "transcripts" / "b.json"
    missing = transcripts.resolve(tmp_path, "transcripts/none.json", dirs)
    assert not missing.exists() and transcripts.parse(missing) is None
    assert transcripts.resolve(tmp_path, "transcripts/b.json") == tmp_path / "transcripts" / "b.json"  # default: root
