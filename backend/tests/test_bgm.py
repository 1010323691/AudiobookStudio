"""Tests for the BGM engine (背景音乐系统 · 匹配链):

* ``engines.bgm`` pure functions (``sample_chapter_text`` / ``parse_analysis_reply`` /
  ``score_track`` / ``match_chapter`` — seeded rng, adjacent de-dup with staged
  relaxation, generic fallback, random mode) and ``build_mix_cmd`` (element-pinned,
  including the short-chapter fade clamp ``min(fade, duration/2)``);
* the two ``08_bgm`` JSON caches (missing-not-written / corrupt downgrade /
  ``write_bytes`` no CRLF / atomic save);
* ``match_stems`` (ordered pass, locked-skip, single-chapter re-match boundary,
  mode persistence, tag snapshots);
* ``analyze_chapter`` e2e (fake LLM: success updates only its own stem / 3 failures
  leave an empty-tag record / cancel-while-queued ≤1 s with zero writes / gate
  balance / model-empty fast-fail);
* ``mix_chapter`` e2e (fake Popen: success / rc≠0 / <1 KiB / cancel kills /
  narration missing / music missing / ``music=None`` copy2 byte-identical /
  ``merge_gate`` balance).

Everything is sandboxed like ``test_music.py`` (project root + music library dir
monkeypatched into a tmp dir); tasks run through the REAL TaskManager.
"""
from __future__ import annotations

import io
import json
import random
import threading
import time
from pathlib import Path

import types

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.core.tasks import TERMINAL, TaskStatus, get_task_manager
from backend.engines import bgm as bgm_engine
from backend.engines import music as music_engine

STEM = "第 001 章 测试"
STEM2 = "第 002 章 夜袭"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Throwaway project root + workspace (with 02 chapter files) + music library."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    (ws / "02_split_text").mkdir(parents=True)
    for stem in (STEM, STEM2):
        (ws / "02_split_text" / f"{stem}.txt").write_bytes(
            ("本章节内容。" * 40).encode("utf-8"))
    core_config.set_workspace_pointer(str(ws))
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("battle.mp3", "calm.mp3"):
        (lib / name).write_bytes(b"fake-music-bytes")
    # seed the library index with one tagged + one generic track
    music_engine.update_index(
        lambda idx: idx["tracks"].update({
            "battle.mp3": {"duration": 120.0, "enabled": True, "description": "",
                           "tags": {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                    "emotion": [], "custom": []},
                           "added_at": ""},
            "calm.mp3": {"duration": 90.0, "enabled": True, "description": "",
                         "tags": {"scene": [], "mood": [], "emotion": [], "custom": []},
                         "added_at": ""},
        }))
    mgr = get_task_manager()
    yield {"root": tmp_path, "ws": ws, "lib": lib, "mgr": mgr}
    # drain any leaked tasks / gate slots (cooperative cancel honours by the engine)
    for t in list(mgr.list()):
        if t.module in ("bgm-analysis", "bgm-mix") and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module in ("bgm-analysis", "bgm-mix") and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0 and concurrency.merge_gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"bgm tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0 and concurrency.merge_gate().active == 0
    core_config.reset_config_cache()


