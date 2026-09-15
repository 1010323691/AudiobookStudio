"""Offline tests for the 段落混合检查 engine (``backend/engines/mix_check.py``).

Pins the stage's two mechanisms and the shared ``_checked`` file model, with no
network access: the LLM transport is a mocked ``urllib.request.urlopen`` (the same
pattern ``test_speaker_check.py`` uses).

- punctuation-only deletion — deterministic, no LLM (a batch of purely deletable
  entries makes ZERO LLM calls);
- the LLM ``keep`` / ``split`` verdict parser (shape-tolerant, non-targets dropped,
  garbage → keep, never guess);
- the four gates of ``validate_split_parts`` (≥2 well-formed parts, re-assembly of
  the original text — quote/whitespace-stripped on both sides, tolerating the
  prompt-permitted split-point punctuation fix at part seams — file-wide roster
  whitelist, ≥2 distinct subjects) — ANY failure rejects the whole split;
- the two retry mechanisms: (a) a batch whose reply is unreadable (call failure or
  zero usable verdicts) is re-asked ONCE in place, immediately — its recovered
  verdicts process normally and the batch never waits for the end pass; (b) the
  single end-of-file retry pass for gate-rejected splits: all failed entries
  (across all batches) are re-asked in context-windowed groups, a re-proposed
  split must pass the same gates again (rescued), anything still failing is
  abandoned (kept unchanged) — each entry/batch is retried at most once;
- ``rebuild_entries`` renumbering (positions unique & contiguous, order stable,
  split parts get an empty ``instruct``, nothing dropped or skipped);
- end-to-end ``mix_check_file`` on the three required input classes (旁白+单角色台词,
  多角色台词, 正常单主体段落) plus rejected splits, cancel, multi-batch and
  fail-fast behaviour;
- the cross-stage handoff: 角色匹配检查 updates the mix check's ``_checked`` file
  IN PLACE (never ``_checked_checked.json``) and the base file stays pristine;
- the API resolver: the mix check always reads the base; 角色匹配检查 prefers a
  FRESH ``_checked`` (mtime guard) and falls back to the base when it is stale.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.config import GenerationConfig, LLMConfig, MixCheckConfig, SpeakerCheckConfig
from backend.core.tasks import TaskCancelled
from backend.engines import mix_check
from backend.engines.mix_check import (
    group_retry_indices,
    is_deletable_text,
    mix_check_file,
    parse_mix_map,
    rebuild_entries,
    validate_split_parts,
)
from backend.engines.speaker_check import build_batch_window, check_file


def _entry(speaker: str, text: str, instruct: str = "tone") -> dict:
    return {"speaker": speaker, "text": text, "instruct": instruct}


@pytest.fixture
def inert_layout(monkeypatch):
    """Pin the engine's layout lookup to an unset (inert) workspace, so the
    stale-manifest check can never reach a real workspace from a ``tmp_path`` run."""
    monkeypatch.setattr(mix_check, "get_layout", lambda: core_paths.Layout(None))


# --------------------------------------------------------------------------- #
# is_deletable_text (deterministic punctuation-only deletion)
# --------------------------------------------------------------------------- #

def test_deletable_punctuation_only_is_deleted():
    for text in ("……", "。！？", "——", "~", "〜", "～", "～……", "   ", "", None):
        assert is_deletable_text(text) is True, repr(text)


def test_deletable_keeps_any_content():
    for text in ("嗯……", "2024", "他", "A", "～好"):
        assert is_deletable_text(text) is False, repr(text)


# --------------------------------------------------------------------------- #
# parse_mix_map (batch verdict parser)
# --------------------------------------------------------------------------- #

def test_parse_mix_map_results_object():
    text = (
        '{"results": [{"index": 0, "action": "keep"},'
        ' {"index": 2, "action": "split", "parts":'
        ' [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]}]}'
    )
    m = parse_mix_map(text, [0, 1, 2])
    assert m[0] == {"action": "keep"}
    assert m[2]["action"] == "split"
    assert m[2]["parts"][1]["speaker"] == "林晚"
    assert 1 not in m  # no verdict for 1 → the caller keeps that entry


def test_parse_mix_map_bare_list_and_index_keyed():
    parts = [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]
    expected = {2: {"action": "keep"}, 5: {"action": "split", "parts": parts}}
    text = (
        '[{"index": 2, "action": "keep"},'
        ' {"index": 5, "action": "split", "parts":'
        ' [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]}]'
    )
    assert parse_mix_map(text, [2, 5, 8]) == expected  # bare list, non-target 8 dropped
    keyed = (
        '{"2": "keep", "5": {"action": "split", "parts":'
        ' [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]}}'
    )
    assert parse_mix_map(keyed, [2, 5]) == expected  # index-keyed object


def test_parse_mix_map_ignores_non_targets():
    text = '{"results": [{"index": 0, "action": "keep"}, {"index": 5, "action": "keep"}]}'
    assert parse_mix_map(text, [0, 1]) == {0: {"action": "keep"}}


def test_parse_mix_map_garbage_and_single_target_fallback():
    assert parse_mix_map("I am not sure about these entries, sorry.", [0, 1]) == {}
    assert parse_mix_map("", [0]) == {}
    assert parse_mix_map(None, [0]) == {}
    # Single-target batch: a bare {"action": ...} object applies to the lone target.
    assert parse_mix_map('{"action": "keep"}', [7]) == {7: {"action": "keep"}}
    parts = [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]
    raw = (
        '{"action": "split", "parts":'
        ' [{"speaker": "NARRATOR", "text": "a"}, {"speaker": "林晚", "text": "b"}]}'
    )
    assert parse_mix_map(raw, [7]) == {7: {"action": "split", "parts": parts}}


# --------------------------------------------------------------------------- #
# validate_split_parts (the four gates)
# --------------------------------------------------------------------------- #

def test_validate_split_accepts_exact_partition():
    original = {"speaker": "NARRATOR", "text": "ABCD"}
    allowed = frozenset({"NARRATOR", "林晚"})
    ok, why = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "林晚", "text": "CD"}],
        allowed,
    )
    assert ok and why == ""
    # Whitespace inside part texts is tolerated (both sides are stripped).
    ok, _ = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "A B"}, {"speaker": "林晚", "text": "CD"}],
        allowed,
    )
    assert ok


def test_validate_split_rejects_reorder_drop_add():
    original = {"speaker": "NARRATOR", "text": "ABCD"}
    allowed = frozenset({"NARRATOR", "林晚"})
    # Reorder: the parts concatenate to "CDAB", not the original.
    ok, _ = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "CD"}, {"speaker": "林晚", "text": "AB"}],
        allowed,
    )
    assert not ok
    # Drop a character.
    ok, _ = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "林晚", "text": "C"}],
        allowed,
    )
    assert not ok
    # Add a character.
    ok, _ = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "林晚", "text": "CDE"}],
        allowed,
    )
    assert not ok
    # Fewer than 2 parts is not a split at all.
    ok, why = validate_split_parts(
        original, [{"speaker": "NARRATOR", "text": "ABCD"}], allowed
    )
    assert not ok and "少于 2 段" in why
    # A non-object part is malformed.
    ok, _ = validate_split_parts(original, ["AB", "CD"], allowed)
    assert not ok


def test_validate_split_rejects_unknown_speaker():
    original = {"speaker": "NARRATOR", "text": "ABCD"}
    # 赵刚 is absent from this entry's local window but IS in the book-wide roster
    # → accepted (pins the roster-whitelist design, not a window-only whitelist).
    roster = frozenset({"NARRATOR", "林晚", "赵刚"})
    ok, _ = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "赵刚", "text": "CD"}],
        roster,
    )
    assert ok
    # 周九 is outside the roster → the whole split is rejected, naming the offender.
    ok, why = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "周九", "text": "CD"}],
        roster,
    )
    assert not ok and "周九" in why


def test_validate_split_rejects_single_subject():
    original = {"speaker": "NARRATOR", "text": "ABCD"}
    # Two parts, but one distinct speaker: a same-subject "split" is not a mix.
    ok, why = validate_split_parts(
        original,
        [{"speaker": "NARRATOR", "text": "AB"}, {"speaker": "NARRATOR", "text": "CD"}],
        frozenset({"NARRATOR"}),
    )
    assert not ok and "≥2 个不同主体" in why


def test_validate_split_tolerates_quote_removal():
    # The prompt permits the model to strip the quotation marks around dialogue parts:
    # the re-assembly gate compares quote-insensitively, so the stripped parts still
    # re-assemble the original.
    original = {"speaker": "NARRATOR", "text": "林晚抬头：「你终于来了。」窗外雨声渐紧。"}
    allowed = frozenset({"NARRATOR", "林晚"})
    ok, _ = validate_split_parts(
        original,
        [
            {"speaker": "NARRATOR", "text": "林晚抬头："},
            {"speaker": "林晚", "text": "你终于来了。"},  # quotes stripped by the model
            {"speaker": "NARRATOR", "text": "窗外雨声渐紧。"},
        ],
        allowed,
    )
    assert ok


def test_validate_split_tolerates_seam_punctuation_fix():
    # The prompt permits fixing a dangling "：" / "，" left at a split point (→ "。") —
    # accepted only at part seams (trailing char of one part / leading of the next).
    original = {"speaker": "NARRATOR", "text": "林晚抬头：「你来了。」，她转身。"}
    allowed = frozenset({"NARRATOR", "林晚"})
    ok, _ = validate_split_parts(
        original,
        [
            {"speaker": "NARRATOR", "text": "林晚抬头。"},   # trailing "：" fixed at the seam
            {"speaker": "林晚", "text": "「你来了。」"},
            {"speaker": "NARRATOR", "text": "。她转身。"},   # leading "，" fixed at the seam
        ],
        allowed,
    )
    assert ok


def test_validate_split_rejects_mid_part_punctuation_change():
    # The seam fix does NOT apply mid-part: a "：" rewritten to "，" away from any part
    # boundary is a real text change → rejected (only the two documented edits pass).
    original = {"speaker": "NARRATOR", "text": "林晚抬头：「你来了。」她转身。"}
    allowed = frozenset({"NARRATOR", "林晚"})
    ok, why = validate_split_parts(
        original,
        [
            # "：" → "，" mid-part (the part continues with the quoted utterance).
            {"speaker": "NARRATOR", "text": "林晚抬头，「你来了。」"},
            {"speaker": "NARRATOR", "text": "她转身。"},
        ],
        allowed,
    )
    assert not ok and "无法拼回原文" in why


def test_validate_split_rejects_quote_only_part():
    # A part whose text is nothing but quotation marks carries no content → gate (a).
    original = {"speaker": "NARRATOR", "text": "「你来了。」她转身。"}
    ok, why = validate_split_parts(
        original,
        [
            {"speaker": "林晚", "text": "「"},
            {"speaker": "NARRATOR", "text": "你来了。」她转身。"},
        ],
        frozenset({"NARRATOR", "林晚"}),
    )
    assert not ok and "text 为空" in why


# --------------------------------------------------------------------------- #
# rebuild_entries (renumber by construction)
# --------------------------------------------------------------------------- #

def test_rebuild_entries_order_and_shape():
    original = [
        {"speaker": "NARRATOR", "text": "a", "instruct": "i0"},
        {"speaker": "NARRATOR", "text": "……", "instruct": "i1"},   # deletable → dropped
        {"speaker": "NARRATOR", "text": "abcd", "instruct": "i2"},  # split → expanded
        {"speaker": "林晚", "text": "e", "instruct": "i3"},
    ]
    split_parts = {2: [
        {"speaker": "NARRATOR", "text": "ab"},
        {"speaker": "林晚", "text": "cd"},
    ]}
    out = rebuild_entries(original, {1}, split_parts)
    assert out == [
        {"speaker": "NARRATOR", "text": "a", "instruct": "i0"},   # kept: shallow copy
        {"speaker": "NARRATOR", "text": "ab", "instruct": ""},    # part 1: empty instruct
        {"speaker": "林晚", "text": "cd", "instruct": ""},        # part 2: empty instruct
        {"speaker": "林晚", "text": "e", "instruct": "i3"},       # kept
    ]
    assert out[0] is not original[0]  # a copy, never the original dict
    # The input list is left intact (the rebuild never mutates its source).
    assert original[1] == {"speaker": "NARRATOR", "text": "……", "instruct": "i1"}
    assert original[2] == {"speaker": "NARRATOR", "text": "abcd", "instruct": "i2"}


def test_batch_window_skip_excludes_deletable_targets():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    # Index 2 sits inside the target range but is skipped (punctuation-only): it
    # still appears in the window, unflagged, like context. (The block starts at 0,
    # so the window has no leading context and one trailing entry, index 3.)
    w = build_batch_window(entries, start=0, size=3, n=1, skip={2})
    assert [e["index"] for e in w] == [0, 1, 2, 3]
    assert [e.get("target") for e in w] == [True, True, None, None]
    # The default (skip=None) preserves the original contract.
    w0 = build_batch_window(entries, start=0, size=3, n=1)
    assert [e["index"] for e in w0] == [0, 1, 2, 3]
    assert [e.get("target") for e in w0] == [True, True, True, None]


def test_retry_grouping_gap_and_batch_cap():
    # Consecutive failures share a call while their ±n context windows overlap; a
    # larger gap or the batch cap starts a new group (one LLM call per group).
    assert group_retry_indices([], 4, 20) == []
    assert group_retry_indices([5], 4, 20) == [[5]]
    # Gaps ≤ n (4) stay together: 6-5=1, 10-6=4.
    assert group_retry_indices([5, 6, 10], 4, 20) == [[5, 6, 10]]
    # Gap 6 > 4 → a new group (one call must not span the gap).
    assert group_retry_indices([5, 11], 4, 20) == [[5], [11]]
    # The batch cap splits a long run into groups of ≤ batch targets.
    assert group_retry_indices(list(range(25)), 4, 10) == [
        list(range(0, 10)), list(range(10, 20)), list(range(20, 25))]
    # n=0: only truly adjacent (gap ≤ 1) indices share a call.
    assert group_retry_indices([5, 6, 8], 0, 20) == [[5, 6], [8]]


# --------------------------------------------------------------------------- #
# mix_check_file (end-to-end, mocked LLM transport)
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


def _mix_completion(items) -> bytes:
    """A completion whose content is ``{"results": items}`` (mix-check verdict shape:
    each item is ``{"index", "action", "parts"?}``)."""
    return _completion(json.dumps({"results": items}, ensure_ascii=False))


def _map_completion(pairs) -> bytes:
    """A completion whose content is ``{"results": [{"index": i, "speaker": s}, ...]}``
    (the 角色匹配检查 shape — used by the cross-stage handoff test)."""
    return _completion(json.dumps(
        {"results": [{"index": i, "speaker": s} for i, s in pairs]},
        ensure_ascii=False,
    ))


class _Handle:
    """Minimal TaskHandle. ``cancel_after=N`` makes the N+1th ``check()`` raise.
    ``logs`` collects ``(level, msg)`` pairs for assertion."""

    def __init__(self, cancel_after: int | None = None):
        self.cancel_after = cancel_after
        self._n = 0
        self.logs = []

    def progress(self, *a, **k):
        pass

    def log(self, *a, **k):
        self.logs.append((a[1] if len(a) > 1 else "INFO", a[0]))

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
    """Mock ``urlopen`` to hand back ``completions`` in order (recording each request
    body); a clear error if the LLM is over-called."""
    calls = {"n": 0, "bodies": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        if calls["n"] > len(completions):
            raise AssertionError(f"LLM called {calls['n']} times, expected {len(completions)}")
        calls["bodies"].append(json.loads(req.data.decode("utf-8")))
        return _BodyResp(completions[calls["n"] - 1])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def test_mix_file_split_narration_plus_line(tmp_path, monkeypatch, inert_layout):
    """Class 1: a NARRATOR entry mixing narration with one character's line → split."""
    entries = [
        _entry("NARRATOR", "夜色像潮水一样漫进街巷。"),
        _entry("林晚", "「你终于来了。」"),
        _entry("NARRATOR", "林晚抬头：「你终于来了。」窗外的雨声一阵紧似一阵。"),
        _entry("林晚", "「我等你很久了。」"),
    ]
    src = _write(tmp_path, "chapter.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "NARRATOR", "text": "林晚抬头："},
        {"speaker": "林晚", "text": "「你终于来了。」"},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。"},
    ]
    calls = _seq_urlopen(monkeypatch, [_mix_completion([
        {"index": 0, "action": "keep"},
        {"index": 1, "action": "keep"},
        {"index": 2, "action": "split", "parts": parts},
        {"index": 3, "action": "keep"},
    ])])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 1  # one batch → one LLM call
    assert src.read_bytes() == before  # the base file is byte-for-byte unchanged
    checked = tmp_path / "chapter_checked.json"
    assert json.loads(checked.read_text("utf-8")) == [
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "tone"},
        {"speaker": "林晚", "text": "「你终于来了。」", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "林晚抬头：", "instruct": ""},
        {"speaker": "林晚", "text": "「你终于来了。」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。", "instruct": ""},
        {"speaker": "林晚", "text": "「我等你很久了。」", "instruct": "tone"},
    ]
    assert result == {
        "input_name": "chapter.json",
        "output_name": "chapter_checked.json",
        "output_path": str(checked),
        "total": 4,
        "kept": 3,
        "deleted": 0,
        "splits": 1,
        "parts": 3,
        "new_total": 6,
        "speakers": ["NARRATOR", "林晚"],
        "rejected": 0,
        "recovered": 0,
        "abandoned": 0,
    }
    # The book-wide roster (and only the roster) is injected into the user prompt.
    body = calls["bodies"][0]
    assert body["model"] == "m"
    user = body["messages"][1]["content"]
    assert "【本书角色】林晚" in user
    assert "target" in user  # the window JSON (with target flags) is embedded


