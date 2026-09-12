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

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎"):
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
    assert _cmd_flag(captured["cmd"], "--concurrency") == "32"


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
    assert any("并发 3 段" in msg for _lvl, msg in h.logs)


# --------------------------------------------------------------------------- #
# resume / force_all / incremental manifest / batch-status
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


def test_synthesize_force_all_redoes_all(workspace, monkeypatch):
    _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None, True)  # force_all=True
    assert sorted(_segments_written(captured)) == [0, 1]  # re-does everything, incl. the done one


def test_synthesize_resume_manifest_is_cumulative(workspace, monkeypatch):
    out_dir = _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    by_index = {e["index"]: e for e in json.loads((out_dir / "manifest.json").read_text("utf-8"))}
    assert set(by_index) == {0, 1}  # pre-done + newly-done: one entry per non-empty segment
    assert by_index[0]["ok"] is True and by_index[0]["path"] == str(out_dir / "0001.mp3")  # preserved
    assert by_index[1]["ok"] is True  # newly synthesized this run


def test_synthesize_writes_manifest_incrementally(workspace, monkeypatch):
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎"):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out-dir") + 1]
        on_line(f"[segment] 0 ok {os.path.join(out, '0001.mp3')}")
        # After the first segment the manifest must already reflect it (write-per-segment, not
        # write-only-at-the-end) — this is exactly what makes a cancel lose nothing.
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            mid = json.load(f)
        assert any(e["index"] == 0 and e["ok"] for e in mid), "manifest not flushed after first segment"
        on_line(f"[segment] 1 ok {os.path.join(out, '0002.mp3')}")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_worker", run_worker)
    tts_batch.synthesize(_Handle(), None, "s.json", None)


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


def test_plan_to_synthesize_force_all_ignores_done():
    assert tts_batch.plan_to_synthesize({0, 1, 2}, {0, 1}, force_all=True) == {0, 1, 2}


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
