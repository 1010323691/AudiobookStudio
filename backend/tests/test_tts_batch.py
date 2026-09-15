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


# --------------------------------------------------------------------------- #
# synthesize → the worker command carries the (clamped) --concurrency
# --------------------------------------------------------------------------- #

import json  # noqa: E402
import os  # noqa: E402
from collections import deque  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from backend.core import config as core_config  # noqa: E402
from backend.core import paths as core_paths  # noqa: E402
import backend.engines.tts_batch as tts_batch  # noqa: E402
from backend.engines.tts import WorkerWatchdogTimeout  # noqa: E402


class _Handle:
    """A minimal TaskHandle stand-in: records log/progress, never cancels or pauses."""

    def __init__(self):
        self.logs = []
        self.progresses = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def progress(self, frac, current=""):
        self.progresses.append((frac, current))

    def check(self):
        pass


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A throwaway project root + workspace so get_layout()/resolve_parsed_json() resolve.

    Seeds one parsed script: two non-empty lines + one empty (which ``_build_segments``
    skips). No voice_config is written — the run still completes because ``run_worker``
    is stubbed to report every segment ok.
    """
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    (ws / "03_parsed_json" / "s.json").write_text(
        json.dumps([
            {"speaker": "A", "text": "hello"},
            {"speaker": "B", "text": "world"},
            {"speaker": "A", "text": ""},  # empty -> skipped by _build_segments
        ]),
        encoding="utf-8",
    )
    yield ws
    core_config.reset_config_cache()


def _fake_run_worker(captured):
    """A run_worker stand-in: records the cmd and simulates a fully-successful child."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    return run_worker


def _stub_engine(monkeypatch, captured):
    """Point the engine at fakes so no real .venv-tts subprocess is spawned."""
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", _fake_run_worker(captured))


def _cmd_flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_synthesize_passes_request_concurrency(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 4)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "4"


def test_synthesize_defaults_concurrency_from_config(workspace, monkeypatch):
    # concurrency=None -> the persisted default (config.tts.batch_concurrency = 4).
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "4"


def test_synthesize_zero_concurrency_falls_back_to_default(workspace, monkeypatch):
    # 0 is falsy -> treated as "use the config default" (4), never a degenerate pool.
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 0)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "4"


def test_synthesize_clamps_concurrency_to_max(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 99)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "64"


def test_synthesize_clamps_concurrency_to_min(workspace, monkeypatch):
    # -5 is truthy (so it is used, not the default) but clamps down to 1.
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", -5)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "1"


def test_synthesize_reports_effective_concurrency_in_log(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 3)
    assert any("批内上限 3 段" in msg for _lvl, msg in h.logs)


# --------------------------------------------------------------------------- #
# the watchdog / restart loop — shrink, isolate, and the attempt cap
# --------------------------------------------------------------------------- #

