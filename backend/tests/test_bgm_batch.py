"""Tests for the BGM batch dispatch (``api/bgm.py``): ordered + prefetched
coordinators over the REAL process-wide gates (shared LLM gate for analysis,
``merge_gate()`` for mix), the per-chapter in-flight 409s, the endpoint guards,
the synchronous ``POST /match`` write, and the read-only ``GET /chapters`` rows.

``run_analyze`` / ``run_mix`` / ``run_match`` / ``list_chapters`` /
``update_chapter`` are called directly (the suite never goes through the HTTP
layer). The engine workers are monkeypatched with controllable stubs that use
the REAL gates — the coordinators' decisions are driven by ``gate().active`` /
``merge_gate().active`` exactly as in production.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api import bgm as api_bgm
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.core.tasks import TERMINAL, TaskCancelled, TaskStatus, get_task_manager
from backend.engines import music as music_engine
import backend.engines.bgm as bgm_engine
import backend.engines.merge as merge_engine

STEMS = [f"ch{i}" for i in range(1, 9)]  # up to 8 chapters


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Throwaway project root + workspace (8 02 chapter files, one library
    track, one 06 narration + assignment per chapter) and drained gates after."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    (ws / "02_split_text").mkdir(parents=True)
    for s in STEMS:
        (ws / "02_split_text" / f"{s}.txt").write_bytes("本章内容。".encode("utf-8"))
    core_config.set_workspace_pointer(str(ws))
    core_config.update_config({"llm": {"model_name": "test-model"}})
    # one enabled, tagged library track
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "t1.mp3").write_bytes(b"music-bytes")
    music_engine.update_index(
        lambda idx: idx["tracks"].update({
            "t1.mp3": {"duration": 120.0, "enabled": True, "description": "",
                       "tags": {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []},
                       "added_at": ""}}))
    _seed_mix(ws, STEMS)
    limits = (concurrency.gate().limit, concurrency.merge_gate().limit)
    yield ws
    # A failed test must not leak gate slots / zombie stub threads into later tests:
    # cancel every still-active BGM task (stubs honour cooperative cancel and release
    # their slots), let BOTH gates drain, restore their limits.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module in ("bgm-analysis", "bgm-mix") and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    stuck = []
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module in ("bgm-analysis", "bgm-mix") and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0 and concurrency.merge_gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"bgm tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0 and concurrency.merge_gate().active == 0
    concurrency.set_concurrency(limits[0])
    concurrency.set_merge_concurrency(limits[1])
    core_config.reset_config_cache()


def _seed_mix(ws, stems: list[str], music: str | None = "t1.mp3") -> None:
    """06 narration mp3s + a (matched) assignment entry per stem."""
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    for s in stems:
        (ws / "06_audio_merge" / f"{s}.mp3").write_bytes(b"NARR" * 16)
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    for s in stems:
        data["chapters"][s] = {"tags": {}, "music": music, "locked": False,
                               "manual": False, "score": 3, "reason": "r",
                               "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


class Ctl:
    """Per-test coordination points for the stub workers."""

    def __init__(self, stems):
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.holders: set[str] = set()
        self.done: set[str] = set()
        self.go = threading.Event()
        self.releases: dict[str, threading.Event] = {s: threading.Event() for s in stems}

    def snapshot_holders(self) -> list[str]:
        with self.lock:
            return sorted(self.holders)


def _make_stub(ctl: Ctl, gate_fn):
    def stub(handle, stem, *args, **kw):
        with ctl.lock:
            ctl.order.append(stem)
        # Park BEFORE the gate (cancel-aware, like the real engine).
        while not handle.cancelled and not ctl.go.wait(0.05):
            pass
        if handle.cancelled:
            raise TaskCancelled()
        g = gate_fn()
        if not g.acquire(stop_check=lambda: handle.cancelled):
            raise TaskCancelled()
        with ctl.lock:
            ctl.holders.add(stem)
        try:
            while not handle.cancelled and not ctl.releases[stem].wait(0.05):
                pass
        finally:
            g.release()
            with ctl.lock:
                ctl.holders.discard(stem)
        if handle.cancelled:
            raise TaskCancelled()
        with ctl.lock:
            ctl.done.add(stem)
        return {"ok": True, "stem": stem}

    return stub


def _launch_analyze(monkeypatch, stems, c: int, go: bool = True):
    """run_analyze with a controllable stub on the REAL shared LLM gate (limit c).

    The Ctl covers ALL stems — non-conflicting chapters may be added to a running
    batch mid-test (the in-flight 409 tests launch extra stems after the fact).
    """
    core_config.update_config({"generation": {"max_concurrency": c}})
    ctl = Ctl(STEMS)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.bgm.analyze_chapter", _make_stub(ctl, concurrency.gate))
    mgr = get_task_manager()
    res = api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=list(stems)))
    return ctl, mgr, res


def _launch_mix(monkeypatch, stems, cpu: int, go: bool = True):
    """run_mix with a controllable stub on the REAL merge gate (limit = cpu // 2)."""
    ctl = Ctl(STEMS)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.bgm.mix_chapter", _make_stub(ctl, concurrency.merge_gate))
    monkeypatch.setattr(merge_engine.os, "cpu_count", lambda: cpu)
    mgr = get_task_manager()
    res = api_bgm.run_mix(api_bgm.MixRequest(chapters=list(stems)))
    return ctl, mgr, res


# -- PENDING shells in order + labels pinned -----------------------------------

def test_analyze_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2 (shared LLM gate), 6 chapters: the coordinator's first round dispatches
    # exactly BGM_PREFETCH_DEPTH; the rest stay PENDING shells.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:6], 2, go=False)
    task_ids = res["task_ids"]
    assert [row["stem"] for row in res["chapters"]] == STEMS[:6]  # request order
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t is not None for t in tasks)
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    # The label tail is the load-bearing contract shared with the frontend's F5
    # reattach and the in-flight 409 — pin BOTH label shapes.
    assert all(t.module == "bgm-analysis" for t in tasks)
    assert [t.label for t in tasks] == [f"章节气氛分析：{s}" for s in STEMS[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == STEMS[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.gate().active == 0  # nothing acquired a slot yet

    ctl.go.set()
    for s in STEMS[:6]:
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == STEMS[:6]  # strict request order end to end


def test_mix_creates_pending_shells_in_order(workspace, monkeypatch):
    # cpu=4 → merge gate limit 2, 6 chapters: same prefetch shape on merge_gate.
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:6], 4, go=False)
    task_ids = res["task_ids"]
    assert [row["stem"] for row in res["chapters"]] == STEMS[:6]
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t.module == "bgm-mix" for t in tasks)
    assert [t.label for t in tasks] == [f"背景音乐混音：{s}" for s in STEMS[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == STEMS[:4]
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.merge_gate().active == 0

    ctl.go.set()
    for s in STEMS[:6]:
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == STEMS[:6]


# -- prefetch cap + strict order ------------------------------------------------

def test_analyze_coordinator_cap_and_order(workspace, monkeypatch):
    # C=2, 6 chapters: steady state = 2 holders + 4 slot-waiters, never more;
    # strict request order as the shared LLM gate frees.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:6], 2)
    task_ids = res["task_ids"]
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED))
            samples.append((concurrency.gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)
        assert ctl.order == STEMS[:6]
        released: set[str] = set()

        def new_wave_count() -> int:
            return sum(1 for h in ctl.snapshot_holders() if h not in released)

        for _ in range(3):
            _wait_until(lambda: new_wave_count() == 2)
            for h in ctl.snapshot_holders():
                if h not in released:
                    released.add(h)
                    ctl.releases[h].set()
        _wait_until(lambda: len(ctl.done) == 6)
    finally:
        stop.set()
        mon.join(timeout=2)
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    for active, running in samples:
        assert active <= 2, f"LLM gate over-cap: {active}"
        assert running <= 2 + api_bgm.BGM_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


def test_mix_coordinator_cap_and_order(workspace, monkeypatch):
    # cpu=4 → C=2, 6 chapters: same invariants on the merge gate.
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:6], 4)
    task_ids = res["task_ids"]
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED))
            samples.append((concurrency.merge_gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)
        assert ctl.order == STEMS[:6]
        released: set[str] = set()

        def new_wave_count() -> int:
            return sum(1 for h in ctl.snapshot_holders() if h not in released)

        for _ in range(3):
            _wait_until(lambda: new_wave_count() == 2)
            for h in ctl.snapshot_holders():
                if h not in released:
                    released.add(h)
                    ctl.releases[h].set()
        _wait_until(lambda: len(ctl.done) == 6)
    finally:
        stop.set()
        mon.join(timeout=2)
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    for active, running in samples:
        assert active <= 2, f"merge gate over-cap: {active}"
        assert running <= 2 + api_bgm.BGM_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


# -- the endpoints size their gates ----------------------------------------------

def test_analyze_endpoint_sizes_shared_llm_gate(workspace, monkeypatch):
    # run_analyze calls set_concurrency(generation.max_concurrency) on every request.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:2], 3, go=False)
    assert concurrency.gate().limit == 3


def test_mix_endpoint_sizes_merge_gate_from_cpu(workspace, monkeypatch):
    # run_mix calls set_merge_concurrency(Merge.concurrency_limit()) = cpu // 2 (cap 4).
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:2], 6, go=False)
    assert concurrency.merge_gate().limit == 3


