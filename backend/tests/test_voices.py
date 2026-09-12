"""Offline tests for the character voice-prep engine's pure logic
(``backend/engines/voices.py``).

These pin the pure helpers of the voice-prep engine — JSON extraction from LLM
output, name normalisation, token-Jaccard, canonical-name resolution (the basis of
alias folding), target-line sampling and per-line context windows (the persona
evidence basis), ref-text selection, and the filename sanitizer — against known
inputs, with no network or engine access. Mirrors the style and focus of
``test_script.py``.
"""
from __future__ import annotations

from backend.api.tts import _clone_status, _foundation_status
from backend.engines.voices import (
    _ChildReg,
    _clone_done,
    _fallback_persona,
    _has_foundation,
    _resolve_to_canonical,
    _sanitize,
    _select_target_bands,
    _token_jaccard,
    _window_block,
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
# _select_target_bands  (optimization A: front / middle / back sampling)
# --------------------------------------------------------------------------- #

def _pairs(n, start=0):
    return [(start + k, f"line {k}") for k in range(n)]


def test_bands_empty():
    assert _select_target_bands([]) == ([], [], [])


def test_bands_few_lines_all_to_front():
    # Fewer than 3 * per lines: everything folds into 'front' (none dropped).
    assert _select_target_bands(_pairs(5)) == ([0, 1, 2, 3, 4], [], [])


def test_bands_front_back_middle_spread():
    front, middle, back = _select_target_bands(_pairs(40))
    assert front == list(range(0, 8))
    assert back == list(range(32, 40))
    # 8 lines spread evenly through the middle region (indices 8..31).
    assert middle == [8, 11, 15, 18, 21, 24, 28, 31]
    assert all(8 <= x <= 31 for x in middle)


def test_bands_are_disjoint():
    front, middle, back = _select_target_bands(_pairs(60))
    seen = set(front) | set(middle) | set(back)
    assert len(seen) == len(front) + len(middle) + len(back)  # no overlap


# --------------------------------------------------------------------------- #
# _window_block  (optimization A: per-line ±context, any speaker)
# --------------------------------------------------------------------------- #

def test_window_block_marks_target_and_keeps_order():
    script = [
        {"speaker": "NARRATOR", "text": "c0"},
        {"speaker": "Bob", "text": "c1"},
        {"speaker": "Alice", "text": "target"},
        {"speaker": "NARRATOR", "text": "c2"},
    ]
    assert _window_block(script, 2, window=4) == (
        "   NARRATOR: c0\n   Bob: c1\n★ Alice: target\n   NARRATOR: c2"
    )


def test_window_block_clamps_at_start():
    script = [
        {"speaker": "Alice", "text": "t0"},
        {"speaker": "NARRATOR", "text": "c1"},
        {"speaker": "Bob", "text": "c2"},
    ]
    assert _window_block(script, 0, window=4) == (
        "★ Alice: t0\n   NARRATOR: c1\n   Bob: c2"
    )


def test_window_block_respects_window():
    script = [
        {"speaker": "NARRATOR", "text": "c0"},
        {"speaker": "NARRATOR", "text": "c1"},
        {"speaker": "Alice", "text": "target"},
        {"speaker": "NARRATOR", "text": "c2"},
        {"speaker": "NARRATOR", "text": "c3"},
    ]
    # window=1 keeps only the immediate neighbours of the target.
    assert _window_block(script, 2, window=1) == (
        "   NARRATOR: c1\n★ Alice: target\n   NARRATOR: c2"
    )


def test_window_block_skips_empty_text():
    script = [
        {"speaker": "NARRATOR", "text": "   "},  # whitespace -> empty -> dropped
        {"speaker": "Alice", "text": "hi"},
    ]
    assert _window_block(script, 1, window=4) == "★ Alice: hi"


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


# --------------------------------------------------------------------------- #
# Phase-selection predicates  (_has_foundation / _clone_done)
# --------------------------------------------------------------------------- #

def test_has_foundation_requires_description():
    assert _has_foundation({"type": "foundation", "description": "a voice"}) is True
    # A foundation entry whose generation failed (empty description) is NOT usable.
    assert _has_foundation({"type": "foundation", "description": "   "}) is False
    # A pre-split entry that carries a description counts as a foundation.
    assert _has_foundation({"type": "clone", "description": "a voice", "ref_audio": "/x.wav"}) is True
    assert _has_foundation({"type": "foundation"}) is False
    assert _has_foundation({}) is False
    assert _has_foundation(None) is False


def test_clone_done_requires_clone_with_ref_audio():
    assert _clone_done({"type": "clone", "ref_audio": "/x.wav"}) is True
    # A clone entry with no (or missing) reference audio is not done.
    assert _clone_done({"type": "clone"}) is False
    assert _clone_done({"type": "clone", "ref_audio": ""}) is False
    # A design fallback / foundation is not a clone.
    assert _clone_done({"type": "design", "description": "x"}) is False
    assert _clone_done({"type": "foundation", "description": "x"}) is False
    assert _clone_done({}) is False
    assert _clone_done(None) is False


# --------------------------------------------------------------------------- #
# Phase-status inference  (_foundation_status / _clone_status)
# --------------------------------------------------------------------------- #

def test_foundation_status_explicit_field_wins():
    # An explicit Phase-1 field is authoritative, even over a stored description.
    assert _foundation_status({"foundation_status": "failed", "description": "x"}) == "failed"
    assert _foundation_status({"foundation_status": "done", "description": ""}) == "done"
    # Unknown values fall through to inference (empty description -> none).
    assert _foundation_status({"foundation_status": "weird", "description": "x"}) == "done"
    assert _foundation_status({"foundation_status": "weird"}) == "none"


def test_foundation_status_inferred_from_description():
    assert _foundation_status({"description": "a voice"}) == "done"
    assert _foundation_status({"type": "clone", "description": "a voice", "ref_audio": "/x"}) == "done"
    assert _foundation_status({"type": "foundation", "description": "   "}) == "none"
    assert _foundation_status({}) == "none"
    assert _foundation_status(None) is not None  # does not raise; empty dict is the caller's concern


def test_clone_status_explicit_field_wins():
    assert _clone_status({"clone_status": "failed", "type": "clone", "ref_audio": "/x"}) == "failed"
    assert _clone_status({"clone_status": "done"}) == "done"


def test_clone_status_inferred_from_clone_entry():
    assert _clone_status({"type": "clone", "ref_audio": "/x.wav"}) == "done"
    assert _clone_status({"type": "clone"}) == "none"
    assert _clone_status({"type": "design", "description": "x"}) == "none"
    assert _clone_status({}) == "none"


# --------------------------------------------------------------------------- #
# _ChildReg  (cancel tears down in-flight TTS children)
# --------------------------------------------------------------------------- #

class _FakeProc:
    def __init__(self, fail=False):
        self.fail = fail
        self.killed = 0

    def kill(self):
        if self.fail:
            raise RuntimeError("boom")
        self.killed += 1


def test_child_reg_kill_all_kills_registered():
    reg = _ChildReg()
    a, b = _FakeProc(), _FakeProc()
    reg.add(a)
    reg.add(b)
    reg.kill_all()
    assert a.killed == 1 and b.killed == 1


def test_child_reg_remove_then_kill_leaves_it():
    reg = _ChildReg()
    a = _FakeProc()
    reg.add(a)
    reg.remove(a)
    reg.kill_all()
    assert a.killed == 0


def test_child_reg_ignores_kill_errors():
    # A child that already exited (kill() raises) must not break teardown of the others.
    reg = _ChildReg()
    bad, good = _FakeProc(fail=True), _FakeProc()
    reg.add(bad)
    reg.add(good)
    reg.kill_all()  # must not raise
    assert good.killed == 1


def test_child_reg_remove_is_idempotent():
    reg = _ChildReg()
    a = _FakeProc()
    reg.add(a)
    reg.remove(a)
    reg.remove(a)  # discarding an absent proc is a no-op, not an error
    reg.kill_all()
    assert a.killed == 0