def test_mix_file_adopts_prompt_style_split(tmp_path, monkeypatch, inert_layout):
    """The model applies the two prompt-permitted edits (quotes stripped around the
    dialogue part, the dangling "：" at the split point fixed to "。") → the split still
    clears the re-assembly gate and the unwrapped texts land in the artifact. (林晚 must
    sit in the roster — the part-speaker whitelist is NARRATOR ∪ roster — so the file
    carries a stored 林晚 entry, as in the class-1 fixture.)"""
    entries = [
        _entry("NARRATOR", "夜色像潮水一样漫进街巷。"),
        _entry("NARRATOR", "林晚抬头：「你终于来了。」窗外的雨声一阵紧似一阵。"),
        _entry("林晚", "「我等你很久了。」"),
    ]
    src = _write(tmp_path, "chapter2.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "NARRATOR", "text": "林晚抬头。"},
        {"speaker": "林晚", "text": "你终于来了。"},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。"},
    ]
    calls = _seq_urlopen(monkeypatch, [_mix_completion([
        {"index": 0, "action": "keep"},
        {"index": 1, "action": "split", "parts": parts},
        {"index": 2, "action": "keep"},
    ])])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert calls["n"] == 1
    assert src.read_bytes() == before  # the base file is byte-for-byte unchanged
    checked = json.loads((tmp_path / "chapter2_checked.json").read_text("utf-8"))
    assert checked == [
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "林晚抬头。", "instruct": ""},
        {"speaker": "林晚", "text": "你终于来了。", "instruct": ""},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。", "instruct": ""},
        {"speaker": "林晚", "text": "「我等你很久了。」", "instruct": "tone"},
    ]
    assert result["splits"] == 1
    assert result["parts"] == 3
    assert result["rejected"] == 0
    assert result["abandoned"] == 0


