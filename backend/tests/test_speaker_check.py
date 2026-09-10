"""Offline tests for the Speaker-check engine (``backend/engines/speaker_check.py``).

Pins the batch context-window builder, the per-target speaker parser, the majority vote,
and the core invariants — "only ``speaker`` changes", "the original file is untouched",
batched disagreement re-sampling (3×, then a 4× tie-break), and per-item failure
isolation — with no network access: the LLM transport is a mocked
``urllib.request.urlopen`` (the same pattern ``test_script.py`` uses).
"""
from __future__ import annotations

import json

import pytest
import urllib.request

from backend.core.config import GenerationConfig, LLMConfig, SpeakerCheckConfig
from backend.core.tasks import TaskCancelled
from backend.engines.speaker_check import (
    BATCH_SIZE,
    _pick_majority,
    build_batch_window,
    check_file,
    parse_speaker,
    parse_speaker_map,
    target_indices,
)


def _entry(speaker: str, text: str, instruct: str = "tone") -> dict:
    return {"speaker": speaker, "text": text, "instruct": instruct}


# --------------------------------------------------------------------------- #
# build_batch_window / target_indices
# --------------------------------------------------------------------------- #

def test_batch_window_flags_the_targets():
    entries = [_entry("N", f"t{i}") for i in range(30)]
    # A middle batch of 3 targets starting at index 10, with ±1 context.
    w = build_batch_window(entries, start=10, size=3, n=1)
    assert [e["index"] for e in w] == [9, 10, 11, 12, 13]
    assert [e.get("target") for e in w] == [None, True, True, True, None]
    assert all("instruct" not in e for e in w)  # instruct is dropped to save tokens


def test_batch_window_clamps_at_start():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    w = build_batch_window(entries, start=0, size=3, n=2)  # no context before index 0
    assert [e["index"] for e in w] == [0, 1, 2, 3, 4]
    assert [e.get("target") for e in w] == [True, True, True, None, None]


def test_batch_window_clamps_at_end():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    w = build_batch_window(entries, start=3, size=5, n=2)  # the block runs past the end
    assert [e["index"] for e in w] == [1, 2, 3, 4]
    assert [e.get("target") for e in w] == [None, None, True, True]


def test_batch_window_zero_n_only_targets():
    entries = [_entry("N", f"t{i}") for i in range(6)]
    w = build_batch_window(entries, start=2, size=3, n=0)
    assert [e["index"] for e in w] == [2, 3, 4]
    assert all(e.get("target") is True for e in w)


def test_batch_window_carries_original_speaker_and_text():
    entries = [_entry("ALICE", "你好"), _entry("NARRATOR", "他走了"), _entry("BOB", "再见")]
    w = build_batch_window(entries, start=0, size=2, n=1)
    assert w[0] == {"index": 0, "speaker": "ALICE", "text": "你好", "target": True}
    assert w[1] == {"index": 1, "speaker": "NARRATOR", "text": "他走了", "target": True}
    assert w[2] == {"index": 2, "speaker": "BOB", "text": "再见"}  # context: no target key


def test_target_indices_clamps():
    assert target_indices(0, 20, 3) == [0, 1, 2]                    # last partial batch
    assert target_indices(10, 20, 30) == list(range(10, 30))        # a full batch
    assert target_indices(25, 20, 30) == [25, 26, 27, 28, 29]       # clamped at the end


# --------------------------------------------------------------------------- #
# parse_speaker (single-entry reply)
# --------------------------------------------------------------------------- #

def test_parse_speaker_clean_object():
    assert parse_speaker('{"speaker": "ELENA"}') == "ELENA"


def test_parse_speaker_object_with_extra_keys():
    assert parse_speaker('{"speaker": "NARRATOR", "reason": "it is narration"}') == "NARRATOR"


def test_parse_speaker_strips_closed_thinking_tags():
    lt, gt = chr(60), chr(62)  # build the tags so no literal <> / newline lives in the file
    raw = lt + "think" + gt + "hmm, it is dialogue" + lt + "/think" + gt + ' {"speaker": "BOB"}'
    assert parse_speaker(raw) == "BOB"


def test_parse_speaker_markdown_fence():
    raw = "```json\n" + '{"speaker": "CARL"}' + "\n```"
    assert parse_speaker(raw) == "CARL"


def test_parse_speaker_single_element_array():
    assert parse_speaker('["ELENA"]') == "ELENA"


def test_parse_speaker_bare_token():
    assert parse_speaker("ELENA") == "ELENA"


def test_parse_speaker_sentence_is_none():
    assert parse_speaker("I think the speaker is probably ELENA because...") is None


def test_parse_speaker_empty_is_none():
    assert parse_speaker("") is None
    assert parse_speaker(None) is None


