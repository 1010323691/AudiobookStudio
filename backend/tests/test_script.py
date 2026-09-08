"""Offline tests for the script-generation engine's pure logic
(``backend/engines/script.py``).

These pin the faithful port of the source ``generate_script.py`` JSON pipeline —
chunking, response cleaning, array repair, and regex salvage — against known inputs,
with no network access (the LLM transport itself is exercised in the manual end-to-end
run). Mirrors the style and focus of ``test_text.py``.
"""
from __future__ import annotations

import json
import re

from backend.engines.script import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    clean_json_string,
    fix_mojibake,
    repair_json_array,
    salvage_json_entries,
    split_into_chunks,
)

BS = chr(92)  # backslash — built via chr() so no literal backslashes live in this file


def _entry(speaker: str, text: str, instruct: str) -> dict:
    return {"speaker": speaker, "text": text, "instruct": instruct}


# --------------------------------------------------------------------------- #
# split_into_chunks
# --------------------------------------------------------------------------- #

def test_split_by_paragraphs():
    assert split_into_chunks("para one\n\npara two\n\npara three", max_size=10) == [
        "para one", "para two", "para three",
    ]


def test_split_packs_small_paragraphs():
    assert split_into_chunks("aa\n\nbb\n\ncc", max_size=100) == ["aa\n\nbb\n\ncc"]


def test_split_long_paragraph_by_sentences():
    assert split_into_chunks("aaaa. bbbb. cccc.", max_size=8) == ["aaaa.", "bbbb.", "cccc."]


def test_split_empty():
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n  ", max_size=10) == []


def test_split_preserves_content():
    samples = [
        "para one\n\npara two\n\npara three",
        "aaaa. bbbb. cccc.",
        "第一段。\n\n第二段。句子一。句子二。\n\n第三段。",
    ]
    for src in samples:
        chunks = split_into_chunks(src, max_size=20)
        assert re.sub(r"\s", "", src) == re.sub(r"\s", "", "\n\n".join(chunks)), repr(src)


# --------------------------------------------------------------------------- #
# clean_json_string
# --------------------------------------------------------------------------- #

def test_clean_strips_code_fence():
    assert clean_json_string('```json\n[{"a":1}]\n```') == '[{"a":1}]'


def test_clean_strips_thinking_tags():
    assert clean_json_string('<thinking>blah</thinking>[{"a":1}] tail') == '[{"a":1}]'


def test_clean_bracket_counter_with_noise():
    assert clean_json_string('x [{"a":1},{"b":2}] y') == '[{"a":1},{"b":2}]'


def test_clean_salvages_unclosed_array():
    assert clean_json_string('noise [{"a":1},{"b":2}') == '[{"a":1}]'


def test_clean_returns_none_without_array():
    assert clean_json_string("no brackets here") is None


def test_clean_escapes_control_chars_in_strings():
    raw = '[{"speaker":"N","text":"a\nb","instruct":"i"}]'  # \n is a real newline
    cleaned = clean_json_string(raw)
    assert json.loads(cleaned)[0]["text"] == "a\nb"


# --------------------------------------------------------------------------- #
# repair_json_array
# --------------------------------------------------------------------------- #

def test_repair_valid_array():
    assert repair_json_array('[{"speaker":"N","text":"a","instruct":"b"}]') == [_entry("N", "a", "b")]


def test_repair_missing_comma():
    got = repair_json_array(
        '[{"speaker":"A","text":"1","instruct":"i"}'
        '{"speaker":"B","text":"2","instruct":"j"}]'
    )
    assert [e["speaker"] for e in got] == ["A", "B"]


def test_repair_trailing_comma():
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"},]') == [_entry("A", "1", "i")]


def test_repair_drops_non_dict_and_logs():
    logs = []
    got = repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]', log=logs.append)
    assert got == [_entry("A", "1", "i")]
    assert logs == ["Dropped 1 non-object entries from LLM JSON array"]


def test_repair_drops_non_dict_without_log():
    # No ``log`` callback: the drop path must not crash (the ``and log`` guard).
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]') == [_entry("A", "1", "i")]


def test_repair_unparseable():
    assert repair_json_array("not json at all") is None
    assert repair_json_array("") is None


# --------------------------------------------------------------------------- #
# salvage_json_entries
# --------------------------------------------------------------------------- #

def test_salvage_single_entry():
    got = salvage_json_entries('noise [{"speaker":"NARRATOR","text":"他说","instruct":"calm"}, garbage')
    assert got == [_entry("NARRATOR", "他说", "calm")]


def test_salvage_unescapes_text():
    raw = '{"speaker":"N","text":"a' + BS + 'nb","instruct":"i"}'  # text value is a\nb (escaped)
    got = salvage_json_entries(raw)
    assert got[0]["text"] == "a\nb"


def test_salvage_none_when_no_match():
    assert salvage_json_entries("no objects here") is None


# --------------------------------------------------------------------------- #
# fix_mojibake
# --------------------------------------------------------------------------- #

def test_fix_mojibake_noop_on_clean_text():
    assert fix_mojibake("plain text 123 中文") == "plain text 123 中文"


def test_fix_mojibake_replaces_ellipsis():
    # The CP1252-as-UTF8 mojibake of "…" (E2 80 A6) is mapped back to the real ellipsis.
    assert fix_mojibake("aâ€¦b") == "a…b"


# --------------------------------------------------------------------------- #
# Bundled default prompts
# --------------------------------------------------------------------------- #

def test_default_prompts_loaded():
    assert DEFAULT_SYSTEM_PROMPT.strip()
    assert "{context}" in DEFAULT_USER_PROMPT
    assert "{chunk}" in DEFAULT_USER_PROMPT