def test_mix_file_split_multi_character(tmp_path, monkeypatch, inert_layout):
    """Class 2: one entry holding lines of TWO characters (plus a narration seam) → split."""
    entries = [
        _entry("NARRATOR", "茶桌上静得落针可闻。"),
        _entry("赵刚", "「这笔账，今天必须算清。」林晚冷冷接话：「算到什么时候？」"),
        _entry("林晚", "「你说了算吗？」"),
    ]
    src = _write(tmp_path, "ch2.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "赵刚", "text": "「这笔账，今天必须算清。」"},
        {"speaker": "NARRATOR", "text": "林晚冷冷接话："},
        {"speaker": "林晚", "text": "「算到什么时候？」"},
    ]
    calls = _seq_urlopen(monkeypatch, [_mix_completion([
        {"index": 0, "action": "keep"},
        {"index": 1, "action": "split", "parts": parts},
        {"index": 2, "action": "keep"},
    ])])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 1
    assert src.read_bytes() == before
    checked = tmp_path / "ch2_checked.json"
    assert json.loads(checked.read_text("utf-8")) == [
        {"speaker": "NARRATOR", "text": "茶桌上静得落针可闻。", "instruct": "tone"},
        {"speaker": "赵刚", "text": "「这笔账，今天必须算清。」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "林晚冷冷接话：", "instruct": ""},
        {"speaker": "林晚", "text": "「算到什么时候？」", "instruct": ""},
        {"speaker": "林晚", "text": "「你说了算吗？」", "instruct": "tone"},
    ]
    assert result["total"] == 3
    assert result["kept"] == 2
    assert result["deleted"] == 0
    assert result["splits"] == 1
    assert result["parts"] == 3
    assert result["new_total"] == 5
    assert result["speakers"] == ["NARRATOR", "林晚", "赵刚"]


def test_mix_file_keep_single_subject(tmp_path, monkeypatch, inert_layout):
    """Class 3: normal single-subject paragraphs → kept byte-identical (even if the
    label is wrong — fixing attribution is the NEXT stage's job)."""
    entries = [
        _entry("NARRATOR", "他推开门，走进来。"),
        _entry("BOB", "「别进来。」"),
    ]
    src = _write(tmp_path, "ch3.json", entries)
    before = src.read_bytes()
    calls = _seq_urlopen(monkeypatch, [_mix_completion([
        {"index": 0, "action": "keep"},
        {"index": 1, "action": "keep"},
    ])])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 1
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch3_checked.json").read_text("utf-8")) == entries
    assert result["kept"] == 2
    assert result["splits"] == 0
    assert result["deleted"] == 0
    assert result["new_total"] == 2


def test_mix_file_rejects_same_speaker_split(tmp_path, monkeypatch, inert_layout):
    # A split whose parts all share one speaker is NOT a mix → rejected by the gates;
    # the one retry asks again and the model re-judges it `keep` → the split is
    # abandoned, the entry stays unchanged, and the reasons land in the task log.
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("NARRATOR", "屋里一片狼藉。桌上摊着半封没写完的信。"),
    ]
    src = _write(tmp_path, "ch4.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "NARRATOR", "text": "屋里一片狼藉。"},
        {"speaker": "NARRATOR", "text": "桌上摊着半封没写完的信。"},
    ]
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "split", "parts": parts},
        ]),
        _mix_completion([{"index": 1, "action": "keep"}]),  # the retry: re-judged keep
    ])
    h = _Handle()

    result = mix_check_file(h, str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert calls["n"] == 2  # main batch + the single retry pass
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch4_checked.json").read_text("utf-8")) == entries
    assert result["splits"] == 0
    assert result["new_total"] == 2
    assert result["rejected"] == 1
    assert result["recovered"] == 0
    assert result["abandoned"] == 1
    assert any("拆分未通过校验" in msg for _lv, msg in h.logs)
    assert any("放弃拆分" in msg for _lv, msg in h.logs)


def test_mix_file_rejects_concat_mismatch(tmp_path, monkeypatch, inert_layout):
    # Part texts that do not concatenate EXACTLY back to the original (a dropped
    # character) → the split is rejected; the one retry proposes the same broken split
    # again → still failing → abandoned, the entry is kept verbatim.
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("林晚", "「快跑！」林晚喊道。"),
    ]
    src = _write(tmp_path, "ch5.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "林晚", "text": "「快跑！」"},
        {"speaker": "NARRATOR", "text": "林晚喊。"},  # dropped the 道
    ]
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "split", "parts": parts},
        ]),
        _mix_completion([  # the retry: the same broken split is proposed again
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "split", "parts": parts},
        ]),
    ])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert calls["n"] == 2
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch5_checked.json").read_text("utf-8")) == entries
    assert result["splits"] == 0
    assert result["new_total"] == 2
    assert result["rejected"] == 1
    assert result["recovered"] == 0
    assert result["abandoned"] == 1


