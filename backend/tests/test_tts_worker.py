"""Offline tests for the batch worker's bounded-concurrency scheduler
(``tts-engine/tts_worker.py::run_bounded``).

``run_bounded`` is the code that enforces the 一键合成 并发段数 limit: it overlaps per-segment
TTS generation with a ``ThreadPoolExecutor`` of exactly ``concurrency`` workers (or runs
sequentially when the limit is 1). It is pure stdlib (no torch), so it can be loaded and
driven from the lean 3.14 backend test suite via ``importlib`` — the worker module's
top-level imports are stdlib-only, so loading it never pulls in the ML stack. These tests
prove the limit is *actually* enforced (peak in-flight never exceeds the configured value)
and, for >1, that real parallelism is reached (the setting is not a silent no-op).
"""
from __future__ import annotations

import importlib.util
import threading
import time

from backend.core.paths import PROJECT_ROOT

WORKER_PATH = PROJECT_ROOT / "tts-engine" / "tts_worker.py"


def _load_worker():
    """Load ``tts-engine/tts_worker.py`` as a fresh module (stdlib-only top level; no torch)."""
    spec = importlib.util.spec_from_file_location("tts_worker_under_test", WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_probe(hold=0.05):
    """A fake per-item ``fn`` that records how many items run at once.

    Each call bumps a shared in-flight counter (recording the peak), holds its slot for
    ``hold`` seconds (so the pool's workers genuinely overlap), then drops the counter.
    Returns ``(fn, state)``.
    """
    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0, "ran": 0}

    def fn(item):
        with lock:
            state["in_flight"] += 1
            if state["in_flight"] > state["peak"]:
                state["peak"] = state["in_flight"]
            state["ran"] += 1
        time.sleep(hold)  # occupy the slot so the pool's workers overlap
        with lock:
            state["in_flight"] -= 1
        return item

    return fn, state


# --------------------------------------------------------------------------- #
# The load-bearing guarantee: the limit is enforced (peak in-flight <= concurrency)
# --------------------------------------------------------------------------- #

def test_run_bounded_never_exceeds_limit():
    tw = _load_worker()
    for n in (1, 2, 4):
        fn, state = _make_probe()
        tw.run_bounded(list(range(n * 6)), n, fn)
        assert state["peak"] <= n, f"concurrency {n}: peak in-flight {state['peak']} exceeded the limit"


def test_run_bounded_sequential_at_one():
    tw = _load_worker()
    fn, state = _make_probe()
    tw.run_bounded(list(range(6)), 1, fn)
    assert state["peak"] == 1  # fully sequential — never two at once


def test_run_bounded_reaches_limit_when_parallel():
    # For >1 the pool must actually run ``concurrency`` items concurrently (not serialize by
    # accident) — otherwise the 并发数 setting would be a no-op.
    tw = _load_worker()
    for n in (2, 4):
        fn, state = _make_probe()
        tw.run_bounded(list(range(n * 6)), n, fn)
        assert state["peak"] == n, f"concurrency {n}: expected to reach {n} in flight, saw {state['peak']}"


# --------------------------------------------------------------------------- #
# Correctness of the scheduler's contract (order / coverage / on_result thread)
# --------------------------------------------------------------------------- #

def test_run_bounded_results_preserve_input_order():
    tw = _load_worker()
    fn, _ = _make_probe(hold=0.0)  # fn returns its item
    items = list(range(12))
    assert tw.run_bounded(items, 4, fn) == items  # input order, regardless of completion order


def test_run_bounded_runs_every_item_once():
    tw = _load_worker()
    fn, state = _make_probe(hold=0.0)
    tw.run_bounded(list(range(10)), 3, fn)
    assert state["ran"] == 10


def test_run_bounded_on_result_runs_on_coordinator_thread():
    # on_result must run on the calling (coordinator) thread — never a pool worker — so the
    # [segment] / [progress] lines it emits stay single-threaded and the progress monotonic.
    tw = _load_worker()
    main = threading.get_ident()
    seen = []

    def fn(item):
        time.sleep(0.01)
        return item

    def on_result(item, result):
        seen.append((threading.get_ident(), result))

    tw.run_bounded(list(range(8)), 4, fn, on_result=on_result)
    assert len(seen) == 8
    assert all(tid == main for tid, _r in seen), "on_result ran off the coordinator thread"


def test_run_bounded_single_item_is_sequential():
    # A lone item takes the sequential path even when the limit is >1.
    tw = _load_worker()
    fn, state = _make_probe()
    tw.run_bounded([42], 8, fn)
    assert state["peak"] == 1


def test_run_bounded_invalid_concurrency_falls_back_to_one():
    tw = _load_worker()
    fn, state = _make_probe()
    tw.run_bounded(list(range(4)), 0, fn)  # 0 -> clamped to 1 (sequential)
    assert state["peak"] == 1


# --------------------------------------------------------------------------- #
# The worker module loads in the lean backend (stdlib-only top level)
# --------------------------------------------------------------------------- #

def test_worker_module_loads_without_torch():
    # The worker's top level is stdlib-only, so it imports in the lean 3.14 suite (no torch).
    tw = _load_worker()
    assert callable(tw.run_bounded)
    # The scheduler's dependencies are stdlib and bound at import time:
    assert tw.ThreadPoolExecutor is not None
    assert tw.as_completed is not None
    assert tw.threading is not None