def _wait_terminal(mgr, tid: str, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = mgr.get(tid)
        if t.status in TERMINAL:
            return t.status
        time.sleep(0.02)
    raise AssertionError(f"task {tid} not terminal within {timeout}s")


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


# --------------------------------------------------------------------------- #
# sample_chapter_text
# --------------------------------------------------------------------------- #

def test_sample_short_text_is_verbatim():
    assert bgm_engine.sample_chapter_text("短文本", 100) == "短文本"
    # len == n exactly → the whole text (the ≤ boundary)
    assert bgm_engine.sample_chapter_text("12345", 5) == "12345"


def test_sample_long_text_three_windows():
    long = "头" + "a" * 20 + "中" + "b" * 20 + "尾" + "c" * 20
    s = bgm_engine.sample_chapter_text(long, 15)
    assert s.startswith("头aaaa") and s.endswith("ccccc")
    assert s.count("……") == 2
    # the sample stays bounded: three windows + two separators
    assert len(s) <= 15 + 2 * len("\n……\n")


def test_sample_zero_budget():
    # n = 0 → still goes the window path (50 > 0) with w = 1; the three
    # identical windows collapse to one via first-seen dedup.
    assert bgm_engine.sample_chapter_text("x" * 50, 0) == "x"
    # barely-longer text: w = max(1, n//3) = 1 → three 1-char windows
    s = bgm_engine.sample_chapter_text("abcdef", 4)  # len 6 > 4
    assert s == "a\n……\nc\n……\nf"


# --------------------------------------------------------------------------- #
# parse_analysis_reply
# --------------------------------------------------------------------------- #

def test_parse_analysis_reply_caps_and_dedup():
    p = bgm_engine.parse_analysis_reply(
        '{"scene": ["战斗", "战斗", "日常"], "mood": ["紧张", "热血", "压抑", "悲伤"], '
        '"emotion": ["愤怒", "孤独"], "custom": ["a", "a"]}'
    )
    assert p == {"scene": ["战斗", "日常"], "mood": ["紧张", "热血", "压抑"],
                 "emotion": ["愤怒", "孤独"], "custom": ["a"]}


def test_parse_analysis_reply_fenced_and_garbage():
    p = bgm_engine.parse_analysis_reply('```json\n{"scene": ["森林"], "mood": [], "emotion": [], "custom": []}\n```')
    assert p is not None and p["scene"] == ["森林"]
    assert bgm_engine.parse_analysis_reply("这不是 JSON") is None
    assert bgm_engine.parse_analysis_reply("[1, 2, 3]") is None  # arrays are not objects
    assert bgm_engine.parse_analysis_reply("null") is None


def test_parse_analysis_reply_missing_buckets_default_empty():
    p = bgm_engine.parse_analysis_reply('{"mood": ["紧张"]}')
    assert p == {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []}


# --------------------------------------------------------------------------- #
# score_track
# --------------------------------------------------------------------------- #

def test_score_track_weights_and_reason_pinned():
    s, r = bgm_engine.score_track(
        {"mood": ["紧张", "热血"], "scene": ["战斗"]},
        {"mood": ["紧张", "热血"], "scene": ["战斗"], "emotion": ["孤独"]},
    )
    assert s == 8  # 2×3 + 1×2
    assert r == "mood 命中 紧张, 热血(+6)；scene 命中 战斗(+2)"


def test_score_track_cross_bucket_names_do_not_hit():
    # 悲伤 lives in BOTH mood and emotion — bucket identity disambiguates.
    s, r = bgm_engine.score_track({"mood": ["悲伤"]}, {"emotion": ["悲伤"]})
    assert (s, r) == (0, "")
    s2, r2 = bgm_engine.score_track({"emotion": ["悲伤"]}, {"emotion": ["悲伤"]})
    assert (s2, r2) == (1, "emotion 命中 悲伤(+1)")


def test_score_track_custom_weight_and_garbage_input():
    s, r = bgm_engine.score_track({"custom": ["我的"]}, {"custom": ["我的", "别的"]})
    assert (s, r) == (1, "custom 命中 我的(+1)")
    assert bgm_engine.score_track(None, {}) == (0, "")
    assert bgm_engine.score_track({}, None) == (0, "")
    assert bgm_engine.score_track({"mood": ["紧张", "紧张"]}, {"mood": ["紧张"]}) == (3, "mood 命中 紧张(+3)")


# --------------------------------------------------------------------------- #
# match_chapter (seeded)
# --------------------------------------------------------------------------- #

def _track(name, enabled=True, **tags):
    base = {"scene": [], "mood": [], "emotion": [], "custom": []}
    base.update(tags)
    return {"enabled": enabled, "tags": base}


def test_match_chapter_highest_score_with_tie_random():
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),
        ("b.mp3", _track("b.mp3", mood=["紧张"])),
        ("c.mp3", _track("c.mp3", mood=["轻松"])),
    ]
    picks = {bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None,
                                      rng=random.Random(i))["music"] for i in range(20)}
    assert picks == {"a.mp3", "b.mp3"}  # the higher tier wins; ties are random
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None, rng=random.Random(7))
    assert res["via"] == "tags" and res["score"] == 3