def test_mix_retry_rescues_bad_split(tmp_path, monkeypatch, inert_layout):
    """A first-pass split that fails a gate is re-asked ONCE with context + the failure
    reason; a corrected re-split that passes all four gates is adopted (rescued)."""
    entries = [
        _entry("NARRATOR", "夜色像潮水一样漫进街巷。"),
        _entry("林晚", "「你终于来了。」"),
        _entry("NARRATOR", "林晚抬头：「你终于来了。」窗外的雨声一阵紧似一阵。"),
        _entry("林晚", "「我等你很久了。」"),
    ]
    src = _write(tmp_path, "ch10.json", entries)
    before = src.read_bytes()
    good = [
        {"speaker": "NARRATOR", "text": "林晚抬头："},
        {"speaker": "林晚", "text": "「你终于来了。」"},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。"},
    ]
    bad = [
        {"speaker": "NARRATOR", "text": "林晚抬头："},
        {"speaker": "林晚", "text": "「你终于来了。」"},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵"},  # dropped the trailing 。
    ]
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "keep"},
            {"index": 2, "action": "split", "parts": bad},
            {"index": 3, "action": "keep"},
        ]),
        _mix_completion([{"index": 2, "action": "split", "parts": good}]),  # the retry
    ])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 2  # one main batch + the single retry pass
    assert src.read_bytes() == before  # the base file is byte-for-byte unchanged
    checked = tmp_path / "ch10_checked.json"
    assert json.loads(checked.read_text("utf-8")) == [
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "tone"},
        {"speaker": "林晚", "text": "「你终于来了。」", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "林晚抬头：", "instruct": ""},
        {"speaker": "林晚", "text": "「你终于来了。」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。", "instruct": ""},
        {"speaker": "林晚", "text": "「我等你很久了。」", "instruct": "tone"},
    ]
    assert result["splits"] == 1  # the rescued split counts toward the final artifact
    assert result["kept"] == 3
    assert result["new_total"] == 6
    assert result["rejected"] == 1
    assert result["recovered"] == 1
    assert result["abandoned"] == 0
    # The retry call carries the gate reason and a ±1 context window around index 2.
    retry_user = calls["bodies"][1]["messages"][1]["content"]
    assert "【重试提示】" in retry_user
    assert "无法拼回原文" in retry_user  # the failed gate's reason, so the model can fix it
    assert "index=2" in retry_user  # the hint uses the window's index field (0-based)
    assert '"index": 2' in retry_user and '"target": true' in retry_user
    assert '"index": 1' in retry_user  # the leading context entry is present