# -- endpoint guards ---------------------------------------------------------------

def test_analyze_endpoint_guards(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=[]))
    assert e.value.status_code == 400
    assert "请选择要分析的章节" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["../evil"]))
    assert e.value.status_code == 400
    assert "非法章节名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ghost"]))
    assert e.value.status_code == 400
    assert "未找到章节文件" in e.value.detail

    core_config.update_config({"llm": {"model_name": ""}})
    try:
        with pytest.raises(HTTPException) as e:
            api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"]))
        assert e.value.status_code == 400
        assert "尚未配置 LLM 模型" in e.value.detail
    finally:
        core_config.update_config({"llm": {"model_name": "test-model"}})


def test_mix_endpoint_guards(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=[]))
    assert e.value.status_code == 400

    # never matched (no assignment entry) → 400 before any task is created
    data = bgm_engine.load_assignments(core_paths.get_layout())
    data["chapters"].pop("ch2")
    bgm_engine.save_assignments(core_paths.get_layout(), data)
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch2"]))
    assert e.value.status_code == 400
    assert "从未匹配" in e.value.detail

    # narration missing → 400
    (workspace / "06_audio_merge" / "ch3.mp3").unlink()
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch3"]))
    assert e.value.status_code == 400
    assert "未找到旁白音频" in e.value.detail

    # music deleted from the library → 400 (the ⚠ 已删除 row may NOT mix)
    (core_paths.MUSIC_LIBRARY_DIR / "t1.mp3").unlink()
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch4"]))
    assert e.value.status_code == 400
    assert "音乐库中找不到 t1.mp3" in e.value.detail
    (core_paths.MUSIC_LIBRARY_DIR / "t1.mp3").write_bytes(b"music-bytes")

    # a matched-but-no-BGM chapter (music=None) is ALLOWED — the copy2 path
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch5"] = {"tags": {}, "music": None, "locked": False,
                               "manual": False, "score": 0, "reason": "r",
                               "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)
    ctl, mgr, res = _launch_mix(monkeypatch, ["ch5"], 4, go=False)
    assert res["chapters"][0]["stem"] == "ch5"  # the copy2 path is allowed
    ctl.go.set()
    ctl.releases["ch5"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in res["task_ids"]))