def test_synthesize_watchdog_shrinks_and_restarts(workspace, monkeypatch):
    """A hung child (exit 124) shrinks the batch and restarts; a clean 2nd run finishes the job."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:  # first run hangs: name the in-flight batch, then exit 124
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # second run is clean: synthesize whatever is still remaining
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 4)
    assert len(calls) == 2  # one restart, not a fatal failure
    assert _cmd_flag(calls[0], "--concurrency") == "4"
    assert _cmd_flag(calls[1], "--concurrency") == "2"  # the cap halved on the restart
    assert result["completed"] == 2 and result["failed"] == []  # the task still succeeds
    assert any("缩到 2 段" in msg for _lvl, msg in h.logs)  # the shrink is logged


def test_synthesize_watchdog_isolates_poison_segment_at_workers_one(workspace, monkeypatch):
    """At workers==1 a segment that times out twice is isolated as a failure; the rest complete."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) in (1, 2):  # poison segment 0 times out twice (strike, then isolate)
            on_line("[watchdog] timeout batch=custom#1 indices=[0] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # third run: only the healthy remaining segment (index 1)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 1)
    assert len(calls) == 3
    assert all(_cmd_flag(c, "--concurrency") == "1" for c in calls)  # never grows back
    assert result["completed"] == 1  # the healthy segment (index 1) completed
    assert len(result["failed"]) == 1 and result["failed"][0]["index"] == 0
    assert "隔离" in result["failed"][0]["reason"]  # the poison segment is a recorded failure
    # and it lands in the manifest as not-ok, so a later default resume would retry it
    by_index = {e["index"]: e for e in json.loads(
        (workspace / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert by_index[0]["ok"] is False and by_index[1]["ok"] is True


def test_synthesize_watchdog_attempt_cap_raises(workspace, monkeypatch):
    """If the engine keeps timing out (and nothing is ever isolated), the run gives up -> FAILED."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        # Always time out, always naming an out-of-range index that can never be isolated,
        # so `remaining` never shrinks and the attempt cap is the only thing that ends the loop.
        calls.append(cmd)
        on_line("[watchdog] timeout batch=custom#1 indices=[99] elapsed=181.2")
        raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    with pytest.raises(RuntimeError):
        tts_batch.synthesize(h, None, "s.json", 1)
    assert len(calls) == 9  # MAX_ATTEMPTS (8) shrink-retries, then the 9th attempt trips the cap


# --------------------------------------------------------------------------- #
# the persistent per-run transcript (a forensic trail for a mid-batch death)
# --------------------------------------------------------------------------- #

def test_synthesize_persists_run_log(workspace, monkeypatch):
    """synthesize mirrors the child transcript to a per-run log file under <workspace>/logs
    (the task log is SSE-only and vanishes with the session; a run that dies mid-batch must
    leave its trail on disk)."""
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        captured["log_file"] = kw.get("log_file")
        log_file = kw.get("log_file")
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("=== attempt started ===\n[out] 引擎就绪\n[err] simulated fault\n"
                        "=== attempt ended rc=0 ===\n")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok "
                    f"{os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine",
                        lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)

    log_file = captured["log_file"]
    assert log_file is not None
    assert log_file.parent == workspace / "logs"
    assert log_file.name.startswith("tts_batch_") and log_file.name.endswith(".log")
    text = log_file.read_text(encoding="utf-8")
    assert "[out] 引擎就绪" in text and "[err] simulated fault" in text
    assert any("运行日志" in msg for _lvl, msg in h.logs)  # the user can find the file


def test_run_worker_mirrors_transcript_to_log_file(tmp_path):
    """run_worker appends every stdout/stderr line — plus attempt start/end markers with the
    exit code — to ``log_file`` (a real one-shot child; OOM tracebacks on stderr included)."""
    import sys

    from backend.engines import tts as tts_eng

    log_file = tmp_path / "logs" / "unit_run.log"
    code = ("import sys; "
            "print('[segment] 0 ok /x.mp3', flush=True); "
            "print('boom-line', file=sys.stderr, flush=True)")
    h = _Handle()
    tail = tts_eng.run_worker([sys.executable, "-c", code], h, lambda line: None,
                              log_file=log_file)
    text = log_file.read_text(encoding="utf-8")
    assert "=== attempt started" in text
    assert "[out] [segment] 0 ok /x.mp3" in text
    assert "[err] boom-line" in text
    assert "=== attempt ended rc=0" in text
    assert "boom-line" in " | ".join(tail)  # the stderr tail still feeds the error message


def test_run_worker_cancel_not_stalled_by_backlog(tmp_path):
    """A cancel must be honoured within one line, NOT after the whole output backlog drains.

    On a loaded machine (AV scan + disk / GPU contention) each line's processing is slow, so
    the main loop falls far behind the full-speed worker and a large backlog queues up. The
    old code checked cancel only once per full-drain cycle, so a cancel clicked mid-run waited
    for the ENTIRE backlog (minutes) before the child was killed — the user saw the log keep
    scrolling and thought "取消停不下来". The per-line check bounds the latency to one line.

    Simulated here: the child spews 1500 lines in well under a second (they all queue up), the
    consumer digests 20 ms/line (1500 × 20 ms = 30 s of draining), and the cancel lands at line
    120 (~2.5 s in). Pre-fix the recognition came with the next top-of-loop check, ~30 s away;
    the bound below pins the post-fix behaviour.
    """
    import sys
    import threading
    import time

    import backend.core.tasks as core_tasks
    from backend.engines import tts as tts_eng

    class _LateCancel(_Handle):
        """A stand-in that sets its cancel flag once the (slow) consumer digested N lines."""

        def __init__(self, after: int):
            super().__init__()
            self.cancel_event = threading.Event()
            self.after = after
            self.seen = 0

        def check(self):
            if self.cancel_event.is_set():
                raise core_tasks.TaskCancelled()

        def note_line(self):
            self.seen += 1
            if self.seen >= self.after:
                self.cancel_event.set()

    code = ("import time\n"
            "for i in range(1500):\n"
            "    print(f'seg-{i}', flush=True)\n"
            "time.sleep(300)\n")
    h = _LateCancel(after=120)
    t0 = time.monotonic()
    with pytest.raises(core_tasks.TaskCancelled):
        tts_eng.run_worker([sys.executable, "-c", code], h,
                           lambda line: (time.sleep(0.02), h.note_line()))
    assert time.monotonic() - t0 < 10, "cancel stalled behind the output backlog"


# --------------------------------------------------------------------------- #
# resume / incremental manifest / batch-status
# --------------------------------------------------------------------------- #

def _seed_done_index0(ws):
    """Pre-seed the package with index 0 already synthesized (manifest entry + file on disk)."""
    out_dir = ws / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "0001.mp3").write_bytes(b"fake")
    (out_dir / "manifest.json").write_text(json.dumps([
        {"index": 0, "speaker": "A", "text": "hello", "path": str(out_dir / "0001.mp3"),
         "ok": True, "reason": "", "pause_after": None},
    ]), encoding="utf-8")
    return out_dir


def _segments_written(captured):
    """The per-line segments the (fake) worker was asked to synthesize, as ``[index, ...]``."""
    seg_file = captured["cmd"][captured["cmd"].index("--segments-file") + 1]
    with open(seg_file, encoding="utf-8") as f:
        return [s["index"] for s in json.load(f)]


def test_synthesize_resume_skips_done(workspace, monkeypatch):
    _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)  # default = resume
    assert _segments_written(captured) == [1]  # only the not-yet-done segment is synthesized


def test_synthesize_resume_manifest_is_cumulative(workspace, monkeypatch):
    out_dir = _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    by_index = {e["index"]: e for e in json.loads((out_dir / "manifest.json").read_text("utf-8"))}
    assert set(by_index) == {0, 1}  # pre-done + newly-done: one entry per non-empty segment
    # The legacy absolute path of the preserved entry is migrated to the workspace-relative
    # form on load; this run's (absolute, worker-reported) output is stored in the same form.
    assert by_index[0]["ok"] is True and by_index[0]["path"] == "05_audio_chunk/s/0001.mp3"
    assert by_index[1]["ok"] is True and by_index[1]["path"] == "05_audio_chunk/s/0002.mp3"


def test_synthesize_writes_manifest_incrementally(workspace, monkeypatch):
    """In-memory state updates per line; the disk rewrite is throttled — but the FIRST line
    always flushes (so a cancel right after the first result loses nothing) and the run's end
    force-flushes (so the manifest is complete whenever the run settles)."""
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out-dir") + 1]
        on_line(f"[segment] 0 ok {os.path.join(out, '0001.mp3')}")
        # After the first segment the manifest must already reflect it (the first line always
        # flushes, even under the throttle) — this is exactly what makes an early cancel lose
        # nothing.
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            mid = json.load(f)
        assert any(e["index"] == 0 and e["ok"] for e in mid), "manifest not flushed after first segment"
        on_line(f"[segment] 1 ok {os.path.join(out, '0002.mp3')}")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    tts_batch.synthesize(_Handle(), None, "s.json", None)

    # The completion force-flush covers the throttled line: the manifest is complete at rest.
    by = {e["index"]: e for e in json.loads(
        (workspace / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert all(by[i]["ok"] for i in (0, 1))


def test_synthesize_manifest_flush_is_throttled(workspace, monkeypatch):
    """A burst of [segment] lines inside the throttle window rewrites the file only once
    (the per-line updates stay in memory); the completion force-flush lands the complete
    manifest. Pins that the hot path is NOT a full-file rewrite per line."""
    captured = {}
    writes = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out-dir") + 1]
        for i in (0, 1):  # both lines land within the 2s throttle window
            on_line(f"[segment] {i} ok {os.path.join(out, f'{i + 1:04d}.mp3')}")

    real_write = tts_batch._write_manifest_file  # capture before the patch below

    def counting(path, manifest):
        writes.append((len(manifest), sum(e["ok"] for e in manifest)))
        real_write(path, manifest)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    monkeypatch.setattr(tts_batch, "_write_manifest_file", counting)
    tts_batch.synthesize(_Handle(), None, "s.json", None)

    # The manifest always describes every segment (done + pending), so each write carries 2
    # entries: the first-line flush sees 1 ok (segment 1 is still pending), the second line
    # triggers NO write (throttle window), and the completion force-flush sees both ok.
    # Exactly two disk writes for the whole run.
    assert writes == [(2, 1), (2, 2)]


def test_synthesize_resume_all_done_short_circuits(workspace, monkeypatch):
    ws = workspace
    out_dir = ws / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("0001.mp3", "0002.mp3"):
        (out_dir / name).write_bytes(b"fake")
    (out_dir / "manifest.json").write_text(json.dumps([
        {"index": 0, "speaker": "A", "text": "hello", "path": str(out_dir / "0001.mp3"),
         "ok": True, "reason": "", "pause_after": None},
        {"index": 1, "speaker": "B", "text": "world", "path": str(out_dir / "0002.mp3"),
         "ok": True, "reason": "", "pause_after": None},
    ]), encoding="utf-8")
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)  # must never be called when a resume has nothing left

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", None)
    assert calls == []  # no engine spawned (no wasted model load)
    assert result["completed"] == 2 and result["failed"] == []
    assert result["done_count"] == 2 and result["all_count"] == 2
    assert any("已全部完成" in msg for _lvl, msg in h.logs)


def test_is_done_requires_ok_and_file(workspace):
    d = workspace / "x"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "a.mp3"
    f.write_bytes(b"fake")
    assert tts_batch.is_done({"ok": True, "path": str(f)}) is True
    assert tts_batch.is_done({"ok": True, "path": str(d / "missing.mp3")}) is False  # ok but file gone
    assert tts_batch.is_done({"ok": False, "path": str(f)}) is False  # a failure is not done
    assert tts_batch.is_done({"ok": True, "path": ""}) is False  # no path
    assert tts_batch.is_done(None) is False


def test_plan_to_synthesize_resume_skips_done():
    assert tts_batch.plan_to_synthesize({0, 1, 2}, {0, 1}) == {2}


def test_plan_to_synthesize_explicit_indices_intersect():
    assert tts_batch.plan_to_synthesize({0, 1, 2}, {0}, indices=[1, 2, 9]) == {1, 2}


def test_batch_status_counts(workspace):
    from backend.api.tts import batch_status
    # No manifest yet -> the two non-empty lines, none done.
    assert batch_status("s.json") == {"total": 2, "completed": 0, "remaining": 2}
    _seed_done_index0(workspace)  # one done
    assert batch_status("s.json") == {"total": 2, "completed": 1, "remaining": 1}


def test_batch_status_missing_script_degrades(workspace):
    from backend.api.tts import batch_status
    assert batch_status("nope.json") == {"total": 0, "completed": 0, "remaining": 0}


# --------------------------------------------------------------------------- #
# multi-file run (synthesize_multi) — the 待合成 card's multi-select
# --------------------------------------------------------------------------- #

def _seed_second_file(ws, name="t.json"):
    """A second parsed script with its own speaker set (multi-file tests)."""
    (ws / "03_parsed_json" / name).write_text(
        json.dumps([{"speaker": "C", "text": "uno"}, {"speaker": "C", "text": "dos"}]),
        encoding="utf-8",
    )


def _seed_voice_config(ws, entries):
    (ws / "04_voice_profiles").mkdir(parents=True, exist_ok=True)
    (ws / "04_voice_profiles" / "voice_config.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _seed_done_package(ws, pkg, done):
    """Pre-seed a package with completed segments: manifest entries + files on disk.

    ``done`` = a list of ``(index, speaker, text, filename)`` tuples.
    """
    out_dir = ws / "05_audio_chunk" / pkg
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, speaker, text, fname in done:
        (out_dir / fname).write_bytes(b"fake")
        manifest.append({"index": index, "speaker": speaker, "text": text,
                         "path": str(out_dir / fname), "ok": True, "reason": "",
                         "pause_after": None})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding="utf-8")
    return out_dir


def test_synthesize_multi_one_done_short_circuits(workspace, monkeypatch):
    """A fully-done file never spawns the engine; the pending file runs; results aggregate."""
    ws = workspace
    _seed_second_file(ws)
    _seed_done_package(ws, "t", [(0, "C", "uno", "0001.mp3"), (1, "C", "dos", "0002.mp3")])
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            # write the file the line claims to have produced (is_done = ok + file on disk)
            (Path(out_dir) / f"{s['index'] + 1:04d}.mp3").write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)

    assert len(calls) == 1  # the done file short-circuits (no wasted model load)
    assert _cmd_flag(calls[0], "--out-dir").endswith(os.path.join("05_audio_chunk", "s"))
    assert [f["script"] for f in result["files"]] == ["s.json", "t.json"]  # request order
    assert result["files"][0]["completed"] == 2 and result["files"][0]["error"] is None
    assert result["files"][1]["completed"] == 2 and result["files"][1]["error"] is None
    assert result["total"] == 4 and result["completed"] == 4 and result["failed"] == []
    assert result["done_count"] == 4 and result["all_count"] == 4


def test_synthesize_multi_all_done_no_engine(workspace, monkeypatch):
    """Everything already done → zero engine spawns and NO failure (resume semantics)."""
    ws = workspace
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3"), (1, "B", "world", "0002.mp3")])
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json"], 4)
    assert calls == []
    assert result["completed"] == 2 and result["files"][0]["error"] is None


def test_synthesize_multi_zero_segment_files_succeed(workspace, monkeypatch):
    """A file with entries but no synthesizable segments takes the zero-short-circuit:
    success, no engine, no (misleading) 'all failed' error."""
    ws = workspace
    (ws / "03_parsed_json" / "empty.json").write_text(
        json.dumps([{"speaker": "A", "text": "   "}]), encoding="utf-8")
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["empty.json"], 4)
    assert calls == []
    assert result["completed"] == 0 and result["files"][0]["error"] is None
    assert result["files"][0]["total"] == 0


def test_synthesize_multi_failed_segments_tagged_with_script(workspace, monkeypatch):
    """One failed segment in file 2: the run succeeds; the failure carries its file name."""
    ws = workspace
    _seed_second_file(ws)

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            # fail exactly one segment — in file 2 only (the package dir is named after the stem)
            if Path(out_dir).name == "t" and s["index"] == 1:
                on_line(f"[segment] {s['index']} error 强制失败（fake worker）")
            else:
                (Path(out_dir) / f"{s['index'] + 1:04d}.mp3").write_bytes(b"fake")
                on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert result["completed"] == 3  # 2 from s.json + 1 from t.json
    assert len(result["failed"]) == 1
    assert result["failed"][0]["script"] == "t.json" and result["failed"][0]["index"] == 1
    assert result["files"][0]["failed"] == 0 and result["files"][1]["failed"] == 1


def test_synthesize_multi_progress_windowed_and_monotonic(workspace, monkeypatch):
    """File i of n runs in the progress window [i/n, (i+1)/n]; the bar never dips and the
    step label always names the active file (no lingering '完成' from the previous file)."""
    ws = workspace
    _seed_second_file(ws)
    _stub_engine(monkeypatch, {})
    h = _Handle()
    tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    fracs = [p[0] for p in h.progresses]
    assert fracs == pytest.approx([0.01, 0.5, 0.51, 1.0, 1.0])  # per-file windows, then the final 完成
    assert all(a <= b + 1e-9 for a, b in zip(fracs, fracs[1:]))
    labels = [lbl for _f, lbl in h.progresses]
    assert "s.json · 启动引擎" in labels and "s.json · 完成" in labels
    assert "t.json · 启动引擎" in labels and "t.json · 完成" in labels


def test_synthesize_multi_per_file_fatal_error_isolated(workspace, monkeypatch):
    """A corrupt file 2 is a recorded per-file error — file 1's success is kept, no exception."""
    ws = workspace
    (ws / "03_parsed_json" / "bad.json").write_text("{ not json", encoding="utf-8")
    _stub_engine(monkeypatch, {})
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "bad.json"], 4)
    assert result["files"][0]["completed"] == 2 and result["files"][0]["error"] is None
    assert result["files"][1]["error"] and "无法解析" in result["files"][1]["error"]
    assert result["completed"] == 2  # the batch as a whole succeeded
    assert any(lvl == "ERROR" for lvl, _msg in h.logs)  # the isolation is logged


def test_synthesize_multi_all_files_fail_raises(workspace):
    """Nothing got synthesized while files WERE attempted → the task fails."""
    ws = workspace
    (ws / "03_parsed_json" / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        tts_batch.synthesize_multi(_Handle(), ["bad.json", "also-missing.json"], 4)


def test_synthesize_multi_cancel_between_files(workspace, monkeypatch):
    """A cancel at the file boundary propagates (never 'file failed, keep going') — file 1's
    work is kept, file 2 never reaches the engine."""
    ws = workspace
    _seed_second_file(ws)
    from backend.core.tasks import TaskCancelled

    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)

    h = _Handle()
    n = 0

    def check():
        nonlocal n
        n += 1
        if n == 2:  # the boundary check before file 2
            raise TaskCancelled()

    h.check = check

    with pytest.raises(TaskCancelled):
        tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert len(calls) == 1  # file 2 never spawned the engine
    by = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert by[0]["ok"] and by[1]["ok"]  # file 1's finished work is kept
    assert not (ws / "05_audio_chunk" / "t").exists()


# --------------------------------------------------------------------------- #
# batch-status multi-file (the per-row 待合成 stats)
# --------------------------------------------------------------------------- #

def test_batch_status_multi_counts(workspace):
    from backend.api.tts import batch_status
    _seed_second_file(workspace)
    _seed_voice_config(workspace, {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"},
        # B deliberately absent from voice_config -> not ready
        "C": {"type": "clone", "ref_audio": "04_voice_profiles/c.wav"},
    })
    r = batch_status(None, ["s.json", "t.json", "nope.json"])
    assert [f["name"] for f in r["files"]] == ["s.json", "t.json", "nope.json"]  # request order
    s, t, z = r["files"]
    assert (s["total"], s["completed"], s["remaining"]) == (2, 0, 2)
    assert s["complete"] is False
    assert (s["speakers"], s["ready"]) == (2, 1) and s["missing"] == ["B"]
    assert (t["total"], t["completed"]) == (2, 0)
    assert (t["speakers"], t["ready"], t["missing"]) == (1, 1, [])
    assert z == {"name": "nope.json", "total": 0, "completed": 0, "remaining": 0,
                 "complete": False, "speakers": 0, "ready": 0, "missing": []}
    # a file whose every segment is done (ok + file on disk) earns the 已合成 flag
    _seed_done_package(workspace, "s", [(0, "A", "hello", "0001.mp3"),
                                        (1, "B", "world", "0002.mp3")])
    assert batch_status(None, ["s.json"])["files"][0]["complete"] is True


def test_batch_status_multi_rejects_all(workspace):
    from fastapi import HTTPException

    from backend.api.tts import batch_status
    with pytest.raises(HTTPException) as ei:
        batch_status(None, ["__all__"])
    assert ei.value.status_code == 400


def test_batch_status_multi_no_workspace_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    try:
        from backend.api.tts import batch_status
        r = batch_status(None, ["a.json", "b.json"])
        assert r == {"files": [
            {"name": n, "total": 0, "completed": 0, "remaining": 0,
             "complete": False, "speakers": 0, "ready": 0, "missing": []}
            for n in ("a.json", "b.json")]}
    finally:
        core_config.reset_config_cache()


# --------------------------------------------------------------------------- #
# run_batch dispatch (the API layer)
# --------------------------------------------------------------------------- #

class _RecordingManager:
    """A task-manager stand-in that records create() calls without spawning threads."""

    def __init__(self):
        self.created = []

    def create(self, module, label, func, *args, **kwargs):
        self.created.append((module, label, func, args))

        class _Task:
            id = "fake"

        return _Task()

    def list(self):
        return []  # no in-flight tasks (the reset guard sees an idle manager)


def _stub_manager(monkeypatch):
    import backend.api.tts as tts_api

    mgr = _RecordingManager()
    monkeypatch.setattr(tts_api, "get_task_manager", lambda: mgr)
    return mgr


def test_run_batch_dispatches_multi(workspace, monkeypatch):
    from backend.api.tts import BatchRequest, run_batch

    mgr = _stub_manager(monkeypatch)
    run_batch(BatchRequest(scripts=["s.json", "t.json"], concurrency=4, seed=7))
    module, label, func, args = mgr.created[0]
    assert module == "tts-batch"
    assert func is tts_batch.synthesize_multi
    assert args == (["s.json", "t.json"], 4, 7)
    assert "2 个文件" in label and "续合" in label


def test_run_batch_single_file_via_scripts_uses_legacy_path(workspace, monkeypatch):
    from backend.api.tts import BatchRequest, run_batch

    mgr = _stub_manager(monkeypatch)
    run_batch(BatchRequest(scripts=["s.json"], concurrency=4))
    module, label, func, args = mgr.created[0]
    assert func is tts_batch.synthesize
    assert args == (None, "s.json", 4, None)  # the byte-identical legacy call
    assert "· s.json" in label


def test_run_batch_scripts_with_indices_rejected(workspace, monkeypatch):
    from fastapi import HTTPException

    from backend.api.tts import BatchRequest, run_batch

    _stub_manager(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        run_batch(BatchRequest(scripts=["s.json", "t.json"], indices=[0]))
    assert ei.value.status_code == 400


def test_run_batch_scripts_reject_all(workspace, monkeypatch):
    from fastapi import HTTPException

    from backend.api.tts import BatchRequest, run_batch

    _stub_manager(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        run_batch(BatchRequest(scripts=["__all__"]))
    assert ei.value.status_code == 400


# --------------------------------------------------------------------------- #
# reset_batch — 「重新全部合成」= delete the packages, then the ordinary run
# --------------------------------------------------------------------------- #

def test_reset_batch_removes_package_dirs(workspace, monkeypatch):
    """The requested packages (mp3s + manifest) are deleted; sibling packages survive."""
    from backend.api.tts import ResetBatchRequest, reset_batch

    _stub_manager(monkeypatch)
    _seed_done_index0(workspace)  # 05_audio_chunk/s (s.json)
    out_t = workspace / "05_audio_chunk" / "t"
    out_t.mkdir(parents=True, exist_ok=True)
    (out_t / "0001.mp3").write_bytes(b"fake")
    (out_t / "manifest.json").write_text("[]", encoding="utf-8")
    out_u = workspace / "05_audio_chunk" / "u"  # NOT requested — must survive
    out_u.mkdir(parents=True, exist_ok=True)
    (out_u / "0001.mp3").write_bytes(b"fake")

    res = reset_batch(ResetBatchRequest(scripts=["s.json", "t.json"]))
    assert res == {"ok": True, "removed": ["s", "t"]}
    assert not (workspace / "05_audio_chunk" / "s").exists()
    assert not (workspace / "05_audio_chunk" / "t").exists()
    assert (out_u / "0001.mp3").exists()


def test_reset_batch_noop_when_absent(workspace, monkeypatch):
    from backend.api.tts import ResetBatchRequest, reset_batch

    _stub_manager(monkeypatch)
    assert reset_batch(ResetBatchRequest(scripts=["s.json"])) == {"ok": True, "removed": []}


def test_reset_batch_checked_name_maps_to_base_package(workspace, monkeypatch):
    """A ``_checked`` file name maps to the SAME package the engine writes to (``package_for``
    strips the suffix on both sides) — a reset can never orphan the engine's output."""
    from backend.api.tts import ResetBatchRequest, reset_batch

    _stub_manager(monkeypatch)
    _seed_done_index0(workspace)  # 05_audio_chunk/s
    res = reset_batch(ResetBatchRequest(scripts=["s_checked.json"]))
    assert res == {"ok": True, "removed": ["s"]}
    assert not (workspace / "05_audio_chunk" / "s").exists()


def test_reset_batch_rejects_all_sentinel(workspace, monkeypatch):
    from fastapi import HTTPException

    from backend.api.tts import ResetBatchRequest, reset_batch

    _stub_manager(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        reset_batch(ResetBatchRequest(scripts=["__all__"]))
    assert ei.value.status_code == 400


def test_reset_batch_requires_workspace(tmp_path, monkeypatch):
    """No workspace pointer -> the write guard refuses before any deletion."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    from fastapi import HTTPException

    from backend.api.tts import ResetBatchRequest, reset_batch

    try:
        core_config.reset_config_cache()
        with pytest.raises(HTTPException) as ei:
            reset_batch(ResetBatchRequest(scripts=["s.json"]))
        assert ei.value.status_code == 409
    finally:
        core_config.reset_config_cache()


class _InFlightManager(_RecordingManager):
    """A manager reporting one in-flight tts-batch task (the reset guard)."""

    def list(self):
        class _Task:
            module = "tts-batch"
            status = "running"

        return [_Task()]


def test_reset_batch_refused_while_task_in_flight(workspace, monkeypatch):
    """A running synthesis task is writing the package folders — the reset must wait."""
    import backend.api.tts as tts_api
    from fastapi import HTTPException

    from backend.api.tts import ResetBatchRequest, reset_batch

    monkeypatch.setattr(tts_api, "get_task_manager", lambda: _InFlightManager())
    with pytest.raises(HTTPException) as ei:
        reset_batch(ResetBatchRequest(scripts=["s.json"]))
    assert ei.value.status_code == 409


def test_reset_then_ordinary_run_redoes_all(workspace, monkeypatch):
    """「重新全部合成」end-to-end: a fully-done package is reset (deleted), and the ordinary
    one-click run — the SAME call a fresh run is, no special flags — re-synthesizes every
    segment instead of taking the zero-segment short-circuit."""
    from backend.api.tts import ResetBatchRequest, reset_batch

    _stub_manager(monkeypatch)
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        """Like _fake_run_worker, but leaves the mp3 files on disk (the real worker does)."""
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        for s in segs:
            name = str(s["index"] + 1).zfill(4) + ".mp3"
            (out_dir / name).write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {str(out_dir / name)}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)

    tts_batch.synthesize(_Handle(), None, "s.json", None)  # run 1: completes everything

    calls = []

    def counting(cmd, handle, on_line, **kw):
        calls.append(cmd)

    monkeypatch.setattr(tts_batch, "run_worker", counting)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    assert calls == []  # pre-reset: a plain resume has nothing left (zero-segment short-circuit)

    reset_batch(ResetBatchRequest(scripts=["s.json"]))
    assert not (workspace / "05_audio_chunk" / "s").exists()

    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    tts_batch.synthesize(_Handle(), None, "s.json", None)  # the identical ordinary call
    assert sorted(_segments_written(captured)) == [0, 1]  # every segment re-synthesized