def test_mix_retry_two_far_failures_two_calls(tmp_path, monkeypatch, inert_layout):
    """Rejected entries far apart (gap > context window) get SEPARATE retry calls — one
    call must not span the whole file between them; both rescued splits land in order."""
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("林晚", "「快跑！」林晚喊道。"),       # index 1 → first-pass split drops 道
        _entry("NARRATOR", "t2"),
        _entry("NARRATOR", "t3"),
        _entry("NARRATOR", "t4"),
        _entry("NARRATOR", "t5"),
        _entry("赵刚", "「住手。」赵刚说。"),         # index 6 → first-pass split drops 说
    ]
    src = _write(tmp_path, "ch11.json", entries)
    before = src.read_bytes()
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "split", "parts": [
                {"speaker": "林晚", "text": "「快跑！」"},
                {"speaker": "NARRATOR", "text": "林晚喊。"},
            ]},
            {"index": 2, "action": "keep"},
            {"index": 3, "action": "keep"},
        ]),
        _mix_completion([
            {"index": 4, "action": "keep"},
            {"index": 5, "action": "keep"},
            {"index": 6, "action": "split", "parts": [
                {"speaker": "赵刚", "text": "「住手。」"},
                {"speaker": "NARRATOR", "text": "赵刚。"},
            ]},
        ]),
        _mix_completion([{"index": 1, "action": "split", "parts": [  # retry group 1
            {"speaker": "林晚", "text": "「快跑！」"},
            {"speaker": "NARRATOR", "text": "林晚喊道。"},
        ]}]),
        _mix_completion([{"index": 6, "action": "split", "parts": [  # retry group 2
            {"speaker": "赵刚", "text": "「住手。」"},
            {"speaker": "NARRATOR", "text": "赵刚说。"},
        ]}]),
    ])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(batch_size=4, context_window=0),
                           GenerationConfig())

    assert calls["n"] == 4  # 2 main batches + 2 separate retry calls (gap 5 > n=0)
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch11_checked.json").read_text("utf-8")) == [
        {"speaker": "NARRATOR", "text": "他推开门。", "instruct": "tone"},
        {"speaker": "林晚", "text": "「快跑！」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "林晚喊道。", "instruct": ""},
        {"speaker": "NARRATOR", "text": "t2", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "t3", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "t4", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "t5", "instruct": "tone"},
        {"speaker": "赵刚", "text": "「住手。」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "赵刚说。", "instruct": ""},
    ]
    assert result["total"] == 7
    assert result["kept"] == 5
    assert result["splits"] == 2
    assert result["parts"] == 4
    assert result["new_total"] == 9
    assert result["rejected"] == 2
    assert result["recovered"] == 2
    assert result["abandoned"] == 0
    # Each retry call covers only its own failed entry (no target flag on the gap).
    retry_user = calls["bodies"][2]["messages"][1]["content"]
    assert '"index": 1' in retry_user and '"target": true' in retry_user
    assert "index=1" in retry_user
    assert "index=6" not in retry_user  # the far entry is not in this call's scope


def test_mix_file_retries_unparseable_batch_in_place(tmp_path, monkeypatch, inert_layout):
    """A batch whose reply is unreadable (zero usable verdicts) is re-asked ONCE, in
    place, immediately — the retry's verdicts process like any other batch's, and the
    batch never waits for the end-of-file retry pass (which only covers gate rejects)."""
    entries = [
        _entry("林晚", "「你终于来了。」"),
        _entry("NARRATOR", "林晚抬头：「快进来。」夜风紧吹。"),
    ]
    src = _write(tmp_path, "ch12.json", entries)
    before = src.read_bytes()
    parts = [
        {"speaker": "NARRATOR", "text": "林晚抬头："},
        {"speaker": "林晚", "text": "「快进来。」"},
        {"speaker": "NARRATOR", "text": "夜风紧吹。"},
    ]
    calls = _seq_urlopen(monkeypatch, [
        _completion("I'm sorry, I cannot produce the JSON you asked for."),  # garbage
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "split", "parts": parts},
        ]),  # the immediate in-place retry
    ])
    h = _Handle()

    result = mix_check_file(h, str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert calls["n"] == 2  # the retry fires in place, not at the end of the file
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch12_checked.json").read_text("utf-8")) == [
        {"speaker": "林晚", "text": "「你终于来了。」", "instruct": "tone"},
        {"speaker": "NARRATOR", "text": "林晚抬头：", "instruct": ""},
        {"speaker": "林晚", "text": "「快进来。」", "instruct": ""},
        {"speaker": "NARRATOR", "text": "夜风紧吹。", "instruct": ""},
    ]
    assert result["kept"] == 1
    assert result["splits"] == 1
    assert result["parts"] == 3
    assert result["new_total"] == 4
    assert result["rejected"] == 0  # a batch retry is not a gate rejection
    assert result["recovered"] == 0
    assert result["abandoned"] == 0
    assert any("立即重试" in msg for _lv, msg in h.logs)
    assert not any("带上下文窗口" in msg for _lv, msg in h.logs)  # the end pass never ran


