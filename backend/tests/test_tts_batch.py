"""Offline tests for the batch TTS segment ordering (``backend/engines/tts_batch.py``)
and the voice-config usability check (``backend/api/tts.py``).

The segment list is what the worker synthesizes and what Merge later reorders, so its
index assignment, ordering, empty-text skipping and index filtering are load-bearing
invariants worth pinning. ``_voice_usable`` decides the ready/pending badge shown per
character on the 角色配音 page.
"""
from __future__ import annotations

from backend.api.tts import _voice_usable
from backend.engines.tts_batch import _build_segments


def _entry(speaker: str, text: str, instruct: str = "", pause_after=None, **extra):
    e = {"speaker": speaker, "text": text, "instruct": instruct}
    if pause_after is not None:
        e["pause_after"] = pause_after
    e.update(extra)
    return e


# --------------------------------------------------------------------------- #
# _build_segments — ordering & index assignment
# --------------------------------------------------------------------------- #

def test_build_segments_in_json_order():
    script = [_entry("A", "a"), _entry("B", "b"), _entry("A", "c")]
    segs = _build_segments(script)
    assert [s["index"] for s in segs] == [0, 1, 2]
    assert [s["speaker"] for s in segs] == ["A", "B", "A"]
    assert [s["text"] for s in segs] == ["a", "b", "c"]


def test_build_segments_index_is_full_position():
    # A filtered run must keep each segment's index = its position in the full script.
    script = [_entry("A", "a"), _entry("B", "b"), _entry("C", "c")]
    segs = _build_segments(script, indices=[2, 0])
    assert [s["index"] for s in segs] == [0, 2]  # re-sorted back to JSON order
    assert [s["speaker"] for s in segs] == ["A", "C"]


def test_build_segments_skips_empty_text():
    script = [_entry("A", "  "), _entry("B", "b"), _entry("C", "")]
    segs = _build_segments(script)
    assert [s["index"] for s in segs] == [1]
    assert segs[0]["speaker"] == "B"


def test_build_segments_ignores_unknown_indices():
    script = [_entry("A", "a"), _entry("B", "b")]
    assert _build_segments(script, indices=[5, 99]) == []


def test_build_segments_falls_back_to_type_for_speaker():
    # A script entry with only ``type`` (no ``speaker``) still yields a speaker label.
    script = [{"type": "NARRATOR", "text": "n", "instruct": ""}]
    segs = _build_segments(script)
    assert segs[0]["speaker"] == "NARRATOR"


def test_build_segments_copies_pause_after_and_instruct():
    script = [_entry("A", "a", instruct="warm", pause_after=900)]
    segs = _build_segments(script)
    assert segs[0]["instruct"] == "warm"
    assert segs[0]["pause_after"] == 900


def test_build_segments_empty_script():
    assert _build_segments([]) == []


# --------------------------------------------------------------------------- #
# _voice_usable
# --------------------------------------------------------------------------- #

def test_voice_usable_clone_needs_ref_audio():
    assert _voice_usable({"type": "clone", "ref_audio": "/x/preview.wav"}) is True
    assert _voice_usable({"type": "clone"}) is False


def test_voice_usable_design_needs_description():
    assert _voice_usable({"type": "design", "description": "a warm voice"}) is True
    assert _voice_usable({"type": "design", "description": "   "}) is False


def test_voice_usable_custom_always():
    assert _voice_usable({"type": "custom"}) is True


def test_voice_usable_unknown_type():
    assert _voice_usable({"type": "lora"}) is False
    assert _voice_usable({}) is False