def test_match_chapter_min_score_filter_falls_to_generic():
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),          # score 3 < 5
        ("g.mp3", _track("g.mp3")),                          # generic
    ]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 5, None, None, rng=random.Random(1))
    assert res == {"music": "g.mp3", "via": "generic", "score": 0,
                   "reason": "无标签命中，使用通用音乐"}


def test_match_chapter_enabled_only():
    tracks = [("a.mp3", _track("a.mp3", enabled=False, mood=["紧张"]))]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None, rng=random.Random(1))
    assert res["via"] == "none" and res["music"] is None


def test_match_chapter_adjacent_dedupe_then_relax():
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),
        ("b.mp3", _track("b.mp3", mood=["紧张"])),
    ]
    # one neighbour blocked → the other is picked (no relaxation note)
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, "a.mp3", None, rng=random.Random(3))
    assert res["music"] == "b.mp3" and "放宽" not in res["reason"]
    # both blocked → relax inside the top tier (never fall through to generic)
    res2 = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, "a.mp3", "b.mp3", rng=random.Random(3))
    assert res2["music"] in ("a.mp3", "b.mp3") and "放宽" in res2["reason"]
    # generic pool is de-duped the same way
    tracks2 = [("g.mp3", _track("g.mp3"))]
    res3 = bgm_engine.match_chapter({"mood": ["不存在"]}, tracks2, 1, "g.mp3", None, rng=random.Random(3))
    assert res3["music"] == "g.mp3" and "放宽" in res3["reason"]


def test_match_chapter_no_candidates():
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, [], 1, None, None, rng=random.Random(1))
    assert res["music"] is None and res["via"] == "none"
    assert "无候选" in res["reason"]


def test_match_chapter_random_mode():
    tracks = [("a.mp3", _track("a.mp3", mood=["紧张"])), ("b.mp3", _track("b.mp3"))]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None,
                                   rng=random.Random(0), mode="random")
    assert res["via"] == "random" and res["score"] == 0
    assert res["reason"].startswith("全章节随机")
    # random mode still de-dupes neighbours (both blocked → relaxed note)
    res2 = bgm_engine.match_chapter({}, tracks, 1, "a.mp3", "b.mp3",
                                    rng=random.Random(0), mode="random")
    assert res2["music"] in ("a.mp3", "b.mp3") and "放宽" in res2["reason"]
    # random mode with an empty enabled pool
    res3 = bgm_engine.match_chapter({}, [("x.mp3", _track("x.mp3", enabled=False))],
                                    1, None, None, rng=random.Random(0), mode="random")
    assert res3["music"] is None and res3["via"] == "none"


# --------------------------------------------------------------------------- #
# build_mix_cmd
# --------------------------------------------------------------------------- #

def _cfg(volume=0.18, fade_in=1.5, fade_out=3.0, loop=True):
    return types.SimpleNamespace(volume=volume, fade_in=fade_in,
                                 fade_out=fade_out, loop=loop)


def test_build_mix_cmd_default_shape():
    cfg = _cfg()
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 100.0, cfg, 60.0)
    assert cmd[:3] == ["ffmpeg", "-y", "-i"]
    assert cmd[3] == str(Path("n.mp3"))
    assert cmd[4:6] == ["-stream_loop", "-1"]
    assert cmd[6:8] == ["-i", str(Path("m.mp3"))]
    assert cmd[8] == "-filter_complex"
    fc = cmd[9]
    assert fc == (
        "[0:a]aresample=44100[nar];"
        "[1:a]aresample=44100,atrim=0:100.000,"
        "afade=t=in:d=1.500,afade=t=out:st=97.000:d=3.000,volume=0.18[bgm];"
        "[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0[out]"
    )
    assert cmd[10:] == ["-map", "[out]", "-c:a", "libmp3lame", str(Path("o.mp3"))]