def test_mix_file_batch_retry_exhaustion_keeps_batch(tmp_path, monkeypatch, inert_layout):
    """After the immediate batch retry also yields nothing, the batch is kept unchanged
    and does NOT enter the end-of-file retry pass (never retried twice)."""
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("NARRATOR", "屋里一片狼藉。"),
    ]
    src = _write(tmp_path, "ch13.json", entries)
    before = src.read_bytes()
    garbage = _completion("sorry, no JSON today")
    calls = _seq_urlopen(monkeypatch, [garbage, garbage])
    h = _Handle()

    result = mix_check_file(h, str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert calls["n"] == 2  # first attempt + exactly one in-place retry — no third call
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch13_checked.json").read_text("utf-8")) == entries
    assert result["kept"] == 2
    assert result["splits"] == 0
    assert result["new_total"] == 2
    assert result["rejected"] == 0
    assert result["abandoned"] == 0
    assert any("重试后本批仍无有效判定" in msg for _lv, msg in h.logs)


def test_mix_file_retries_failed_call_in_place(tmp_path, monkeypatch, inert_layout):
    """A failed LLM call (transport error) also triggers the one immediate in-place
    retry of the batch; the recovered verdicts process normally."""
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("NARRATOR", "屋里一片狼藉。"),
    ]
    src = _write(tmp_path, "ch14.json", entries)
    before = src.read_bytes()
    good = _mix_completion([{"index": 0, "action": "keep"}, {"index": 1, "action": "keep"}])
    state = {"n": 0}

    def flaky_urlopen(req, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("connection reset by peer")
        return _BodyResp(good)

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    h = _Handle()

    result = mix_check_file(h, str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(context_window=0), GenerationConfig())

    assert state["n"] == 2  # the failed call + one immediate retry
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch14_checked.json").read_text("utf-8")) == entries
    assert result["kept"] == 2
    assert result["new_total"] == 2
    assert any("本批响应无法解析" in msg for _lv, msg in h.logs)
    assert any("立即重试" in msg for _lv, msg in h.logs)


def test_mix_file_deletes_punct_only_batch_llm_free(tmp_path, monkeypatch, inert_layout):
    # Punctuation-only entries are deleted deterministically; a batch made up ONLY of
    # deletable entries never reaches the LLM (2 batches of 2 → exactly 1 call).
    entries = [
        _entry("NARRATOR", "他推开门。"),
        _entry("NARRATOR", "……"),
        _entry("NARRATOR", "。！"),
        _entry("NARRATOR", "——"),
    ]
    src = _write(tmp_path, "ch6.json", entries)
    before = src.read_bytes()
    calls = _seq_urlopen(monkeypatch, [_mix_completion([{"index": 0, "action": "keep"}])])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(batch_size=2, context_window=0),
                           GenerationConfig())

    assert calls["n"] == 1  # batch 2 (indices 2,3) is purely deletable → no LLM call
    assert src.read_bytes() == before
    checked = tmp_path / "ch6_checked.json"
    assert json.loads(checked.read_text("utf-8")) == [entries[0]]
    assert result["total"] == 4
    assert result["kept"] == 1
    assert result["deleted"] == 3
    assert result["splits"] == 0
    assert result["new_total"] == 1