def test_mix_endpoint_dedupes(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, ["ch1", "ch1", "ch2"], 4, go=False)
    assert [row["stem"] for row in res["chapters"]] == ["ch1", "ch2"]
    assert len(res["task_ids"]) == 2 and len(set(res["task_ids"])) == 2
    ctl.go.set()
    for s in ("ch1", "ch2"):
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.done) == 2)


# -- same-chapter in-flight 409 ----------------------------------------------------

def test_analyze_inflight_same_chapter_409(workspace, monkeypatch):
    # ch1 / ch2 in flight (holding their slots): resubmitting ch1 is 409; adding a
    # non-conflicting chapter is allowed; a mixed request short-circuits.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:2], 2, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "ch1" in e.value.detail

    res3 = api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch3"]))
    assert res3["chapters"][0]["stem"] == "ch3"

    with pytest.raises(HTTPException) as e2:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1", "ch3"]))
    assert e2.value.status_code == 409

    ctl.releases["ch1"].set()
    ctl.releases["ch2"].set()
    ctl.releases["ch3"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


def test_mix_inflight_same_chapter_409(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:2], 4, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "ch1" in e.value.detail

    res3 = api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch3"]))
    assert res3["chapters"][0]["stem"] == "ch3"

    for s in ("ch1", "ch2", "ch3"):
        ctl.releases[s].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


# -- POST /match (synchronous) ------------------------------------------------------

def test_match_segment_and_unknown_mode_400(workspace):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(mode="segment"))
    assert e.value.status_code == 400
    assert "段落级匹配为后续版本功能" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(mode="weird"))
    assert e.value.status_code == 400
    assert "未知匹配模式" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=[]))
    assert e.value.status_code == 400