def test_build_mix_cmd_no_loop():
    cfg = _cfg(volume=0.5, loop=False)
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 50.0, cfg, 30.0)
    assert cmd[4:6] == ["-stream_loop", "-0"]
    fc = cmd[9]
    assert "volume=0.5[bgm]" in fc and "st=47.000:d=3.000" in fc


def test_build_mix_cmd_short_chapter_fade_clamp():
    # duration 4 s: fade_out 3.0 → clamped to 2.0 (min(3.0, 4/2)); st = 4 − 2 = 2.
    cfg = _cfg()
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 4.0, cfg, 60.0)
    fc = cmd[9]
    assert "afade=t=in:d=1.500" in fc            # min(1.5, 2.0) = 1.5 (unchanged)
    assert "afade=t=out:st=2.000:d=2.000" in fc
    # extreme: 1 s chapter — both fades clamp to 0.5, st = 0.5 (never negative)
    cmd2 = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                    Path("o.mp3"), 1.0, cfg, 60.0)
    fc2 = cmd2[9]
    assert "afade=t=in:d=0.500" in fc2 and "afade=t=out:st=0.500:d=0.500" in fc2


# --------------------------------------------------------------------------- #
# 08_bgm JSON caches
# --------------------------------------------------------------------------- #

def test_analysis_missing_not_written(sandbox):
    layout = core_paths.get_layout()
    data = bgm_engine.load_analysis(layout)
    assert data == {"version": 1, "model": "", "chapters": {}}
    assert not (sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME).exists()
    data2 = bgm_engine.load_assignments(layout)
    assert data2["chapters"] == {} and data2["mode"] == "llm"


def test_analysis_corrupt_downgrades(sandbox):
    ws = sandbox["ws"] / "08_bgm"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / bgm_engine.ANALYSIS_NAME).write_bytes(b"{not json")
    layout = core_paths.get_layout()
    assert bgm_engine.load_analysis(layout)["chapters"] == {}
    (ws / bgm_engine.ASSIGNMENTS_NAME).write_bytes(b"[1, 2]")
    assert bgm_engine.load_assignments(layout)["chapters"] == {}


def test_analysis_save_roundtrip_no_crlf(sandbox):
    layout = core_paths.get_layout()
    data = bgm_engine.load_analysis(layout)
    data["model"] = "test-model"
    data["chapters"][STEM] = {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                              "custom": [], "analyzed_at": "t", "edited": False}
    bgm_engine.save_analysis(layout, data)
    raw = (sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME).read_bytes()
    assert b"\r\n" not in raw
    again = bgm_engine.load_analysis(layout)
    assert again["chapters"][STEM]["mood"] == ["紧张"]
    assert again["model"] == "test-model"


def test_list_chapter_stems(sandbox):
    layout = core_paths.get_layout()
    assert bgm_engine.list_chapter_stems(layout) == [STEM, STEM2]
    (sandbox["ws"] / "02_split_text" / "子目录").mkdir()
    (sandbox["ws"] / "02_split_text" / "notes.md").write_bytes(b"x")
    assert bgm_engine.list_chapter_stems(layout) == [STEM, STEM2]  # *.txt files only


# --------------------------------------------------------------------------- #
# match_stems
# --------------------------------------------------------------------------- #