def test_mix_file_cancel_writes_nothing(tmp_path, monkeypatch, inert_layout):
    entries = [_entry("A", "x"), _entry("B", "y")]
    src = _write(tmp_path, "ch7.json", entries)
    before = src.read_bytes()
    calls = _seq_urlopen(monkeypatch, [_mix_completion([{"index": 0, "action": "keep"},
                                                        {"index": 1, "action": "keep"}])])
    with pytest.raises(TaskCancelled):
        mix_check_file(_Handle(cancel_after=0), str(src), _llm_cfg(), MixCheckConfig(),
                      SpeakerCheckConfig(context_window=1), GenerationConfig())
    assert calls["n"] == 0  # cancelled before the first LLM call
    assert not (tmp_path / "ch7_checked.json").exists()  # the single write never happens
    assert src.read_bytes() == before


def test_mix_file_multi_batch_advances_without_overlap(tmp_path, monkeypatch, inert_layout):
    # 7 entries at batch size 3 → batches [0..2] [3..5] [6]: three calls, no overlap
    # or gap; all keeps → the file comes back unchanged.
    entries = [_entry("NARRATOR", f"t{i}") for i in range(7)]
    src = _write(tmp_path, "ch8.json", entries)
    before = src.read_bytes()
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([{"index": i, "action": "keep"} for i in range(0, 3)]),
        _mix_completion([{"index": i, "action": "keep"} for i in range(3, 6)]),
        _mix_completion([{"index": 6, "action": "keep"}]),
    ])

    result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                           SpeakerCheckConfig(batch_size=3, context_window=0),
                           GenerationConfig())

    assert calls["n"] == 3  # one LLM call per batch
    assert src.read_bytes() == before
    assert json.loads((tmp_path / "ch8_checked.json").read_text("utf-8")) == entries
    assert result["total"] == 7
    assert result["kept"] == 7
    assert result["new_total"] == 7


def test_mix_file_requires_model_name(tmp_path):
    src = _write(tmp_path, "ch9.json", [_entry("A", "x")])
    with pytest.raises(RuntimeError):
        mix_check_file(_Handle(), str(src), LLMConfig(stream=False, model_name=""),
                      MixCheckConfig(), SpeakerCheckConfig(), GenerationConfig())


