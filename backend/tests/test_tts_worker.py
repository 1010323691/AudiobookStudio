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
    # once a batch holds >= 4 rows, a row > 5x the shortest opens a new batch
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


# --------------------------------------------------------------------------- #
# sub_batch_timeout_seconds — the device-scaled watchdog budget
# --------------------------------------------------------------------------- #

def test_timeout_cuda_floor_cap_and_scaling():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0) == 180           # floor
    assert tw.sub_batch_timeout_seconds("cuda", 30000) == 1500      # cap
    assert tw.sub_batch_timeout_seconds("cuda", 1000) == 60 + 0.4 * 1000  # scales with chars


def test_timeout_cpu_is_looser():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cpu", 0) == 600            # floor (much looser)
    assert tw.sub_batch_timeout_seconds("cpu", 30000) == 10800      # cap
    assert tw.sub_batch_timeout_seconds("cpu", 1000) == 600 + 4 * 1000


def test_timeout_gpu_formula_for_mps():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("mps", 0) == 180  # any non-cpu device uses the GPU budget


def test_timeout_clone_formula_floor_scale_cap():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0, vtype="clone") == 300            # floor
    assert tw.sub_batch_timeout_seconds("cuda", 30000, vtype="clone") == 3600        # cap
    assert tw.sub_batch_timeout_seconds("cuda", 1000, vtype="clone") == 120 + 0.7 * 1000


def test_timeout_clone_is_roomier_than_custom():
    tw = _load_worker()
    # A batched clone decode is slower per char (its L^2 attention spans the reference frames
    # too) and the GPU is time-sliced with other processes — so clone's budget is roomier,
    # at every batch size, or a healthy-but-slow batch gets false-killed under contention.
    for chars in (0, 100, 500, 2000, 10000):
        assert tw.sub_batch_timeout_seconds("cuda", chars, vtype="clone") > \
            tw.sub_batch_timeout_seconds("cuda", chars, vtype="design")


def test_timeout_clone_covers_measured_batch_rate_under_contention():
    tw = _load_worker()
    # 16 long rows (2254 chars) measured 662s on an IDLE GPU (~0.29 s/char); the clone budget
    # must survive ~2x GPU contention (WDDM time-slicing with browser/compositor) without
    # killing a batch that is still generating.
    budget = tw.sub_batch_timeout_seconds("cuda", 2254, vtype="clone")
    assert budget >= 2 * 662


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
# The worker module loads in the lean backend (stdlib-only top level)
# --------------------------------------------------------------------------- #

def test_worker_module_loads_without_torch():
    # The worker's top level is stdlib-only, so it imports in the lean 3.14 suite (no torch).
    tw = _load_worker()
    # The batch planner / bands / governor / watchdog / VRAM-budget helpers are all present...
    for name in ("plan_sub_batches", "estimate_batch_vram", "sub_batch_timeout_seconds",
                 "run_with_watchdog", "_clear_gpu_cache", "_talker_vram_params",
                 "_free_vram_budget", "_free_vram", "_total_vram", "_warmup",
                 "_synth_sub_batch", "plan_next_sub_batch", "plan_row_tokens",
                 "_clone_input_overhead", "band_cap_for_chars", "VramGovernor"):
        assert callable(getattr(tw, name)), f"missing {name}"
    for const in ("ROW_STRUCTURAL_OVERHEAD", "CLONE_FALLBACK_OVERHEAD",
                  "CHAR_TOKENS_PER_CHAR", "PEAK_PRESSURE_FRAC", "PEAK_GROW_FRAC",
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
