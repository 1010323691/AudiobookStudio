"""Offline tests for the two-stage merge orchestration (``backend/engines/merge.py``).

``run()`` hands the ordered per-segment manifest to the isolated worker — stubbed
here, mirroring the test_tts_batch.py pattern (monkeypatched resolve_engine /
run_worker, no real .venv-tts subprocess) — and owns the staging dir's cleanup. The
worker's two-stage part-merge behaviour itself is pinned by the pure-function tests
in test_tts_worker.py plus manual runs against the real .venv-tts.
"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
import backend.engines.merge as merge


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
    """A throwaway project root + workspace (no parsed script — merge reads the
    batch manifest directly, seeded per test)."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    yield ws
    core_config.reset_config_cache()


def _seed_manifest(ws, n, package="pkg", missing=(), top_level=False):
    """A batch manifest of n ok segments; files for non-missing indices are created."""
    pkg_dir = ws / "05_audio_chunk" if top_level else ws / "05_audio_chunk" / package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(n):
        f = pkg_dir / f"{i:04d}.mp3"
        if i not in missing:
            f.write_bytes(b"0" * 32)  # a dummy segment file
        entries.append({
            "index": i,
            "speaker": "A" if i % 2 == 0 else "B",
            "text": f"t{i}",
            "pause_after": 900 if i == 1 else None,
            "path": str(f),
            "ok": True,
            "reason": "",
        })
    (pkg_dir / "manifest.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return entries


def _fake_run_worker(captured, behaviour="ok"):
    """A run_worker stand-in: records the cmd and simulates the child's [result]
    (ok: the mp3 at --out; wav-fallback: the whole-book WAV inside --tmp-dir)."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        if behaviour == "fail":
            raise RuntimeError("engine exploded")
        out = cmd[cmd.index("--out") + 1]
        if behaviour == "wav-fallback":
            tmp_dir = cmd[cmd.index("--tmp-dir") + 1]
            path = os.path.join(tmp_dir, os.path.splitext(os.path.basename(out))[0] + ".wav")
        else:
            path = out
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * 2048)
        on_line(f"[result] {path}")
        if behaviour == "plain-lines":
            on_line("一条普通日志行")
        return deque()

    return run_worker


def _stub_engine(monkeypatch, captured, behaviour="ok"):
    """Point the engine at fakes so no real .venv-tts subprocess is spawned."""
    monkeypatch.setattr(merge, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(merge, "run_worker", _fake_run_worker(captured, behaviour))


def _cmd_flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _log_msgs(handle):
    return [msg for _level, msg in handle.logs]


# --------------------------------------------------------------------------- #
# run() — the two-stage command + staging dir lifecycle
# --------------------------------------------------------------------------- #

def test_run_cmd_includes_tmp_dir_and_batch_size(workspace, monkeypatch):
    _seed_manifest(workspace, 250)
    captured = {}
    _stub_engine(monkeypatch, captured)
    merge.run(_Handle(), False, "pkg")
    tmp_dir = _cmd_flag(captured["cmd"], "--tmp-dir")
    assert tmp_dir.startswith(str(workspace / "00_temp") + os.sep)
    assert _cmd_flag(captured["cmd"], "--merge-batch-size") == "100"


def test_run_logs_two_stage_plan(workspace, monkeypatch):
    _seed_manifest(workspace, 250)
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    merge.run(handle, False, "pkg")
    assert "两阶段合并：250 段 → 3 批（每批 100 段）→ 整书" in _log_msgs(handle)

    # a single batch (<= MERGE_BATCH_SIZE segments) needs no plan line
    _seed_manifest(workspace, 50, package="small")
    handle2 = _Handle()
    merge.run(handle2, False, "small")
    assert not any("两阶段合并" in m for m in _log_msgs(handle2))


def test_run_success_result(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    result = merge.run(handle, False, "pkg")
    assert result["file"] == "pkg.mp3"
    assert result["path"] == str(workspace / "06_audio_merge" / "pkg.mp3")
    assert result["segments"] == 120
    assert result["size"] == 2048
    assert Path(result["path"]).exists()
    assert handle.progresses and handle.progresses[-1][0] == 1.0


def test_run_cleans_tmp_dir_on_success(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured)
    merge.run(_Handle(), False, "pkg")
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


def test_run_cleans_tmp_dir_on_failure(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="fail")
    with pytest.raises(RuntimeError):
        merge.run(_Handle(), False, "pkg")
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


def test_run_relocates_wav_fallback(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="wav-fallback")
    result = merge.run(_Handle(), False, "pkg")
    target = workspace / "06_audio_merge" / "pkg.wav"
    assert result["file"] == "pkg.wav"
    assert result["path"] == str(target)
    assert target.exists()
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


# --------------------------------------------------------------------------- #
# run() — error paths & log plumbing (no regression)
# --------------------------------------------------------------------------- #

def test_run_missing_manifest_raises(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="未找到合成结果清单"):
        merge.run(_Handle(), False, "nope")


def test_run_empty_manifest_raises(workspace, monkeypatch):
    (workspace / "05_audio_chunk" / "pkg").mkdir(parents=True, exist_ok=True)
    (workspace / "05_audio_chunk" / "pkg" / "manifest.json").write_text("[]", encoding="utf-8")
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="manifest.json 为空"):
        merge.run(_Handle(), False, "pkg")


def test_run_no_ok_segments_raises(workspace, monkeypatch):
    _seed_manifest(workspace, 3, missing={0, 1, 2})  # ok=True but every file missing
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="没有可合并的音频"):
        merge.run(_Handle(), False, "pkg")


def test_run_skips_missing_files_with_warning(workspace, monkeypatch):
    _seed_manifest(workspace, 5, missing={1, 3})
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    result = merge.run(handle, False, "pkg")
    assert result["segments"] == 3
    assert any(level == "WARNING" and "2 段成功记录的文件缺失" in msg
               for level, msg in handle.logs)


def test_run_legacy_output_name(workspace, monkeypatch):
    _seed_manifest(workspace, 3, top_level=True)
    captured = {}
    _stub_engine(monkeypatch, captured)
    result = merge.run(_Handle())  # no package -> most recent / legacy top-level
    assert result["file"] == "cloned_audiobook.mp3"


def test_run_plain_lines_go_to_log(workspace, monkeypatch):
    _seed_manifest(workspace, 3)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="plain-lines")
    handle = _Handle()
    merge.run(handle, False, "pkg")
    assert "一条普通日志行" in _log_msgs(handle)