def test_mix_then_speaker_cross_stage_in_place(tmp_path, monkeypatch, inert_layout):
    """The handoff (requirements 4/5): the mix check writes ``_checked``; the 角色匹配检查
    then runs on that SAME file and updates it IN PLACE — the final file holds the split
    AND the speaker correction, the output path is still the single ``_checked`` file
    (never ``_checked_checked.json``), and the base file stays pristine."""
    entries = [
        _entry("NARRATOR", "夜色像潮水一样漫进街巷。"),
        _entry("林晚", "「你终于来了。」"),
        _entry("NARRATOR", "林晚抬头：「你终于来了。」窗外的雨声一阵紧似一阵。"),
        _entry("林晚", "「我等你很久了。」"),
    ]
    src = _write(tmp_path, "chx.json", entries)
    before = src.read_bytes()
    checked = tmp_path / "chx_checked.json"
    parts = [
        {"speaker": "NARRATOR", "text": "林晚抬头："},
        {"speaker": "林晚", "text": "「你终于来了。」"},
        {"speaker": "NARRATOR", "text": "窗外的雨声一阵紧似一阵。"},
    ]
    # The rebuilt file indexes the split parts at 2/3/4; the speaker check flips the
    # middle one (林晚 → ALICE), confirmed 2:1 after one retry.
    flip = [(0, "NARRATOR"), (1, "林晚"), (2, "NARRATOR"), (3, "ALICE"),
            (4, "NARRATOR"), (5, "林晚")]
    calls = _seq_urlopen(monkeypatch, [
        _mix_completion([
            {"index": 0, "action": "keep"},
            {"index": 1, "action": "keep"},
            {"index": 2, "action": "split", "parts": parts},
            {"index": 3, "action": "keep"},
        ]),                                    # stage 1: mix check (1 call)
        _map_completion(flip),                 # stage 2: first pass (index 3 flips)
        _map_completion(flip),                 # stage 2: retry 1 → ALICE wins 2:1
    ])

    mix_result = mix_check_file(_Handle(), str(src), _llm_cfg(), MixCheckConfig(),
                                SpeakerCheckConfig(context_window=1), GenerationConfig())
    speaker_result = check_file(_Handle(), str(checked), _llm_cfg(),
                                SpeakerCheckConfig(context_window=1), GenerationConfig())

    assert calls["n"] == 3  # 1 mix + 1 first pass + 1 retry
    assert src.read_bytes() == before  # the base file is untouched by BOTH stages
    assert not (tmp_path / "chx_checked_checked.json").exists()  # no double suffix
    assert mix_result["output_path"] == str(checked)
    assert speaker_result["output_path"] == str(checked)  # in-place, same path
    assert speaker_result["input_name"] == "chx_checked.json"
    assert speaker_result["changed"] == 1

    final = json.loads(checked.read_text("utf-8"))
    assert [e["speaker"] for e in final] == \
        ["NARRATOR", "林晚", "NARRATOR", "ALICE", "NARRATOR", "林晚"]  # order stable
    assert final[3] == {"speaker": "ALICE", "text": "「你终于来了。」", "instruct": ""}
    # The split parts keep their empty instruct; the kept entries keep theirs.
    assert [e["instruct"] for e in final] == ["tone", "tone", "", "", "", "tone"]
    # The split part text is verbatim — the speaker correction touched only `speaker`.
    assert [e["text"] for e in final] == [
        "夜色像潮水一样漫进街巷。", "「你终于来了。」", "林晚抬头：",
        "「你终于来了。」", "窗外的雨声一阵紧似一阵。", "「我等你很久了。」",
    ]


# --------------------------------------------------------------------------- #
# API resolver (_resolve_parsed_file) — sandboxed, as in test_paths.py
# --------------------------------------------------------------------------- #

@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect the project-root globals into ``tmp_path`` and start from a clean
    (pointer-less) root ``app.json``. The in-memory config cache is reset around
    each test so it re-resolves against the sandbox."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


@pytest.fixture
def set_pointer(sandbox):
    """Write the workspace pointer into the sandboxed root ``app.json``."""

    def _set(working_dir: str):
        core_config.set_workspace_pointer(working_dir)

    return _set


def _parsed_dir(set_pointer, sandbox, base_mtime, checked_mtime):
    """A workspace with ``ch.json`` + ``ch_checked.json`` at the given mtimes."""
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    base = d / "ch.json"
    base.write_text("[]", encoding="utf-8")
    checked = d / "ch_checked.json"
    checked.write_text("[]", encoding="utf-8")
    os.utime(base, (base_mtime, base_mtime))
    os.utime(checked, (checked_mtime, checked_mtime))
    return base, checked


def test_resolve_parsed_file_base_ignores_checked(sandbox, set_pointer):
    from backend.api.script import _resolve_parsed_file
    now = time.time()
    base, checked = _parsed_dir(set_pointer, sandbox, now, now + 10)  # checked is NEWER
    # Base mode (the mix check): the _checked artifact is ignored entirely, and a
    # ``_checked`` name is reduced to its base.
    assert _resolve_parsed_file("ch.json") == base
    assert _resolve_parsed_file("ch_checked.json") == base


def test_resolve_parsed_file_prefer_latest_fresh(sandbox, set_pointer):
    from backend.api.script import _resolve_parsed_file
    now = time.time()
    base, checked = _parsed_dir(set_pointer, sandbox, now, now + 10)
    # Fresh _checked (mtime >= base, the mix check's output) → the 角色匹配检查 updates
    # it in place.
    assert _resolve_parsed_file("ch.json", prefer_latest=True) == checked


def test_resolve_parsed_file_prefer_latest_stale(sandbox, set_pointer):
    from backend.api.script import _resolve_parsed_file
    now = time.time()
    base, checked = _parsed_dir(set_pointer, sandbox, now + 10, now)  # base is NEWER
    # Stale _checked (a re-parse left it behind) → fall back to the fresh base.
    assert _resolve_parsed_file("ch.json", prefer_latest=True) == base
