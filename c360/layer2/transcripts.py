"""Tolerant call-transcript JSON -> plain text.

The real file format is unknown on the development machine, so this parser accepts the shapes a
transcript export usually takes:
  - a list of turns/segments, each a dict with one of the keys text / utterance / content / transcript
    (and optionally a speaker) -> "<speaker>: <text>" per line
  - a dict with a single text field
  - anything else -> every string leaf, concatenated
Missing or unreadable files give None (counted and reported by the publisher).
"""
import json
from pathlib import Path

TEXT_KEYS = ("text", "utterance", "content", "transcript")
SPEAKER_KEYS = ("speaker", "role", "speaker_label", "participant", "channel", "who")
LIST_KEYS = ("turns", "segments", "utterances", "messages", "dialogue", "conversation", "transcript", "results", "entries")


def _turn_text(turn):
    if not isinstance(turn, dict):
        return None
    for k in TEXT_KEYS:
        if isinstance(turn.get(k), str) and turn[k].strip():
            return turn[k].strip()
    return None


def _is_turn_list(v):
    return isinstance(v, list) and len(v) > 0 and any(_turn_text(t) for t in v)


def _find_turns(data, depth=0):
    if _is_turn_list(data):
        return data
    if isinstance(data, dict) and depth < 3:
        for k in LIST_KEYS:
            if _is_turn_list(data.get(k)):
                return data[k]
        for v in data.values():
            found = _find_turns(v, depth + 1) if isinstance(v, (dict, list)) else None
            if found:
                return found
    return None


def _string_leaves(data, out):
    if isinstance(data, str):
        if data.strip():
            out.append(data.strip())
    elif isinstance(data, dict):
        for v in data.values():
            _string_leaves(v, out)
    elif isinstance(data, list):
        for v in data:
            _string_leaves(v, out)
    return out


def text_of(data) -> str | None:
    """Plain text of an already-loaded transcript object."""
    turns = _find_turns(data)
    if turns:
        lines = []
        for t in turns:
            text = _turn_text(t)
            if not text:
                continue
            speaker = next((str(t[k]).strip() for k in SPEAKER_KEYS if t.get(k) not in (None, "")), None)
            lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines) or None
    if isinstance(data, dict):
        for k in TEXT_KEYS:
            if isinstance(data.get(k), str) and data[k].strip():
                return data[k].strip()
    return " ".join(_string_leaves(data, [])) or None


def parse(path) -> str | None:
    """Read one transcript JSON file. None when the file is missing, unreadable or has no text."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return text_of(data)


def top_level_keys(path) -> list[str] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return sorted(data.keys()) if isinstance(data, dict) else [f"<{type(data).__name__}>"]


def resolve(data_root, rel_path, base_dirs=("",)):
    """Locate a file whose stored path is relative to one of several folders under the dataset root.

    `base_dirs` comes from the view's `file_text.base_dirs` in l2_views.yaml (tried in order); the
    first folder where the file exists wins. Returns the last candidate when none exists, so the
    caller still reports the file as missing.
    """
    root = Path(data_root)
    candidates = [root / b / rel_path if b else root / rel_path for b in (base_dirs or ("",))]
    return next((c for c in candidates if c.exists()), candidates[-1])