def test_parse_speaker_object_without_speaker_is_none():
    assert parse_speaker('{"foo": "bar"}') is None


# --------------------------------------------------------------------------- #
# parse_speaker_map (batch reply)
# --------------------------------------------------------------------------- #

def test_parse_speaker_map_results_object():
    text = '{"results": [{"index": 0, "speaker": "A"}, {"index": 1, "speaker": "B"}]}'
    assert parse_speaker_map(text, [0, 1, 2]) == {0: "A", 1: "B"}


def test_parse_speaker_map_ignores_non_targets():
    # A speaker for a non-target index must be dropped (only target indices are kept).
    text = '{"results": [{"index": 0, "speaker": "A"}, {"index": 5, "speaker": "X"}]}'
    assert parse_speaker_map(text, [0, 1]) == {0: "A"}


def test_parse_speaker_map_bare_array_of_objects():
    text = '[{"index": 0, "speaker": "A"}, {"index": 1, "speaker": "B"}]'
    assert parse_speaker_map(text, [0, 1, 2]) == {0: "A", 1: "B"}


def test_parse_speaker_map_bare_array_of_strings_in_order():
    assert parse_speaker_map('["A", "B", "C"]', [0, 1, 2]) == {0: "A", 1: "B", 2: "C"}


def test_parse_speaker_map_index_keyed_object():
    assert parse_speaker_map('{"0": "A", "1": "B"}', [0, 1, 2]) == {0: "A", 1: "B"}


def test_parse_speaker_map_batch_shape_on_single_target():
    # A one-target batch still understands the {"results": [...]} shape (not just scalar).
    assert parse_speaker_map('{"results": [{"index": 0, "speaker": "A"}]}', [0]) == {0: "A"}


def test_parse_speaker_map_single_target_scalar_fallback():
    # ...and falls back to the scalar parser for a {"speaker": ...} reply.
    assert parse_speaker_map('{"speaker": "ELENA"}', [4]) == {4: "ELENA"}


def test_parse_speaker_map_strips_thinking_and_fence():
    lt, gt = chr(60), chr(62)
    inner = '{"results": [{"index": 0, "speaker": "A"}, {"index": 1, "speaker": "B"}]}'
    raw = lt + "think" + gt + "..." + lt + "/think" + gt + "\n```json\n" + inner + "\n```"
    assert parse_speaker_map(raw, [0, 1]) == {0: "A", 1: "B"}


def test_parse_speaker_map_garbage_is_empty():
    assert parse_speaker_map("I am not sure about the speakers, sorry.", [0, 1]) == {}
    assert parse_speaker_map("", [0]) == {}
    assert parse_speaker_map(None, [0]) == {}


# --------------------------------------------------------------------------- #
# _pick_majority (the vote)
# --------------------------------------------------------------------------- #

def test_majority_two_of_three():
    assert _pick_majority(["A", "A", "B"]) == "A"


def test_majority_all_three():
    assert _pick_majority(["A", "A", "A"]) == "A"


def test_majority_all_distinct_is_none():
    assert _pick_majority(["A", "B", "C"]) is None


def test_majority_tie_is_none():
    assert _pick_majority(["A", "A", "B", "B"]) is None


def test_majority_ignores_none_and_rejects_lone_vote():
    assert _pick_majority(["A"]) is None              # a lone vote is not a majority
    assert _pick_majority([None, "A", "A"]) == "A"    # None votes are ignored
    assert _pick_majority([None, None]) is None


def test_majority_four_with_and_without_majority():
    assert _pick_majority(["A", "B", "A", "C"]) == "A"
    assert _pick_majority(["A", "B", "C", "D"]) is None


# --------------------------------------------------------------------------- #
# check_file (end-to-end, mocked LLM transport)
# --------------------------------------------------------------------------- #

