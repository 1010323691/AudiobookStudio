"""Offline tests for the character voice-prep engine's pure logic
(``backend/engines/voices.py``).

These pin the faithful port of the source ``generate_personas.py`` helpers — JSON
extraction from LLM output, name normalisation, token-Jaccard, canonical-name
resolution (the basis of alias folding), narrator-context gathering, ref-text
selection, and the filename sanitizer — against known inputs, with no network or
engine access. Mirrors the style and focus of ``test_script.py``.
"""
from __future__ import annotations

from backend.engines.voices import (
    _collect_narrator_context,
    _fallback_persona,
    _resolve_to_canonical,
    _sanitize,
    _token_jaccard,
    extract_json_object,
    normalize_speaker_name,
    pick_ref_text,
)

BS = chr(92)  # backslash — built via chr() so no literal backslashes live in this file


# --------------------------------------------------------------------------- #
# extract_json_object
# --------------------------------------------------------------------------- #

def test_extract_simple_object():
    assert extract_json_object('{"description":"d","ref_text":"r"}') == {
        "description": "d", "ref_text": "r",
    }


def test_extract_embedded_in_prose():
    got = extract_json_object('Here you go: {"description":"x"} and more')
    assert got == {"description": "x"}


def test_extract_nested_object():
    assert extract_json_object('{"a": {"b": 1}}') == {"a": {"b": 1}}


def test_extract_ignores_brace_in_string():
    assert extract_json_object('{"s":"a}b"}') == {"s": "a}b"}


def test_extract_ignores_escaped_quote():
    # raw JSON text: {"a":"a\"b"}  — the \" must not close the string
    raw = '{"a":"a' + BS + '"b"}'
    assert extract_json_object(raw) == {"a": 'a"b'}


def test_extract_none_without_braces():
    assert extract_json_object("no braces here") is None


def test_extract_none_when_unbalanced():
    assert extract_json_object('{"a":1') is None


# --------------------------------------------------------------------------- #
# normalize_speaker_name
# --------------------------------------------------------------------------- #

def test_normalize_strips_honorifics():
    assert normalize_speaker_name("Dr. Smith") == "smith"
    assert normalize_speaker_name("  Mr. Jones  ") == "jones"
    assert normalize_speaker_name("Prof. Ada") == "ada"


def test_normalize_lowercases_and_strips_punct():
    assert normalize_speaker_name("Alice") == "alice"
    assert normalize_speaker_name("O'Brien") == "obrien"


def test_normalize_collapses_whitespace():
    assert normalize_speaker_name("John   Smith") == "john smith"


def test_normalize_non_string_is_empty():
    assert normalize_speaker_name(123) == ""
    assert normalize_speaker_name(None) == ""


def test_normalize_cjk_stripped():
    # CJK is not in the [a-z0-9\s] keep-set (1:1 port of the source) — the filename
    # sanitizer (_sanitize) is what preserves CJK, not the name normaliser.
    assert normalize_speaker_name("张三") == ""


# --------------------------------------------------------------------------- #
# _token_jaccard
# --------------------------------------------------------------------------- #

def test_jaccard_identical():
    assert _token_jaccard("John Smith", "john smith") == 1.0


def test_jaccard_disjoint():
    assert _token_jaccard("John", "Smith") == 0.0


def test_jaccard_partial():
    assert _token_jaccard("John Smith", "John Doe") == 1 / 3


def test_jaccard_empty_side():
    assert _token_jaccard("", "John") == 0.0


# --------------------------------------------------------------------------- #
# _resolve_to_canonical
# --------------------------------------------------------------------------- #

def test_canonical_exact_match():
    # "Smith" -> "smith" matches "Dr. Smith" -> "smith" after normalization.
    assert _resolve_to_canonical("Smith", ["John", "Dr. Smith"]) == "Dr. Smith"


def test_canonical_exact_after_normalize():
    assert _resolve_to_canonical("Dr. Smith", ["John", "Smith"]) == "Smith"


def test_canonical_substring():
    assert _resolve_to_canonical("Smithson", ["Smith"]) == "Smith"


def test_canonical_jaccard_pass():
    # Neither exact nor substring; the token-Jaccard (1/3) clears the low threshold.
    assert _resolve_to_canonical("John Peter", ["John James"], threshold=0.2) == "John James"


def test_canonical_jaccard_below_threshold():
    assert _resolve_to_canonical("John Peter", ["John James"], threshold=0.5) is None


def test_canonical_empty_raw():
    assert _resolve_to_canonical("", ["John"]) is None


def test_canonical_cjk_unresolvable():
    # CJK normalises to "" on both sides, so it can never be resolved this way.
    assert _resolve_to_canonical("张三", ["李四"]) is None


# --------------------------------------------------------------------------- #
# _collect_narrator_context
# --------------------------------------------------------------------------- #

def _script():
    return [
        {"speaker": "NARRATOR", "text": "ctx1"},
        {"speaker": "Alice", "text": "a1"},
        {"speaker": "NARRATOR", "text": "ctx2"},
        {"speaker": "Alice", "text": "a2"},
    ]


def test_narrator_context_gathers_nearby():
    assert _collect_narrator_context(_script(), "Alice", window=4) == ["ctx1", "ctx2"]


def test_narrator_context_respects_window():
    assert _collect_narrator_context(_script(), "Alice", window=1) == ["ctx1"]


def test_narrator_context_dedups():
    s = [
        {"speaker": "NARRATOR", "text": "same"},
        {"speaker": "Bob", "text": "b"},
        {"speaker": "NARRATOR", "text": "same"},
    ]
    assert _collect_narrator_context(s, "Bob", window=4) == ["same"]


def test_narrator_context_absent_speaker():
    assert _collect_narrator_context(_script(), "Nobody", window=4) == []


# --------------------------------------------------------------------------- #
# pick_ref_text
# --------------------------------------------------------------------------- #

def test_ref_text_prefers_long_enough_line():
    assert pick_ref_text(["short", "this is a long enough line"]) == "this is a long enough line"


def test_ref_text_falls_back_to_first_nonempty():
    assert pick_ref_text(["a", "b c"]) == "a"


def test_ref_text_skips_blank():
    assert pick_ref_text(["", "  ", "hello there friend"]) == "hello there friend"


def test_ref_text_empty():
    assert pick_ref_text([]) == ""


# --------------------------------------------------------------------------- #
# _fallback_persona
# --------------------------------------------------------------------------- #

def test_fallback_persona_shape():
    desc, ref = _fallback_persona("Bob", ["a long enough line here"])
    assert desc == "Bob has a clear, natural audiobook voice."
    assert ref == "a long enough line here"


# --------------------------------------------------------------------------- #
# _sanitize (filename-safe)
# --------------------------------------------------------------------------- #

def test_sanitize_replaces_separators():
    assert _sanitize("John Smith") == "john_smith"


def test_sanitize_replaces_punct():
    assert _sanitize("A.B/C") == "a_b_c"


def test_sanitize_keeps_cjk():
    # \w matches CJK, so CJK character names survive into the filename.
    assert _sanitize("张三") == "张三"


def test_sanitize_empty_or_none():
    assert _sanitize("") == "unknown"
    assert _sanitize(None) == "unknown"