def test_match_stems_llm_pass_prev_is_fresh_result(sandbox):
    # Both chapters match 战斗/紧张; the first pick excludes the second's → no repeat.
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    for stem in (STEM, STEM2):
        analysis["chapters"][stem] = {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                      "emotion": [], "custom": [], "analyzed_at": "t", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    res = bgm_engine.match_stems(layout, [STEM, STEM2], "llm", 1, rng=random.Random(11))
    assert res["mode"] == "llm" and res["matched"] == 2 and res["no_bgm"] == 0
    a1 = res["assignments"]["chapters"][STEM]
    a2 = res["assignments"]["chapters"][STEM2]
    # only battle.mp3 scores; the second chapter's neighbour (a1) is the SAME file →
    # blocked → relaxed (the only enabled track).
    assert a1["music"] == "battle.mp3" and a1["score"] == 8
    assert a2["music"] == "battle.mp3" and "放宽" in a2["reason"]
    # tag snapshot + bookkeeping
    assert a1["tags"]["mood"] == ["紧张", "热血"] and a1["manual"] is False and a1["locked"] is False
    assert a1["matched_at"]
    on_disk = bgm_engine.load_assignments(layout)
    assert on_disk["mode"] == "llm" and on_disk["chapters"][STEM2]["music"] == "battle.mp3"


def test_match_stems_locked_skipped_whole(sandbox):
    layout = core_paths.get_layout()
    # Pre-seed: STEM locked with calm.mp3, STEM2 fresh.
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "calm.mp3", "locked": True,
                              "manual": False, "score": None, "reason": "手动指定",
                              "matched_at": "old-t"}
    bgm_engine.save_assignments(layout, data)
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"][STEM2] = {"scene": [], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    res = bgm_engine.match_stems(layout, [STEM, STEM2], "llm", 1, rng=random.Random(1))
    assert res["skipped_locked"] == 1 and res["matched"] == 1
    locked = res["assignments"]["chapters"][STEM]
    assert locked["music"] == "calm.mp3" and locked["matched_at"] == "old-t"  # verbatim
    # the locked neighbour constrains STEM2: calm.mp3 is the generic-only pool? No —
    # STEM2 tags 紧张 → battle.mp3 scores; prev = calm.mp3 → battle is free.
    assert res["assignments"]["chapters"][STEM2]["music"] == "battle.mp3"


def test_match_stems_single_chapter_boundary_uses_existing_neighbours(sandbox):
    # Re-matching ONLY STEM2 must read prev/next from the EXISTING assignments and
    # never touch the neighbours' entries.
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"][STEM2] = {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "battle.mp3", "locked": False,
                              "manual": False, "score": 3, "reason": "…", "matched_at": "t1"}
    bgm_engine.save_assignments(layout, data)
    before = json.loads((sandbox["ws"] / "08_bgm" / bgm_engine.ASSIGNMENTS_NAME)
                        .read_bytes().decode("utf-8"))
    res = bgm_engine.match_stems(layout, [STEM2], "llm", 1, rng=random.Random(1))
    after = json.loads((sandbox["ws"] / "08_bgm" / bgm_engine.ASSIGNMENTS_NAME)
                       .read_bytes().decode("utf-8"))
    assert after["chapters"][STEM] == before["chapters"][STEM]  # neighbour untouched
    # prev = battle.mp3 → blocked; only enabled track → relaxed re-pick of battle.mp3
    assert res["assignments"]["chapters"][STEM2]["music"] == "battle.mp3"
    assert "放宽" in res["assignments"]["chapters"][STEM2]["reason"]


def test_match_stems_no_bgm_and_random_mode(sandbox):
    layout = core_paths.get_layout()
    # disable everything → every chapter gets music=None (still a matched entry)
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": False} for n, t in idx["tracks"].items()}))
    res = bgm_engine.match_stems(layout, [STEM], "llm", 1, rng=random.Random(1))
    assert res["no_bgm"] == 1 and res["matched"] == 0
    e = res["assignments"]["chapters"][STEM]
    assert e["music"] is None and e["matched_at"]  # matched_at distinguishes 判无 vs 从未
    # random mode over an empty enabled pool → also None
    res2 = bgm_engine.match_stems(layout, [STEM2], "random", 1, rng=random.Random(1))
    assert res2["assignments"]["chapters"][STEM2]["music"] is None
    assert res2["mode"] == "random"
    # re-enabling the library: random mode draws from ALL enabled tracks
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": True} for n, t in idx["tracks"].items()}))
    res3 = bgm_engine.match_stems(layout, [STEM], "random", 1, rng=random.Random(2))
    e3 = res3["assignments"]["chapters"][STEM]
    assert e3["music"] in ("battle.mp3", "calm.mp3")
    assert e3["score"] == 0 and "随机" in e3["reason"]