def test_match_writes_assignments_locked_skipped(workspace):
    # ch1 locked with t1.mp3 → preserved verbatim (matched_at untouched, counted);
    # ch2 re-matched (its tags hit 紧张 → t1.mp3, but prev = ch1's locked t1 → the
    # only candidate is blocked → relaxed re-pick with the note).
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    for s in ("ch1", "ch2"):
        analysis["chapters"][s] = {"scene": [], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch1"]["locked"] = True
    data["chapters"]["ch1"]["matched_at"] = "old-t"
    bgm_engine.save_assignments(layout, data)

    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1", "ch2"]))
    assert res["skipped_locked"] == 1 and res["matched"] == 1 and res["no_bgm"] == 0
    kept = res["assignments"]["chapters"]["ch1"]
    assert kept["music"] == "t1.mp3" and kept["matched_at"] == "old-t" and kept["locked"]
    rematched = res["assignments"]["chapters"]["ch2"]
    assert rematched["music"] == "t1.mp3" and "放宽" in rematched["reason"]
    assert rematched["locked"] is False and rematched["manual"] is False
    # mode persisted + the neighbours outside the selection are untouched
    on_disk = bgm_engine.load_assignments(layout)
    assert on_disk["mode"] == "llm"
    assert on_disk["chapters"]["ch3"]["matched_at"] == "t0"  # not in the selection


def test_match_analysis_inflight_409_mix_inflight_ok(workspace, monkeypatch):
    # an analysis task on ch1 blocks /match (would read a half-written analysis);
    # a mix task on ch2 does NOT block it.
    ctl_a, mgr, res_a = _launch_analyze(monkeypatch, ["ch1"], 2, go=False)
    ctl_m, mgr2, res_m = _launch_mix(monkeypatch, ["ch2"], 4, go=False)
    ctl_a.go.set()
    ctl_m.go.set()
    _wait_until(lambda: len(ctl_a.holders) == 1 and len(ctl_m.holders) == 1)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "分析任务在途" in e.value.detail

    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch2"]))
    assert res["matched"] + res["no_bgm"] == 1

    ctl_a.releases["ch1"].set()
    ctl_m.releases["ch2"].set()
    _wait_until(lambda: all(
        mgr.get(tid).status in TERMINAL for tid in [*res_a["task_ids"], *res_m["task_ids"]]),
        timeout=5)


# -- 【取消全部】= per-task cancel converges the whole batch ------------------------

def _cancel_all_converges(ctl, mgr, task_ids, gate):
    pending = [tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]
    for tid in task_ids:
        mgr.control(tid, "cancel")
    _wait_until(lambda: all(mgr.get(tid).status is TaskStatus.CANCELLED for tid in task_ids),
                timeout=5)
    _wait_until(lambda: gate().active == 0, timeout=5)
    for tid in pending:  # the not-yet-dispatched shells finalized immediately
        t = mgr.get(tid)
        assert t.status is TaskStatus.CANCELLED and t.finished > 0


def test_analyze_cancel_all_converges(workspace, monkeypatch):
    # C=2, 8 chapters: 6 dispatched (2 holders + 4 waiters), 2 PENDING shells left.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS, 2)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"], concurrency.gate)


def test_mix_cancel_all_converges(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS, 4)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.merge_gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"], concurrency.merge_gate)


# -- GET /chapters -------------------------------------------------------------------

def test_chapters_rows(workspace):
    ws = workspace
    layout = core_paths.get_layout()
    # ch1: full pipeline state (06 + 08 + analysis + assignment with live music)
    (ws / "08_bgm").mkdir(parents=True, exist_ok=True)
    (ws / "08_bgm" / "ch1.mp3").write_bytes(b"MIXED")
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"]["ch1"] = {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": True}
    bgm_engine.save_analysis(layout, analysis)
    # ch2: narration is a .wav (fallback) + assignment points at a deleted music
    (ws / "06_audio_merge" / "ch2.mp3").unlink()
    (ws / "06_audio_merge" / "ch2.wav").write_bytes(b"WAVNARR")
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch2"]["music"] = "ghost.mp3"
    data["chapters"]["ghost"] = data["chapters"]["ch1"].copy()  # orphan (no 02 file)
    bgm_engine.save_assignments(layout, data)

    res = api_bgm.list_chapters()
    rows = {r["stem"]: r for r in res["chapters"]}
    assert list(rows) == STEMS  # the 02 files are the row basis (orphan hidden)
    assert "ghost" not in rows

    r1 = rows["ch1"]
    assert r1["narration_exists"] is True and r1["mix_exists"] is True
    assert r1["music_missing"] is False
    assert r1["analysis"]["mood"] == ["紧张"] and r1["analysis"]["edited"] is True
    assert r1["assignment"]["music"] == "t1.mp3" and r1["assignment"]["score"] == 3

    r2 = rows["ch2"]
    assert r2["narration_exists"] is True  # the .wav fallback counts
    assert r2["mix_exists"] is False
    assert r2["music_missing"] is True     # ⚠ 已删除
    assert r2["analysis"] is None

    r3 = rows["ch3"]
    assert r3["narration_exists"] is True  # fixture seed
    assert r3["mix_exists"] is False and r3["analysis"] is None
    assert r3["assignment"]["music"] == "t1.mp3" and r3["music_missing"] is False
    assert res["mode"] == "llm"