class _BodyResp:
    """Non-streaming response stand-in: one ``read()`` of the full completion JSON."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _completion(content: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content},
                      "finish_reason": "stop"}]},
        ensure_ascii=False,
    ).encode("utf-8")


def _map_completion(pairs):
    """A completion whose content is ``{"results": [{"index": i, "speaker": s}, ...]}``."""
    return _completion(json.dumps(
        {"results": [{"index": i, "speaker": s} for i, s in pairs]},
        ensure_ascii=False,
    ))


class _Handle:
    """Minimal TaskHandle. ``cancel_after=N`` makes the N+1th ``check()`` raise."""

    def __init__(self, cancel_after: int | None = None):
        self.cancel_after = cancel_after
        self._n = 0

    def progress(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def llm_rate(self, *a, **k):
        pass

    def llm_chars(self, *a, **k):
        pass

    def llm_chunk(self, *a, **k):
        pass

    def check(self):
        self._n += 1
        if self.cancel_after is not None and self._n > self.cancel_after:
            raise TaskCancelled()

    @property
    def cancelled(self):
        return False


def _llm_cfg() -> LLMConfig:
    return LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False)


def _write(tmp_path, name, entries):
    src = tmp_path / name
    src.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return src


def _seq_urlopen(monkeypatch, completions):
    """Mock ``urlopen`` to hand back ``completions`` in order; a clear error if over-called."""
    calls = {"n": 0}

    def urlopen(*a, **k):
        calls["n"] += 1
        if calls["n"] > len(completions):
            raise AssertionError(f"LLM called {calls['n']} times, expected {len(completions)}")
        return _BodyResp(completions[calls["n"] - 1])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def test_check_file_no_disagreement_keeps_originals(tmp_path, monkeypatch):
    entries = [
        _entry("NARRATOR", "他走进房间"),
        _entry("BOB", "你好"),
        _entry("NARRATOR", "他离开了"),
    ]
    src = _write(tmp_path, "chapter.json", entries)
    before = src.read_bytes()
    # The first pass returns the same speakers as the originals → no re-sampling, one call.
    calls = _seq_urlopen(monkeypatch, [_map_completion([(0, "NARRATOR"), (1, "BOB"), (2, "NARRATOR")])])

    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 1  # no disagreement → exactly one LLM call
    assert src.read_bytes() == before  # the original is byte-for-byte unchanged
    assert json.loads((tmp_path / "chapter_checked.json").read_text("utf-8")) == entries
    assert result["checked"] == 3
    assert result["rechecked"] == 0
    assert result["changed"] == 0


def test_check_file_disagreement_resolved_by_majority(tmp_path, monkeypatch):
    entries = [_entry("NARRATOR", "a"), _entry("BOB", "b"), _entry("NARRATOR", "c")]
    src = _write(tmp_path, "ch.json", entries)
    before = src.read_bytes()
    flip = [(0, "NARRATOR"), (1, "ALICE"), (2, "NARRATOR")]  # entry 1 flips BOB -> ALICE
    # First pass flips entry 1 (BOB -> ALICE); the vote is [BOB(解析), ALICE(检查), ALICE(重试1)]
    # → 2:1 for ALICE, so it settles after ONE retry (two LLM calls total, no 2nd/3rd retry).
    calls = _seq_urlopen(monkeypatch, [
        _map_completion(flip),  # first pass (检查结果)
        _map_completion(flip),  # retry 1 (重试结果) → ALICE wins 2:1
    ])

    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 2  # 1 first pass + 1 retry (a 2:1 majority forms immediately)
    assert src.read_bytes() == before
    checked = json.loads((tmp_path / "ch_checked.json").read_text("utf-8"))
    assert checked[1] == {"speaker": "ALICE", "text": "b", "instruct": "tone"}
    assert checked[0] == entries[0] and checked[2] == entries[2]
    assert result["rechecked"] == 1
    assert result["changed"] == 1


def test_check_file_three_way_tie_triggers_second_retry(tmp_path, monkeypatch):
    entries = [_entry("NARRATOR", "a"), _entry("BOB", "b"), _entry("NARRATOR", "c")]
    src = _write(tmp_path, "ch.json", entries)
    base = [(0, "NARRATOR"), (2, "NARRATOR")]  # entries 0 and 2 always match the original
    # [BOB(解析), ALICE(检查), CARL(重试1)] is a 1:1:1 tie → escalate to a 2nd retry, which
    # returns ALICE → [BOB, ALICE, CARL, ALICE] = 2:1 for ALICE → settled (3 LLM calls).
    _seq_urlopen(monkeypatch, [
        _map_completion(base + [(1, "ALICE")]),  # first pass (检查)
        _map_completion(base + [(1, "CARL")]),   # retry 1 → 1:1:1 tie
        _map_completion(base + [(1, "ALICE")]),   # retry 2 → ALICE wins 2:1
    ])

    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=1), GenerationConfig())

    checked = json.loads((tmp_path / "ch_checked.json").read_text("utf-8"))
    assert checked[1]["speaker"] == "ALICE"
    assert result["rechecked"] == 1
    assert result["changed"] == 1


def test_check_file_no_consensus_keeps_original(tmp_path, monkeypatch):
    entries = [_entry("NARRATOR", "a"), _entry("BOB", "b"), _entry("NARRATOR", "c")]
    src = _write(tmp_path, "ch.json", entries)
    before = src.read_bytes()
    base = [(0, "NARRATOR"), (2, "NARRATOR")]
    # All five samples differ on entry 1 (BOB 解析, ALICE 检查, CARL/DAN/EDD retries) → no
    # majority even after the 3rd retry → the original is kept (4 LLM calls total).
    calls = _seq_urlopen(monkeypatch, [
        _map_completion(base + [(1, "ALICE")]),  # first pass
        _map_completion(base + [(1, "CARL")]),   # retry 1
        _map_completion(base + [(1, "DAN")]),    # retry 2
        _map_completion(base + [(1, "EDD")]),    # retry 3
    ])

    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 4  # first pass + all 3 retries (never reached a majority)
    assert src.read_bytes() == before
    checked = json.loads((tmp_path / "ch_checked.json").read_text("utf-8"))
    assert checked[1]["speaker"] == "BOB"  # original kept (no consensus)
    assert result["rechecked"] == 1
    assert result["changed"] == 0


def test_check_file_only_speaker_changes_text_instruct_intact(tmp_path, monkeypatch):
    entries = [_entry("BOB", "b", "warm"), _entry("NARRATOR", "c", "calm")]
    src = _write(tmp_path, "ch.json", entries)
    before = src.read_bytes()
    _seq_urlopen(monkeypatch, [
        _map_completion([(0, "ALICE"), (1, "NARRATOR")]),  # first pass flips entry 0
        _map_completion([(0, "ALICE"), (1, "NARRATOR")]),  # retry 1 → ALICE wins 2:1
    ])
    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=0), GenerationConfig())
    assert src.read_bytes() == before
    checked = json.loads((tmp_path / "ch_checked.json").read_text("utf-8"))
    # Only entry 0's speaker changed; its text/instruct and entry 1 are fully intact.
    assert checked[0] == {"speaker": "ALICE", "text": "b", "instruct": "warm"}
    assert checked[1] == entries[1]
    assert result["changed"] == 1


def test_check_file_failed_retry_escalates_and_keeps_majority(tmp_path, monkeypatch):
    # A failed retry contributes no votes (per-item isolation) and must not abort the batch;
    # the resulting short vote is a 1:1 tie, which escalates to a 2nd retry that restores a
    # 2:1 majority (3 LLM calls: first pass + failed retry 1 + retry 2).
    entries = [_entry("NARRATOR", "a"), _entry("BOB", "b"), _entry("NARRATOR", "c")]
    src = _write(tmp_path, "ch.json", entries)
    base = [(0, "NARRATOR"), (2, "NARRATOR")]
    good = _map_completion(base + [(1, "ALICE")])
    calls = _seq_urlopen(monkeypatch, [
        _map_completion(base + [(1, "ALICE")]),  # first pass → [BOB, ALICE]
        _completion("garbage, no JSON here"),     # retry 1: unparseable → no vote → 1:1 tie
        good,                                      # retry 2: ALICE → 2:1 majority
    ])
    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=1), GenerationConfig())
    assert calls["n"] == 3
    checked = json.loads((tmp_path / "ch_checked.json").read_text("utf-8"))
    assert checked[1]["speaker"] == "ALICE"  # two valid ALICE votes beat the gap
    assert result["changed"] == 1


def test_check_file_multi_batch_advances_without_overlap(tmp_path, monkeypatch):
    # BATCH_SIZE + 5 entries → a full batch + a partial batch; the loop must advance
    # 0 → BATCH_SIZE → end with no overlap or gap (one LLM call per batch, no disagreement).
    total = BATCH_SIZE + 5
    entries = [_entry("NARRATOR", f"t{i}") for i in range(total)]
    src = _write(tmp_path, "ch.json", entries)
    calls = _seq_urlopen(monkeypatch, [
        _map_completion([(i, "NARRATOR") for i in range(0, BATCH_SIZE)]),          # batch 1
        _map_completion([(i, "NARRATOR") for i in range(BATCH_SIZE, total)]),      # batch 2
    ])
    result = check_file(_Handle(), str(src), _llm_cfg(),
                        SpeakerCheckConfig(context_window=0), GenerationConfig())
    assert calls["n"] == 2  # two batches, each a single call
    assert result["checked"] == total
    assert result["changed"] == 0
    assert json.loads(src.read_text("utf-8")) == entries  # original untouched


def test_check_file_cancel_writes_nothing(tmp_path, monkeypatch):
    entries = [_entry("A", "x"), _entry("B", "y")]
    src = _write(tmp_path, "ch.json", entries)
    _seq_urlopen(monkeypatch, [_map_completion([(0, "A"), (1, "B")])])
    with pytest.raises(TaskCancelled):
        check_file(_Handle(cancel_after=0), str(src), _llm_cfg(),
                   SpeakerCheckConfig(context_window=1), GenerationConfig())
    assert not (tmp_path / "ch_checked.json").exists()


def test_check_file_requires_model_name(tmp_path):
    src = _write(tmp_path, "ch.json", [_entry("A", "x")])
    with pytest.raises(RuntimeError):
        check_file(_Handle(), str(src), LLMConfig(stream=False, model_name=""),
                   SpeakerCheckConfig(), GenerationConfig())