# --------------------------------------------------------------------------- #
# analyze_chapter e2e (fake LLM)
# --------------------------------------------------------------------------- #

def _fake_llm(reply, calls=None, fail_times=0):
    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        if calls is not None:
            calls.append(model)
        if fail_times:
            fail_times[0] -= 1
            raise RuntimeError("boom")
        return (reply, "stop", None)
    return fake


def test_analyze_success_updates_only_own_stem(sandbox, monkeypatch):
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_layout()
    # pre-seed another stem's entry — it must survive verbatim
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"][STEM2] = {"scene": ["森林"], "mood": [], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    monkeypatch.setattr(
        bgm_engine, "_llm_chat_completion",
        _fake_llm('{"scene": ["战斗"], "mood": ["紧张"], "emotion": [], "custom": []}'),
    )
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-analysis", f"章节气氛分析：{STEM}",
                     bgm_engine.analyze_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    data = bgm_engine.load_analysis(layout)
    assert data["model"] == "test-model"
    assert data["chapters"][STEM]["mood"] == ["紧张"] and data["chapters"][STEM]["edited"] is False
    assert data["chapters"][STEM2] == analysis["chapters"][STEM2]  # untouched
    assert concurrency.gate().active == 0  # slot released


def test_analyze_three_failures_leave_empty_record(sandbox, monkeypatch):
    core_config.update_config({"llm": {"model_name": "test-model"}})
    calls = []
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion",
                        _fake_llm("", calls=calls, fail_times=[3]))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-analysis", f"章节气氛分析：{STEM}",
                     bgm_engine.analyze_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"  # a chapter is never blocked
    assert len(calls) == 3
    entry = bgm_engine.load_analysis(core_paths.get_layout())["chapters"][STEM]
    assert entry == {"scene": [], "mood": [], "emotion": [], "custom": [],
                     "analyzed_at": entry["analyzed_at"], "edited": False}


def test_analyze_model_empty_fast_fail(sandbox):
    mgr = sandbox["mgr"]
    cfg = core_config.get_config()
    assert not cfg.llm.model_name
    tid = mgr.create("bgm-analysis", f"章节气氛分析：{STEM}",
                     bgm_engine.analyze_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "尚未配置 LLM 模型" in mgr.get(tid).error


def test_analyze_missing_chapter_file(sandbox):
    mgr = sandbox["mgr"]
    cfg = core_config.get_config()
    tid = mgr.create("bgm-analysis", "章节气氛分析：不存在",
                     bgm_engine.analyze_chapter, "不存在", cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "未找到章节文件" in mgr.get(tid).error


def test_analyze_cancel_while_queued(sandbox, monkeypatch):
    core_config.update_config({"llm": {"model_name": "test-model"}})
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", _fake_llm("{}"))
    g = concurrency.gate()
    concurrency.set_concurrency(1)
    g.acquire()  # the test holds the only slot
    try:
        cfg = core_config.get_config()
        mgr = sandbox["mgr"]
        t0 = time.time()
        tid = mgr.create("bgm-analysis", f"章节气氛分析：{STEM}",
                         bgm_engine.analyze_chapter, STEM, cfg.llm, cfg.bgm).id
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.RUNNING, timeout=3)
        mgr.control(tid, "cancel")
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.CANCELLED, timeout=3)
        assert time.time() - t0 < 2.5  # one stop_check poll (0.2 s) + overhead
        assert g.active == 1  # the worker never took the slot (no release underflow)
        assert not (sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME).exists()  # 零落盘
    finally:
        g.release()
        assert g.active == 0