def test_chapters_no_workspace_empty(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> empty rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        assert api_bgm.list_chapters() == {"chapters": [], "mode": "llm"}
    finally:
        core_config.reset_config_cache()


# -- PUT /chapters/{stem} -------------------------------------------------------------

def test_update_chapter_tags(workspace):
    ws = workspace
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"]["ch1"] = {"scene": [], "mood": ["旧标签"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": False}
    bgm_engine.save_analysis(layout, analysis)

    res = api_bgm.update_chapter(
        "ch1", api_bgm.ChapterUpdateRequest(tags={"mood": ["紧张"], "custom": ["我的"]}))
    assert res["locked"] is False and res["manual"] is False
    a = bgm_engine.load_analysis(layout)["chapters"]["ch1"]
    assert a["edited"] is True and a["edited_at"]
    assert a["analyzed_at"] == "t0"  # the original analysis time is kept
    assert a["mood"] == ["紧张"] and a["custom"] == ["我的"]  # 紧张 = vocab mood; 我的 → custom
    e = bgm_engine.load_assignments(layout)["chapters"]["ch1"]
    assert e["tags"]["mood"] == ["紧张"]  # the snapshot followed the edit
    assert e["music"] == "t1.mp3"        # music untouched (key absent)
    assert e["reason"] == "r"


def test_update_chapter_music_and_lock(workspace):
    # manual pick
    e = api_bgm.update_chapter(
        "ch1", api_bgm.ChapterUpdateRequest(music="t1.mp3", locked=True))
    assert e["music"] == "t1.mp3" and e["manual"] is True and e["locked"] is True
    assert e["score"] is None and e["reason"] == "手动指定" and e["matched_at"]
    # clear (music present, value null)
    e2 = api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(music=None))
    assert e2["music"] is None and e2["manual"] is True and e2["reason"] == "手动指定"
    assert e2["locked"] is True  # the lock survived (key absent)
    # an unmatched chapter gets an entry created on demand
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"].pop("ch3")
    bgm_engine.save_assignments(layout, data)
    e3 = api_bgm.update_chapter("ch3", api_bgm.ChapterUpdateRequest(music="t1.mp3"))
    assert e3["music"] == "t1.mp3" and e3["manual"] is True and e3["locked"] is False


def test_update_chapter_guard_400s(workspace):
    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("../evil", api_bgm.ChapterUpdateRequest(locked=True))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="a/b.mp3"))
    assert e.value.status_code == 400
    assert "非法音乐文件名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="ghost.mp3"))
    assert e.value.status_code == 400
    assert "音乐库中找不到 ghost.mp3" in e.value.detail
    # a failed update must not have touched anything
    e0 = bgm_engine.load_assignments(core_paths.get_layout())["chapters"]["ch1"]
    assert e0["music"] == "t1.mp3" and e0["reason"] == "r"


def test_write_endpoints_no_workspace_409(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        for call in (
            lambda: api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"])),
            lambda: api_bgm.run_match(api_bgm.MatchRequest()),
            lambda: api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"])),
            lambda: api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(locked=True)),
        ):
            with pytest.raises(HTTPException) as e:
                call()
            assert e.value.status_code == 409
    finally:
        core_config.reset_config_cache()
