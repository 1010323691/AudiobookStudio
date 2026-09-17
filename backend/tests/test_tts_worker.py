"""Offline tests for the batch worker's pure planning logic (``tts-engine/tts_worker.py``).

The old ``run_bounded`` thread-pool scheduler is gone (replaced by native tensor-batch
planning), so these tests now pin the *pure* functions that decide how segments are padded
into GPU tensor batches and how long a hung batch may run before the watchdog kills the
process: ``plan_sub_batches`` (the greedy batcher), ``estimate_batch_vram`` (the VRAM budget,
including the L^2 attention peak the reference project omitted), and
``sub_batch_timeout_seconds`` (the device-scaled watchdog budget). They are pure stdlib (no
torch), so they run in the lean 3.14 backend suite via ``importlib`` — the worker module's
top-level imports are stdlib-only, so loading it never pulls in the ML stack.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

from backend.core.paths import PROJECT_ROOT

WORKER_PATH = PROJECT_ROOT / "tts-engine" / "tts_worker.py"


def _load_worker():
    """Load ``tts-engine/tts_worker.py`` as a fresh module (stdlib-only top level; no torch)."""
    spec = importlib.util.spec_from_file_location("tts_worker_under_test", WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# plan_sub_batches — the greedy tensor-batch planner
# --------------------------------------------------------------------------- #

def test_plan_sub_batches_manual_cap():
    tw = _load_worker()
    # a manual cap of 2 over 5 equal rows -> batches of 2, 2, 1
    assert tw.plan_sub_batches([10] * 5, max_batch=2, max_batch_chars=1000) == [[0, 1], [2, 3], [4]]


def test_plan_sub_batches_char_cap_limits_batch():
    tw = _load_worker()
    # 600-char rows under a 1000-char batch cap -> only one row fits per batch
    assert tw.plan_sub_batches([600] * 3, max_batch=4, max_batch_chars=1000) == [[0], [1], [2]]


def test_plan_sub_batches_length_ratio_splits():
    tw = _load_worker()
    # a row > 3x (LENGTH_RATIO) the batch's shortest row opens a new batch
    assert tw.plan_sub_batches([10, 11, 12, 100], max_batch=8, max_batch_chars=10000) == [[0, 1, 2], [3]]


def test_plan_sub_batches_overlong_row_is_solo():
    tw = _load_worker()
    # a row over the per-row char cap never mixes with the shorter rows already batched
    assert tw.plan_sub_batches([10, 20, 3000], max_batch=8, max_batch_chars=100000,
                               max_seq_chars=1000) == [[0, 1], [2]]


def test_plan_sub_batches_vram_ok_limits_batch():
    tw = _load_worker()
    # a vram_ok that only allows 1 row forces single-row batches
    r = tw.plan_sub_batches([10, 10, 10], max_batch=4, max_batch_chars=1000,
                            vram_ok=lambda tokens: len(tokens) <= 1, tokens=[5, 5, 5])
    assert r == [[0], [1], [2]]


def test_plan_sub_batches_never_skips_a_row():
    tw = _load_worker()
    # even a row the budget rejects still gets a solo batch (min size 1)
    assert tw.plan_sub_batches([10], max_batch=4, max_batch_chars=1000,
                               vram_ok=lambda tokens: False, tokens=[5]) == [[0]]


def test_plan_sub_batches_covers_every_row_exactly_once():
    tw = _load_worker()
    lengths = [7, 9, 14, 20, 33, 61, 120, 300, 700]
    batches = tw.plan_sub_batches(lengths, max_batch=3, max_batch_chars=400,
                                  max_seq_chars=2500, vram_ok=lambda t: sum(t) <= 400,
                                  tokens=lengths)
    assert sorted(i for b in batches for i in b) == list(range(len(lengths)))  # every row once


def test_plan_sub_batches_empty():
    tw = _load_worker()
    assert tw.plan_sub_batches([], max_batch=2, max_batch_chars=1000) == []


# --------------------------------------------------------------------------- #
# estimate_batch_vram — the VRAM budget (L^2 attention peak + KV cache)
# --------------------------------------------------------------------------- #

def test_estimate_batch_vram_has_both_terms():
    tw = _load_worker()
    heads, kvt = 32, 24000
    v = tw.estimate_batch_vram(1, heads, kvt, [100], 2048)
    assert v == 1 * heads * (100 + 2048) ** 2 * 4 + (100 + 2048) * kvt * 1.5


def test_estimate_batch_vram_scales_with_batch_size():
    tw = _load_worker()
    heads, kvt = 32, 24000
    v1 = tw.estimate_batch_vram(1, heads, kvt, [100], 2048)
    v8 = tw.estimate_batch_vram(8, heads, kvt, [100] * 8, 2048)
    assert v8 > v1  # a bigger batch costs more VRAM (the whole point of real GPU parallelism)


def test_estimate_batch_vram_unknown_heads_kv_only():
    tw = _load_worker()
    kvt = 24000
    assert tw.estimate_batch_vram(4, 0, kvt, [100] * 4, 2048) == \
        sum(100 + 2048 for _ in range(4)) * kvt * 1.5


# ---------------------------------------------------------------------------
# plan_row_tokens / _clone_input_overhead — honest per-row input lengths
# ---------------------------------------------------------------------------

def test_plan_row_tokens_prices_text_and_instruct_in_chars():
    tw = _load_worker()
    # char-based pricing (no tokenizer): (text + instruct) chars x CHAR_TOKENS_PER_CHAR + overhead
    toks = tw.plan_row_tokens(["abc", "abcdefghij"], ["xy", ""], 16)
    assert toks == [int(5 * tw.CHAR_TOKENS_PER_CHAR) + 16,
                    int(10 * tw.CHAR_TOKENS_PER_CHAR) + 16]
    assert toks[0] != toks[1]  # longer input is priced higher


def test_plan_row_tokens_empty():
    tw = _load_worker()
    assert tw.plan_row_tokens([], [], 16) == []


def test_clone_input_overhead_measured_from_prompt():
    tw = _load_worker()

    class _RC:
        shape = (81, 16)  # 81 reference frames x 16 codebooks

    class _Item:
        ref_code = _RC()
        ref_text = "你好，我是参考文本。"  # 10 chars

    overhead = tw._clone_input_overhead([_Item()], {})
    # structural markers + 81 ref frames + 10 ref-text chars priced at CHAR_TOKENS_PER_CHAR
    assert overhead == tw.ROW_STRUCTURAL_OVERHEAD + 81 + int(10 * tw.CHAR_TOKENS_PER_CHAR)


def test_clone_input_overhead_xvec_only_is_structural_only():
    tw = _load_worker()

    class _Item:
        ref_code = None
        ref_text = None

    # no ref frames, no ref text -> only the structural markers are owed
    assert tw._clone_input_overhead([_Item()], {}) == tw.ROW_STRUCTURAL_OVERHEAD


def test_clone_input_overhead_fallback_when_prompt_missing():
    tw = _load_worker()
    assert tw._clone_input_overhead(None, None) == tw.CLONE_FALLBACK_OVERHEAD


def test_clone_input_overhead_ref_text_from_voice_config():
    tw = _load_worker()

    class _Item:
        ref_code = None
        ref_text = None

    # the prompt item carries no ref_text -> price the voice config's transcript in chars
    assert tw._clone_input_overhead([_Item()], {"ref_text": "参考文本十"}) == \
        tw.ROW_STRUCTURAL_OVERHEAD + int(5 * tw.CHAR_TOKENS_PER_CHAR)


def test_clone_overhead_shrinks_the_admitted_batch():
    """The fix pinned end-to-end: under one VRAM budget, honest clone tokens admit FEWER rows
    than the old target-only tokens did (the L^2 term is sized by the rows' full input length).
    """
    tw = _load_worker()
    heads, kvt, max_new = 8, 1000, 256
    budget = 40_000_000  # a fixed VRAM budget (bytes)

    def vram_ok(tokens):
        return tw.estimate_batch_vram(len(tokens), heads, kvt, tokens, max_new) <= budget

    char_lens = [80] * 8
    custom_tokens = tw.plan_row_tokens(["t" * 80] * 8, [""] * 8, tw.ROW_STRUCTURAL_OVERHEAD)
    clone_tokens = tw.plan_row_tokens(["t" * 80] * 8, [""] * 8, tw.CLONE_FALLBACK_OVERHEAD)
    # both lists share the same (char-fallback) base; they differ only by the overhead delta
    delta = tw.CLONE_FALLBACK_OVERHEAD - tw.ROW_STRUCTURAL_OVERHEAD
    assert clone_tokens == [c + delta for c in custom_tokens]
    first_custom = tw.plan_sub_batches(char_lens, max_batch=32, max_batch_chars=100000,
                                       vram_ok=vram_ok, tokens=custom_tokens)[0]
    first_clone = tw.plan_sub_batches(char_lens, max_batch=32, max_batch_chars=100000,
                                      vram_ok=vram_ok, tokens=clone_tokens)[0]
    assert len(first_clone) < len(first_custom)  # the same budget admits fewer clone rows
    assert len(first_clone) == 4 and len(first_custom) == 8  # the exact shrink, pinned


# ---------------------------------------------------------------------------
# band_cap_for_chars — the length-class concurrency bands
# ---------------------------------------------------------------------------

def test_band_cap_pinned_values():
    tw = _load_worker()
    caps = {n: tw.band_cap_for_chars(n, 16) for n in
            (10, 64, 65, 256, 257, 512, 513, 1024, 1025, 2048, 2049, 99999)}
    assert caps == {10: 16, 64: 16, 65: 12, 256: 12, 257: 8, 512: 8,
                    513: 6, 1024: 6, 1025: 3, 2048: 3, 2049: 1, 99999: 1}


def test_band_cap_monotonic_nonincreasing():
    tw = _load_worker()
    caps = [tw.band_cap_for_chars(n, 32) for n in
            (10, 64, 100, 256, 300, 512, 600, 1024, 1100, 2048, 3000)]
    assert all(a >= b for a, b in zip(caps, caps[1:]))


def test_band_cap_never_exceeds_manual_cap():
    tw = _load_worker()
    for n in (1, 10, 100, 300, 3000):
        assert tw.band_cap_for_chars(n, 1) == 1
    for n in (10, 100, 300, 3000):
        assert tw.band_cap_for_chars(n, 3) <= 3


# ---------------------------------------------------------------------------
# VramGovernor — the measured VRAM / throughput feedback loop
# ---------------------------------------------------------------------------

GB = 2 ** 30


def _gov(cap=16, **kw):
    tw = _load_worker()
    return tw, tw.VramGovernor(cap, device="cuda",
                               total_vram=kw.pop("total_vram", 8 * GB), **kw)


def test_governor_shrinks_on_high_peak_frac():
    tw, gov = _gov()
    # the batch consumed 7.5 of the 8GB pool (94%) -> pressure -> halve the cap
    action = gov.observe_success(free_before=8 * GB, free_after=0.5 * GB, rows=8,
                                 chars=1600, elapsed=20, static_est=4 * GB)
    assert action == "shrink"
    assert gov.cap == 8
    # the measured working set (7.5GB) beat the static estimate (4GB) -> distrust it
    assert gov.vram_scale < 1.0


def test_governor_grows_back_after_pressure_clears():
    tw, gov = _gov()
    gov.observe_success(free_before=8 * GB, free_after=0.5 * GB, rows=8,
                        chars=1600, elapsed=20, static_est=4 * GB)  # -> cap 8
    assert gov.cap == 8
    # a later batch stays well under the pool with steady throughput -> grow toward the ceiling
    action = gov.observe_success(free_before=8 * GB, free_after=7 * GB, rows=4,
                                 chars=800, elapsed=10, static_est=2 * GB)
    assert action == "grow"
    assert gov.cap == 10  # 8 + max(1, 8 // 4)
    assert gov.cap <= gov.manual_cap


def test_governor_never_exceeds_manual_cap():
    tw, gov = _gov(4)
    for _ in range(6):
        gov.observe_success(free_before=8 * GB, free_after=7.9 * GB, rows=1,
                            chars=100, elapsed=1, static_est=1 * GB)
    assert gov.cap <= 4


def test_governor_floor_is_one():
    tw, gov = _gov(2)
    assert gov.observe_fault(2) == "fault"
    assert gov.cap == 1
    # pressure at the floor is a no-op (the cap is already minimal)
    assert gov.observe_success(free_before=8 * GB, free_after=0.1 * GB, rows=1,
                               chars=10, elapsed=1, static_est=0) is None
    assert gov.cap == 1


def test_governor_fault_halves_to_retry_size():
    tw, gov = _gov()
    assert gov.observe_fault(6) == "fault"
    assert gov.cap == 3  # no later batch may re-propose a 6-row size
    assert gov.observe_fault(2) == "fault"
    assert gov.cap == 1


def test_governor_row_cap_is_band_limited():
    tw, gov = _gov()
    assert gov.row_cap_for(10) == 16     # short class -> full cap
    assert gov.row_cap_for(3000) == 1    # extreme class -> solo, whatever the adaptive cap is
    assert gov.observe_fault(1) == "fault"
    assert gov.cap == 1
    assert gov.row_cap_for(10) == 1      # the shrunk cap also binds the short class


def test_governor_off_cuda_is_inert():
    tw, gov = _gov()
    gov.device = "cpu"
    assert gov.observe_success(free_before=8 * GB, free_after=0, rows=4,
                               chars=1000, elapsed=5, static_est=1 * GB) is None
    assert gov.cap == 16


def test_governor_calibration_learns_estimate_bias():
    tw, gov = _gov()
    # measured working set (2GB) far below the static estimate (4GB) -> the estimate is
    # conservative: trust it more (later batches may be admitted bigger)
    gov.observe_success(free_before=8 * GB, free_after=6 * GB, rows=8,
                        chars=1600, elapsed=20, static_est=4 * GB)
    assert gov.vram_scale > 1.0

    _tw2, gov2 = _gov()
    # measured (3GB) above the static (2GB) -> the guess was beaten: distrust + pressure
    action = gov2.observe_success(free_before=8 * GB, free_after=5 * GB, rows=8,
                                  chars=1600, elapsed=20, static_est=2 * GB)
    assert gov2.vram_scale < 1.0
    assert action == "shrink"
    assert gov2.cap < 16


# ---------------------------------------------------------------------------
# plan_sub_batches — the length bands + tightened ratio as planner constraints
# ---------------------------------------------------------------------------

def test_plan_sub_batches_band_cap_limits_batch_size():
    tw = _load_worker()
    # 20 medium rows: the length band (8 of 16) closes each batch before the row cap would
    batches = tw.plan_sub_batches([300] * 20, max_batch=16, max_batch_chars=100000,
                                  band_cap=lambda c: tw.band_cap_for_chars(c, 16))
    assert [len(b) for b in batches] == [8, 8, 4]


def test_plan_sub_batches_band_keeps_short_rows_out_of_long_batches():
    tw = _load_worker()
    lens = [10] * 12 + [300] * 4
    batches = tw.plan_sub_batches(lens, max_batch=16, max_batch_chars=100000,
                                  band_cap=lambda c: tw.band_cap_for_chars(c, 16))
    assert batches[0] == list(range(12))       # the short rows fill their own batches
    assert batches[1] == list(range(12, 16))   # the long rows batch together


def test_plan_sub_batches_ratio_splits_even_two_row_batches():
    tw = _load_worker()
    # with min_ratio_size=2 a 10x spread splits even a two-row batch (no padding waste)
    assert tw.plan_sub_batches([10, 100], max_batch=8, max_batch_chars=10000) == [[0], [1]]


def test_plan_sub_batches_ratio_three_boundaries():
    tw = _load_worker()
    # exactly 3x (the tightened LENGTH_RATIO) still shares a batch; just over 3x splits —
    # the decode cap scales with the batch's longest row, so a 5x spread (the old ratio)
    # made a 2-char line run a 10-char line's full cap
    assert tw.plan_sub_batches([2, 6], max_batch=8, max_batch_chars=10000) == [[0, 1]]
    assert tw.plan_sub_batches([2, 7], max_batch=8, max_batch_chars=10000) == [[0], [1]]


# ---------------------------------------------------------------------------
# plan_next_sub_batch — the lazy planning round the run loop drives
# ---------------------------------------------------------------------------

def _row(chars, **kw):
    base = {"chars": chars, "text": "字" * chars, "instruct": "", "vd": {}}
    base.update(kw)
    return base


def test_lazy_rounds_cover_every_row_and_follow_bands():
    tw = _load_worker()
    # 40 short rows (10 chars) + 20 medium rows (300 chars), a healthy GPU throughout
    rows = [_row(c) for c in [10] * 40 + [300] * 20]
    gov = tw.VramGovernor(16, device="cuda", total_vram=8 * GB)
    remaining = rows
    sizes = []
    while remaining:
        rows_b, remaining = tw.plan_next_sub_batch(
            remaining, vtype="custom", overhead=16, params=None, budget=None,
            gov=gov, max_batch=16, max_batch_chars=12000)
        sizes.append(len(rows_b))
        # healthy batch: the pool barely moves, steady throughput -> no adjustment
        gov.observe_success(free_before=8 * GB, free_after=7.5 * GB, rows=len(rows_b),
                            chars=sum(r["chars"] for r in rows_b), elapsed=10, static_est=0)
    assert sum(sizes) == 60  # every row scheduled exactly once
    # short rows ran at the full manual cap; medium rows were band-limited (8 of 16)
    assert sizes == [16, 16, 8, 8, 8, 4]


def test_lazy_rounds_shrink_and_replan_under_pressure():
    tw = _load_worker()
    rows = [_row(10) for _ in range(64)]
    gov = tw.VramGovernor(16, device="cuda", total_vram=8 * GB)
    remaining = rows
    sizes = []
    for i in range(10):
        if not remaining:
            break
        rows_b, remaining = tw.plan_next_sub_batch(
            remaining, vtype="custom", overhead=16, params=None, budget=None,
            gov=gov, max_batch=16, max_batch_chars=12000)
        sizes.append(len(rows_b))
        if i == 0:
            # the first round is healthy (the full cap is kept)
            gov.observe_success(free_before=8 * GB, free_after=7.5 * GB, rows=len(rows_b),
                                chars=sum(r["chars"] for r in rows_b), elapsed=10, static_est=0)
        else:
            # the pool runs dry -> the cap halves and the remainder is re-planned smaller
            gov.observe_success(free_before=8 * GB, free_after=0.3 * GB, rows=len(rows_b),
                                chars=sum(r["chars"] for r in rows_b), elapsed=10, static_est=0)
    # the first round planned at the old cap; every re-plan used the halved cap
    assert sizes[:5] == [16, 16, 8, 4, 2]
    assert all(s == 1 for s in sizes[5:])


# ---------------------------------------------------------------------------
# --disabled-checks — the settings-page planner switches
# ---------------------------------------------------------------------------

def test_parse_disabled_checks_empty_and_valid():
    tw = _load_worker()
    assert tw.parse_disabled_checks("") == frozenset()
    assert tw.parse_disabled_checks(None) == frozenset()
    assert tw.parse_disabled_checks("vram") == frozenset({"vram"})
    # whitespace-tolerant, order-insensitive, duplicates collapse
    assert tw.parse_disabled_checks(" vram , length_bands ,vram") == \
        frozenset({"vram", "length_bands"})
    assert tw.parse_disabled_checks("length_bands,batch_chars,seq_chars,length_ratio,vram") \
        == frozenset(tw.PLANNER_CHECK_NAMES)


def test_parse_disabled_checks_unknown_name_raises():
    tw = _load_worker()
    with pytest.raises(ValueError) as exc:
        tw.parse_disabled_checks("vram,bogus")
    # the error names the offender AND the valid set (the caller surfaces it as
    # TTS_WORKER_ERROR + exit 2 — a typo must never silently close a gate)
    assert "bogus" in str(exc.value)
    assert "length_bands" in str(exc.value)


def test_plan_next_sub_batch_disabled_vram_user_scenario():
    tw = _load_worker()
    # The real 2026-09-17 stress case: 32 x 50-char rows, manual cap 32. The static
    # VRAM estimate prices every row at the worst-case 2048-frame decode (per row =
    # 8 heads x 2124^2 x 4 + 2124 x 1000 x 1.5 = 147,550,032 B), so a 400 MB budget
    # fits exactly 2 rows; with the estimate closed the manual cap governs -> all 32
    # in one batch. Bands (50 chars -> full cap), ratio (1:1) and char caps (1600 <=
    # 12000) are non-binding in both arms, so the VRAM gate is the sole constraint.
    rows = [_row(50) for _ in range(32)]
    params = {"heads": 8, "kv_per_token": 1000}
    budget = 400_000_000
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=params, budget=budget,
        gov=tw.VramGovernor(32, device="cuda", total_vram=8 * GB),
        max_batch=32, max_batch_chars=12000)
    assert len(rows_b) == 2 and len(remaining) == 30
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=params, budget=budget,
        gov=tw.VramGovernor(32, device="cuda", total_vram=8 * GB),
        max_batch=32, max_batch_chars=12000, disabled=frozenset({"vram"}))
    assert len(rows_b) == 32 and remaining == []


def test_plan_next_sub_batch_disabled_bands():
    tw = _load_worker()
    # 300-char rows: the length band (8 of 16) binds while on; closed, the manual
    # cap (16) governs — 16 x 300 = 4800 chars still fits the batch char cap.
    rows = [_row(300) for _ in range(20)]
    rows_b, _rem = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(16, device="cuda", total_vram=8 * GB),
        max_batch=16, max_batch_chars=120000)
    assert len(rows_b) == 8
    rows_b, _rem = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(16, device="cuda", total_vram=8 * GB),
        max_batch=16, max_batch_chars=120000, disabled=frozenset({"length_bands"}))
    assert len(rows_b) == 16


def test_plan_next_sub_batch_disabled_batch_chars():
    tw = _load_worker()
    rows = [_row(50) for _ in range(30)]
    rows_b, _rem = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(32, device="cuda", total_vram=8 * GB),
        max_batch=32, max_batch_chars=60)
    assert len(rows_b) == 1  # 2 x 50 = 100 > 60 -> one row per batch
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(32, device="cuda", total_vram=8 * GB),
        max_batch=32, max_batch_chars=60, disabled=frozenset({"batch_chars"}))
    # 30 x 50 = 1500 chars; the bands are the full cap for 50-char rows, so the
    # manual cap (32) admits every row in one batch
    assert len(rows_b) == 30 and remaining == []


def test_plan_next_sub_batch_disabled_seq_chars_interaction():
    tw = _load_worker()
    rows = [_row(30), _row(30), _row(3000)]
    # Only seq_chars closed: the 3000-char row is STILL solo — the >2048 band forces
    # size 1 and the 100x ratio splits it from its 30-char neighbours. The checks
    # interact: closing one does not guarantee the expected merge while a neighbour
    # still forces splits (pinned so this is never misread as a regression).
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(8, device="cuda", total_vram=8 * GB),
        max_batch=8, max_batch_chars=120000, disabled=frozenset({"seq_chars"}))
    assert len(rows_b) == 2 and len(remaining) == 1
    # All three closed: nothing splits the rows -> one batch of 3.
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(8, device="cuda", total_vram=8 * GB),
        max_batch=8, max_batch_chars=120000,
        disabled=frozenset({"length_bands", "seq_chars", "length_ratio"}))
    assert len(rows_b) == 3 and remaining == []


def test_plan_next_sub_batch_disabled_ratio():
    tw = _load_worker()
    rows = [_row(10), _row(100)]
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(8, device="cuda", total_vram=8 * GB),
        max_batch=8, max_batch_chars=120000)
    assert len(rows_b) == 1 and len(remaining) == 1  # 10x spread > 3 -> split
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=None, budget=None,
        gov=tw.VramGovernor(8, device="cuda", total_vram=8 * GB),
        max_batch=8, max_batch_chars=120000, disabled=frozenset({"length_ratio"}))
    assert len(rows_b) == 2 and remaining == []


def test_plan_next_sub_batch_disabled_all_governed_by_manual_cap():
    tw = _load_worker()
    rows = [_row(50) for _ in range(32)]
    params = {"heads": 8, "kv_per_token": 1000}
    budget = 400_000_000
    rows_b, remaining = tw.plan_next_sub_batch(
        rows, vtype="custom", overhead=16, params=params, budget=budget,
        gov=tw.VramGovernor(32, device="cuda", total_vram=8 * GB),
        max_batch=32, max_batch_chars=12000,
        disabled=frozenset(tw.PLANNER_CHECK_NAMES))
    # The VRAM estimate alone would cap this at 2 rows; every static check closed ->
    # the manual cap (32) is the only constraint left (the VramGovernor's measured
    # cap starts at the manual cap, so it agrees).
    assert len(rows_b) == 32 and remaining == []


# --------------------------------------------------------------------------- #
# order_speaker_groups — per-character grouping, most lines first
# --------------------------------------------------------------------------- #

def test_order_speaker_groups_most_lines_first():
    tw = _load_worker()
    classified = {
        ("clone", "龙套"): [_row(5)] * 2,
        ("clone", "主角"): [_row(8)] * 5,
        ("custom", "旁白"): [_row(30)] * 9,
    }
    groups = tw.order_speaker_groups(classified)
    # the most-line character (旁白, 9 lines) runs first, then 5, then 2
    assert [key for key, _rows in groups] == [
        ("custom", "旁白"), ("clone", "主角"), ("clone", "龙套")]


def test_order_speaker_groups_ties_keep_first_seen():
    tw = _load_worker()
    classified = {
        ("clone", "甲"): [_row(4)],
        ("clone", "乙"): [_row(6), _row(2)],
        ("clone", "丙"): [_row(3), _row(7)],
    }
    groups = tw.order_speaker_groups(classified)
    # equal counts -> first-seen (insertion) order, deterministic for a given input
    assert [key for key, _rows in groups] == [
        ("clone", "乙"), ("clone", "丙"), ("clone", "甲")]


def test_order_speaker_groups_rows_length_ascending():
    tw = _load_worker()
    classified = {("custom", "旁白"): [_row(40), _row(3), _row(200), _row(12)]}
    _key, rows = tw.order_speaker_groups(classified)[0]
    # rows come back length-ascending so a sub-batch drawn from them stays homogeneous
    assert [r["chars"] for r in rows] == [3, 12, 40, 200]


def test_order_speaker_groups_never_mix_characters():
    tw = _load_worker()
    classified = {
        ("custom", "A"): [_row(5), _row(9)],
        ("custom", "B"): [_row(50)],
        ("clone", "C"): [_row(2), _row(3), _row(4)],
    }
    groups = tw.order_speaker_groups(classified)
    # each group is exactly one character's rows — all of them, length-ascending,
    # no cross-character pooling
    for key, rows in groups:
        assert [r["chars"] for r in rows] == \
            sorted(r["chars"] for r in classified[key])
    assert {key for key, _rows in groups} == set(classified)


def test_order_speaker_groups_empty():
    tw = _load_worker()
    assert tw.order_speaker_groups({}) == []


# --------------------------------------------------------------------------- #
# sub_batch_timeout_seconds — the device-scaled watchdog budget
# --------------------------------------------------------------------------- #

def test_timeout_cuda_floor_cap_and_scaling():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0) == 30                 # floor
    assert tw.sub_batch_timeout_seconds("cuda", 1000) == int(1000 / 15)  # scales: chars / 15
    assert tw.sub_batch_timeout_seconds("cuda", 60000) == 3600           # cap


def test_timeout_cpu_is_looser():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cpu", 0) == 600            # floor (much looser)
    assert tw.sub_batch_timeout_seconds("cpu", 30000) == 10800      # cap
    assert tw.sub_batch_timeout_seconds("cpu", 1000) == 600 + 4 * 1000


def test_timeout_gpu_formula_for_mps():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("mps", 0) == 30  # any non-cpu device uses the GPU budget


def test_timeout_clone_formula_floor_scale_cap():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0, vtype="clone") == 30                 # floor
    assert tw.sub_batch_timeout_seconds("cuda", 1000, vtype="clone") == int(1000 / 15)  # chars / 15
    assert tw.sub_batch_timeout_seconds("cuda", 60000, vtype="clone") == 3600           # cap


def test_timeout_gpu_budget_is_uniform_across_vtypes():
    tw = _load_worker()
    # The GPU budget no longer differentiates by voice type: the 15 chars/sec rate was measured
    # on a clone batch, and short-row batches decode at ~the same per-char rate whichever type
    # renders them — so clone / custom / design all share one budget (chars/15, floored & capped).
    for chars in (0, 100, 500, 2000, 10000, 60000):
        assert tw.sub_batch_timeout_seconds("cuda", chars, vtype="clone") == \
            tw.sub_batch_timeout_seconds("cuda", chars, vtype="design") == \
            tw.sub_batch_timeout_seconds("cuda", chars)  # default vtype="custom"


def test_timeout_budget_tracks_measured_decode_rate():
    tw = _load_worker()
    # The budget tracks the measured GPU decode rate (~15 chars/sec): a 2240-char batch (the
    # shape of the 2026-09-17 run that showed an over-generous 1688s budget) now gets ~149s,
    # so a hung batch is caught in minutes rather than ~half an hour.
    assert tw.sub_batch_timeout_seconds("cuda", 2240, vtype="clone") == int(2240 / 15)


# --------------------------------------------------------------------------- #
# run_with_watchdog — the non-timeout paths
# (the timeout path calls os._exit(124), which would kill the test runner, so it is not exercised
# in-process; these pin that a fast ``fn`` returns its result and a fault is re-raised)
# --------------------------------------------------------------------------- #

def test_run_with_watchdog_returns_result():
    tw = _load_worker()
    assert tw.run_with_watchdog(lambda: 42, 5.0, "custom#1", []) == 42


def test_run_with_watchdog_reraises_fault():
    tw = _load_worker()

    def boom():
        raise ValueError("simulated OOM")

    with pytest.raises(ValueError):
        tw.run_with_watchdog(boom, 5.0, "custom#1", [])


# --------------------------------------------------------------------------- #
# Two-stage merge planning (pure)
# --------------------------------------------------------------------------- #

def test_plan_merge_batches_single():
    tw = _load_worker()
    assert tw.plan_merge_batches(0, 100) == []
    assert tw.plan_merge_batches(1, 100) == [(0, 1)]
    assert tw.plan_merge_batches(80, 100) == [(0, 80)]
    assert tw.plan_merge_batches(100, 100) == [(0, 100)]


def test_plan_merge_batches_coverage():
    tw = _load_worker()
    plan = tw.plan_merge_batches(250, 100)
    assert plan == [(0, 100), (100, 200), (200, 250)]
    flat = [i for start, end in plan for i in range(start, end)]
    assert flat == list(range(250))  # full coverage, no overlap, in order


def test_plan_merge_batches_clamps_size():
    tw = _load_worker()
    # a bogus size degrades to one-segment parts, never to an empty range
    assert tw.plan_merge_batches(3, 0) == [(0, 1), (1, 2), (2, 3)]
    assert tw.plan_merge_batches(2, -5) == [(0, 1), (1, 2)]


def test_normalize_pause_ms():
    tw = _load_worker()
    assert tw.normalize_pause_ms(None) is None
    assert tw.normalize_pause_ms(900) == 900
    assert tw.normalize_pause_ms("900") == 900
    assert tw.normalize_pause_ms(900.6) == 900
    assert tw.normalize_pause_ms("abc") is None
    assert tw.normalize_pause_ms({}) is None
    assert tw.normalize_pause_ms(-5) == 0


def test_boundary_gap_override_wins():
    tw = _load_worker()
    assert tw.boundary_gap_ms(900, "A", "B", 500, 250) == 900
    assert tw.boundary_gap_ms(0, "A", "A", 500, 250) == 0  # an explicit 0 is still an override


def test_boundary_gap_speaker_rule():
    tw = _load_worker()
    assert tw.boundary_gap_ms(None, "A", "A", 500, 250) == 250
    assert tw.boundary_gap_ms(None, "A", "B", 500, 250) == 500
    assert tw.boundary_gap_ms("bad", "A", "A", 500, 250) == 250  # dirty -> speaker rule


def test_boundary_gap_matches_single_pass():
    """The two-stage merge must insert exactly the gaps a single-pass merge would.
    The single-pass rule for the gap after segment i is: its pause_after (if any),
    else the same/different-speaker default against segment i+1. With batch size 2
    the part boundary falls exactly on the override-bearing gap below."""
    tw = _load_worker()
    segs = [("A", None), ("A", 900), ("B", None), ("A", None)]
    # single-pass gaps for the three inter-segment boundaries (pause_ms=500, same_ms=250):
    single_pass = [250, 900, 500]  # 0->1 same speaker; 1->2 override; 2->3 speaker change

    def single_pass_gap(i):
        # the reference rule, inlined (what one combine pass over the whole sequence does)
        override = segs[i][1]
        if override is not None:
            return int(override)
        return 250 if segs[i + 1][0] == segs[i][0] else 500

    assert [single_pass_gap(i) for i in range(3)] == single_pass

    # two-stage: the part boundary (after segment 1, the last of part [0,1]) is
    # boundary_gap_ms(last_pause_after, last_speaker, first_speaker, ...) of the two parts
    assert tw.boundary_gap_ms(segs[1][1], segs[1][0], segs[2][0], 500, 250) == single_pass[1]
    # ...and the intra-part boundaries are the same rule the single pass applies there
    assert tw.boundary_gap_ms(segs[0][1], segs[0][0], segs[1][0], 500, 250) == single_pass[0]
    assert tw.boundary_gap_ms(segs[2][1], segs[2][0], segs[3][0], 500, 250) == single_pass[2]


def test_boundary_gap_across_skipped_segments():
    """When segments are skipped (missing files), the single-pass gap falls between
    the last valid segment before and the first valid segment after the skip — the
    part boundary must use exactly those two (plus any override on the earlier one)."""
    tw = _load_worker()
    assert tw.boundary_gap_ms(None, "A", "C", 500, 250) == 500  # A -> C across the skip
    assert tw.boundary_gap_ms(700, "A", "C", 500, 250) == 700    # override still wins


def test_merge_frac_bands_contiguous():
    tw = _load_worker()
    assert tw.merge_stage1_frac(0, 3000) == pytest.approx(0.05)
    assert tw.merge_stage1_frac(3000, 3000) == pytest.approx(0.80)
    assert tw.merge_stage2_frac(0, 30) == pytest.approx(0.80)
    assert tw.merge_stage2_frac(30, 30) == pytest.approx(0.95)
    assert tw.merge_encode_frac(0.0) == pytest.approx(0.95)
    assert tw.merge_encode_frac(1.0) == pytest.approx(1.0)
    assert tw.merge_encode_frac(5.0) == pytest.approx(1.0)  # clamped at the top
    seq = ([tw.merge_stage1_frac(i, 100) for i in range(101)]
           + [tw.merge_stage2_frac(i, 3) for i in range(1, 4)]
           + [tw.merge_encode_frac(f) for f in (0.0, 0.5, 1.0)])
    assert all(a <= b for a, b in zip(seq, seq[1:]))  # one monotone run, 0.05 -> 1.0


# --------------------------------------------------------------------------- #
# design-batch — the 角色配音·克隆 stage (shared planning / generate / save path)
# --------------------------------------------------------------------------- #

def test_design_rows_run_solo_by_default_but_share_with_force_rows_cap():
    tw = _load_worker()
    rows = [_row(10) for _ in range(4)]  # four identical short design rows
    gov = tw.VramGovernor(8, device="cuda", total_vram=8 * GB)
    # Default (batch mode's design rows = book segments): one row per sub-batch...
    remaining = rows
    sizes = []
    while remaining:
        rows_b, remaining = tw.plan_next_sub_batch(
            remaining, vtype="design", overhead=16, params=None, budget=None,
            gov=gov, max_batch=8, max_batch_chars=12000)
        sizes.append(len(rows_b))
    assert sizes == [1, 1, 1, 1]
    # ...design-batch forces the governor's cap: the candidates share a tensor sub-batch.
    remaining = rows
    sizes = []
    while remaining:
        rows_b, remaining = tw.plan_next_sub_batch(
            remaining, vtype="design", overhead=16, params=None, budget=None,
            gov=gov, max_batch=8, max_batch_chars=12000, force_rows_cap=gov.cap)
        sizes.append(len(rows_b))
    assert sizes == [4]


def test_generate_rows_design_uses_per_row_instruct_and_do_sample_switch():
    tw = _load_worker()

    class _Model:
        def __init__(self):
            self.calls = []

        def generate_voice_design(self, **kw):
            self.calls.append(kw)
            return [[1.0] * 8] * len(kw["text"]), 24000

    model = _Model()
    args = SimpleNamespace(language="chinese")
    rows = [
        {"index": 0, "text": "t0", "instruct": "", "vd": {"description": "deep voice"}},
        {"index": 1, "text": "t1", "instruct": "", "vd": {"description": "bright voice"}},
    ]
    # Default (batch mode): no do_sample override — its design behaviour is unchanged.
    results = tw._generate_rows(model, "design", rows, args, {}, None)
    assert results == [(True, ([1.0] * 8, 24000))] * 2
    kw = model.calls[0]
    # Each row keeps its own description (a per-row instruct list, matching the
    # token counting / VRAM estimate).
    assert kw["instruct"] == ["deep voice", "bright voice"]
    assert kw["text"] == ["t0", "t1"]
    assert "do_sample" not in kw
    # design-batch: sampling forced on (identical-input candidates stay distinct even on
    # a checkpoint whose generate_config disables sampling).
    tw._generate_rows(model, "design", rows, args, {}, None, force_do_sample=True)
    assert model.calls[1].get("do_sample") is True


class TestLengthProportionalDecodeCap:
    """In a multi-row tensor batch the model does not stop individual rows at EOS
    (model-side behaviour), so the decode cap scales with the sub-batch's longest row
    instead of always being MAX_NEW_TOKENS — a short-row batch must no longer be able
    to run the full 2048-step cap (the 2026-09-17 one-click-synthesis hang: 31 rows of
    2-6 chars ran the full cap and blew the sub-batch watchdog budget)."""

    def test_short_rows_get_the_floor(self):
        tw = _load_worker()
        assert tw.max_new_tokens_for_chars(0) == tw.FRAME_CAP_FLOOR
        for chars in (1, 2, 6, 21):
            assert tw.max_new_tokens_for_chars(chars) == tw.FRAME_CAP_FLOOR

    def test_proportional_mid_range(self):
        tw = _load_worker()
        per_char = tw.FRAME_CAP_PER_CHAR
        for chars in (30, 50, 100, 200):
            assert tw.max_new_tokens_for_chars(chars) == chars * per_char

    def test_long_rows_keep_the_full_cap(self):
        tw = _load_worker()
        # the floor of proportionality: 341*6 = 2046 < 2048; 342*6 = 2052 clamps to 2048
        assert tw.max_new_tokens_for_chars(341) == 341 * tw.FRAME_CAP_PER_CHAR
        for chars in (342, 1000, 2500):
            assert tw.max_new_tokens_for_chars(chars) == tw.MAX_NEW_TOKENS

    def test_monotonic(self):
        tw = _load_worker()
        caps = [tw.max_new_tokens_for_chars(c) for c in range(0, 400)]
        assert caps == sorted(caps)

    def test_generate_rows_passes_the_proportional_cap(self):
        tw = _load_worker()

        class _Model:
            def __init__(self):
                self.calls = []

            def generate_voice_clone(self, **kw):
                self.calls.append(kw)
                return [[1.0] * 8] * len(kw["text"]), 24000

        model = _Model()
        args = SimpleNamespace(language="chinese")
        # The incident shape: a few ultra-short rows in one sub-batch.
        rows = [
            {"index": 0, "speaker": "NARRATOR", "text": "啪！", "instruct": "", "vd": {}},
            {"index": 1, "speaker": "NARRATOR", "text": "侍女抱了拳。", "instruct": "", "vd": {}},
        ]
        tw._generate_rows(model, "clone", rows, args, {"NARRATOR": "prompt"}, None)
        # 6-char longest row -> the floor, NOT the full 2048 cap (the hang signature)
        assert model.calls[0]["max_new_tokens"] == tw.max_new_tokens_for_chars(6)
        assert model.calls[0]["max_new_tokens"] < tw.MAX_NEW_TOKENS

        # A ~105-char row -> proportional, still under the full cap.
        long_rows = [{
            "index": 2, "speaker": "NARRATOR",
            "text": "风从街口穿过来，带着一股淡淡的柴火气味。" * 5,
            "instruct": "", "vd": {},
        }]
        tw._generate_rows(model, "clone", long_rows, args, {"NARRATOR": "prompt"}, None)
        cap = model.calls[1]["max_new_tokens"]
        assert cap == tw.max_new_tokens_for_chars(len(long_rows[0]["text"]))
        assert tw.FRAME_CAP_FLOOR < cap < tw.MAX_NEW_TOKENS


def test_save_and_report_design_protocol_and_fault_tolerance(tmp_path, monkeypatch):
    tw = _load_worker()

    # The save path imports numpy/soundfile lazily — stub them (the lean 3.14 env has
    # neither; the real .venv-tts has both).
    class _Nd:  # the fake ndarray type: no real value is an instance of it
        pass

    class _Arr:
        def __init__(self, x):
            self.x = x
            self.size = len(x)
            self.ndim = 1

        def __len__(self):
            return self.size

    def _np_array(x):
        return x if isinstance(x, _Arr) else _Arr(x)  # idempotent, like the real one

    fake_np = types.SimpleNamespace(array=_np_array, ndarray=_Nd)
    written = []

    class _FakeSF:
        @staticmethod
        def write(path, data, sr):
            if path.endswith("bad.wav"):
                raise RuntimeError("boom")
            written.append((path, len(data), sr))
            with open(path, "wb") as f:
                f.write(b"RIFF" + bytes(100))

    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    monkeypatch.setitem(sys.modules, "soundfile", _FakeSF)

    out = tmp_path / "dv"
    rows = [
        {"index": 10, "out": str(out / "ok.wav")},
        {"index": 11, "out": str(out / "empty.wav")},
        {"index": 12, "out": str(out / "bad.wav")},
        {"index": 13, "out": str(out / "lost.wav")},
    ]
    results = [
        (True, ([1.0] * 8, 24000)),   # ok: WAV straight to the row's out path
        (True, ([], 24000)),          # empty audio: recorded error, not an abort
        (True, ([1.0] * 8, 24000)),   # save fault -> recorded error, run continues
        (False, "生成失败"),            # generation fault: reported as-is
    ]
    reports = []
    tw._save_and_report_design(rows, results, str(out), 4,
                               lambda i, ok, p: reports.append((i, ok, p)), 12345)
    assert reports == [
        (10, True, (12345, os.path.abspath(str(out / "ok.wav")))),
        (11, False, "模型返回空音频"),
        (12, False, "boom"),
        (13, False, "生成失败"),
    ]
    # Exactly the ok row produced a file (no MP3 encode in this mode).
    assert written and written[0][0] == os.path.abspath(str(out / "ok.wav"))
    assert (out / "ok.wav").exists()
    assert not (out / "empty.wav").exists() and not (out / "bad.wav").exists()


# --------------------------------------------------------------------------- #
# _row_output_paths — per-row chapter save location (pooled multi-file runs)
# --------------------------------------------------------------------------- #

def test_row_output_paths_fallback_and_override():
    tw = _load_worker()
    fb = r"C:\ws\05_audio_chunk\pool_fallback"
    # Legacy rows (no out_dir / file_index) fall back to the whole-batch dir + segment
    # index — byte-identical to the pre-pooling formula.
    assert tw._row_output_paths({"index": 3}, fb, 4) == (
        os.path.join(fb, "0004.mp3"), os.path.join(fb, "0004.wav"))
    # Pooled rows carry their own chapter dir + local line number (file number =
    # file_index + 1, zero-padded to the pool width). The segment-table index is
    # never used for the file number once file_index is present.
    assert tw._row_output_paths({"index": 12, "out_dir": r"C:\ws\05_audio_chunk\t",
                                 "file_index": 37}, fb, 5) == (
        r"C:\ws\05_audio_chunk\t\00038.mp3", r"C:\ws\05_audio_chunk\t\00038.wav")
    # zfill pads but never truncates: a number wider than `width` keeps its digits.
    assert tw._row_output_paths({"index": 0, "file_index": 99999}, fb, 4) == (
        os.path.join(fb, "100000.mp3"), os.path.join(fb, "100000.wav"))
    # Empty-string out_dir also falls back (truthiness, not presence).
    assert tw._row_output_paths({"index": 0, "out_dir": ""}, fb, 4) == (
        os.path.join(fb, "0001.mp3"), os.path.join(fb, "0001.wav"))


def test_save_and_report_pool_rows_land_in_own_packages(tmp_path, monkeypatch):
    tw = _load_worker()

    # The save path imports numpy/soundfile lazily — stub them (the lean 3.14 env has
    # neither; the real .venv-tts has both), and stub the pydub-based WAV->MP3 encode.
    class _Nd:  # the fake ndarray type: no real value is an instance of it
        pass

    class _Arr:
        def __init__(self, x):
            self.x = x
            self.size = len(x)
            self.ndim = 1

        def __len__(self):
            return self.size

    def _np_array(x):
        return x if isinstance(x, _Arr) else _Arr(x)  # idempotent, like the real one

    fake_np = types.SimpleNamespace(array=_np_array, ndarray=_Nd)
    encoded = []

    class _FakeSF:
        @staticmethod
        def write(path, data, sr):
            with open(path, "wb") as f:
                f.write(b"RIFF" + bytes(100))

    def _fake_wav_to_mp3(wav_path, mp3_path):
        assert os.path.exists(wav_path)
        with open(mp3_path, "wb") as f:
            f.write(b"ID3" + bytes(1200))  # >= 1024B, so the encode counts as good
        encoded.append(mp3_path)
        return True

    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    monkeypatch.setitem(sys.modules, "soundfile", _FakeSF)
    monkeypatch.setattr(tw, "_wav_to_mp3", _fake_wav_to_mp3)

    pkg_a = str(tmp_path / "s")  # chapter package s
    pkg_b = str(tmp_path / "t")  # chapter package t
    fallback = str(tmp_path / "fallback")
    rows = [
        {"index": 0, "out_dir": pkg_a, "file_index": 3},   # pooled -> s/0004.mp3
        {"index": 1, "out_dir": pkg_b, "file_index": 0},   # pooled -> t/0001.mp3
        {"index": 2, "file_index": 7},                     # legacy fallback -> fallback/0008.mp3
    ]
    results = [
        (True, ([1.0] * 8, 24000)),  # ok
        (True, ([], 24000)),         # empty audio -> recorded error, no file touched
        (True, ([1.0] * 8, 24000)),  # ok, falls back to the batch out_dir
    ]
    reports = []
    tw._save_and_report(rows, results, fallback, 4,
                        lambda i, ok, p: reports.append((i, ok, p)))
    # Reports always carry the segment-table index (pool-global in a pooled run),
    # never the file_index.
    assert reports == [
        (0, True, os.path.join(pkg_a, "0004.mp3")),
        (1, False, "模型返回空音频"),
        (2, True, os.path.join(fallback, "0008.mp3")),
    ]
    # Files land in their own chapter packages; the file number is file_index + 1.
    assert sorted(encoded) == sorted([os.path.join(pkg_a, "0004.mp3"),
                                      os.path.join(fallback, "0008.mp3")])
    assert (tmp_path / "s" / "0004.mp3").exists()
    assert not (tmp_path / "t" / "0001.mp3").exists()      # empty-audio row wrote nothing
    assert not (tmp_path / "s" / "0001.mp3").exists()      # index 0 was NOT used as a number
    # No stray WAV left behind (the successful encode removes it).
    assert not list((tmp_path / "s").glob("*.wav")) and not list((tmp_path / "fallback").glob("*.wav"))


# --------------------------------------------------------------------------- #
# The worker module loads in the lean backend (stdlib-only top level)
# --------------------------------------------------------------------------- #

def test_worker_module_loads_without_torch():
    # The worker's top level is stdlib-only, so it imports in the lean 3.14 suite (no torch).
    tw = _load_worker()
    # The batch planner / bands / governor / watchdog / VRAM-budget helpers are all present...
    for name in ("plan_sub_batches", "order_speaker_groups", "estimate_batch_vram",
                 "sub_batch_timeout_seconds",
                 "run_with_watchdog", "_clear_gpu_cache", "_talker_vram_params",
                 "_free_vram_budget", "_free_vram", "_total_vram", "_warmup",
                 "_synth_sub_batch", "plan_next_sub_batch", "plan_row_tokens",
                 "_run_design_batch", "_save_and_report_design", "_generate_rows",
                 "_row_output_paths", "_save_and_report",
                 "_clone_input_overhead", "band_cap_for_chars", "VramGovernor",
                 "plan_merge_batches", "normalize_pause_ms", "boundary_gap_ms",
                 "merge_stage1_frac", "merge_stage2_frac", "merge_encode_frac"):
        assert callable(getattr(tw, name)), f"missing {name}"
    for const in ("ROW_STRUCTURAL_OVERHEAD", "CLONE_FALLBACK_OVERHEAD",
                  "CHAR_TOKENS_PER_CHAR", "LENGTH_RATIO", "PEAK_PRESSURE_FRAC",
                  "PEAK_GROW_FRAC",
                  "FREE_FLOOR_GB", "VRAM_SCALE_MIN", "VRAM_SCALE_MAX"):
        assert getattr(tw, const) > 0, f"missing {const}"
    # the length bands: non-empty, ascending ceilings, fractions in (0, 1]
    bands = tw.LENGTH_BANDS
    assert bands and all(0 < f <= 1 for _limit, f in bands)
    ceilings = [limit for limit, _f in bands]
    assert all(a < b for a, b in zip(ceilings, ceilings[1:]))
    # ...and the tokenizer is OUT of the planning path (char counts are the length metric)...
    assert not hasattr(tw, "_row_tokens")
    assert not hasattr(tw, "_tensor_tokens")
    # ...and the old thread-pool scheduler is gone...
    assert not hasattr(tw, "run_bounded")
    # ...with its stdlib dependencies intact (no concurrent.futures).
    assert tw.threading is not None
    assert not hasattr(tw, "ThreadPoolExecutor")