# --------------------------------------------------------------------------- #
# mix_chapter e2e (fake Popen)
# --------------------------------------------------------------------------- #

def _seed_mix_inputs(sandbox, stem=STEM, music="battle.mp3", narration_bytes=b"NARR" * 256):
    ws = sandbox["ws"]
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (ws / "06_audio_merge" / f"{stem}.mp3").write_bytes(narration_bytes)
    data = bgm_engine.load_assignments(core_paths.get_layout())
    data["chapters"][stem] = {"tags": {}, "music": music, "locked": False,
                              "manual": False, "score": 3, "reason": "r", "matched_at": "t"}
    bgm_engine.save_assignments(core_paths.get_layout(), data)


class _FakeProc:
    def __init__(self, cmd, out_path: Path, out_size: int = 4096, rc: int = 0):
        self.cmd = cmd
        self._rc = rc
        self.killed = False
        self.stderr = io.BytesIO(b"fake stderr tail" if rc else b"")
        if rc == 0 and out_size:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"A" * out_size)

    def poll(self):
        return self._rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._rc


def test_mix_success_and_gate_balance(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    procs = []

    def fake_popen(cmd, **kw):
        p = _FakeProc(cmd, core_paths.get_layout().bgm / f"{STEM}.mp3")
        procs.append(p)
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert t.result["music"] == "battle.mp3" and t.result["duration"] == 100.0
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.exists() and out.stat().st_size == 4096
    assert procs[0].cmd[4:6] == ["-stream_loop", "-1"]  # the real command shape reached Popen
    assert concurrency.merge_gate().active == 0


def test_mix_rc_nonzero_fails_with_stderr(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    monkeypatch.setattr(bgm_engine.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(cmd, core_paths.get_layout().bgm / f"{STEM}.mp3", rc=1))
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    err = mgr.get(tid).error
    assert "退出码 1" in err and "fake stderr tail" in err
    assert concurrency.merge_gate().active == 0


def test_mix_output_too_small_fails(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    monkeypatch.setattr(bgm_engine.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(cmd, core_paths.get_layout().bgm / f"{STEM}.mp3", out_size=100))
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "输出异常" in mgr.get(tid).error


def test_mix_cancel_kills_process(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    started = threading.Event()

    class SlowProc(_FakeProc):
        def poll(self):
            if self.killed:
                return 1
            started.set()
            return None  # still running

    def fake_popen(cmd, **kw):
        p = SlowProc(cmd, core_paths.get_layout().bgm / f"{STEM}.mp3")
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    g = concurrency.merge_gate()
    concurrency.set_merge_concurrency(1)
    g.acquire()  # hold the only mix slot so the task parks in the cooperative wait
    try:
        cfg = core_config.get_config()
        mgr = sandbox["mgr"]
        tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                         bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.RUNNING, timeout=3)
        mgr.control(tid, "cancel")
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.CANCELLED, timeout=3)
        assert g.active == 1  # queued worker took no slot
        assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()
    finally:
        g.release()


def test_mix_narration_missing(sandbox):
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "battle.mp3", "locked": False,
                              "manual": False, "score": 3, "reason": "r", "matched_at": "t"}
    bgm_engine.save_assignments(layout, data)
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "未找到旁白音频" in mgr.get(tid).error


def test_mix_music_missing(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox, music="ghost.mp3")  # not in the library
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "音乐库中找不到 ghost.mp3" in mgr.get(tid).error
    assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()


def test_mix_music_none_copies_narration_verbatim(sandbox):
    _seed_mix_inputs(sandbox, music=None, narration_bytes=b"VERBATIM-BYTES-123")
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.read_bytes() == b"VERBATIM-BYTES-123"  # 字节一致
    assert mgr.get(tid).result["music"] is None


def test_mix_never_matched_fails(sandbox):
    (sandbox["ws"] / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (sandbox["ws"] / "06_audio_merge" / f"{STEM}.mp3").write_bytes(b"NARR")
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "从未匹配" in mgr.get(tid).error
