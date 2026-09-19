"""Tests for the global music library (背景音乐系统 · 音乐库):

* ``engines.music`` pure functions (tag normalisation / generic detection /
  rename-delete propagation / name guard) and index IO (missing-not-written /
  corrupt downgrade / ``write_bytes`` no CRLF / atomic update transactions /
  concurrent read-modify-write without lost updates);
* ``api.music`` endpoints called directly (no HTTP layer): upload 409/400,
  preview traversal 400, locked-reference delete skip, batch ops, tag
  management with analysis-cache propagation, and suggest-tags (fake LLM:
  in-vocabulary filtering, caps, 400 model-empty, 502 failure);
* the AI suggestion cache (``music_tag_suggestions.json`` — candidates only):
  IO (missing-not-written / corrupt downgrade / ``write_bytes`` no CRLF /
  atomic abort), prompt/parse pure functions, ``clear_suggestion`` semantics,
  ``/library`` orphan filtering, and tag-decision consumption;
* ``suggest_track_tags`` worker e2e (fake LLM, real TaskManager + shared LLM
  gate): success writes only its own candidate entry / 2-fail task-failed /
  model-empty fast-fail / missing file / cancel-while-queued zero writes.

The library dir is a project-root constant (``core_paths.MUSIC_LIBRARY_DIR``),
so every filesystem test monkeypatches it into a sandbox — the engine reads it
at call time, which is what makes the monkeypatch take effect.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from backend.api import music as api_music
from backend.core import concurrency
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.tasks import TERMINAL, TaskStatus, get_task_manager
from backend.engines import music as music_engine


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #

@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Throwaway project root + workspace + music library dir."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    ws.mkdir(parents=True)
    core_config.set_workspace_pointer(str(ws))
    gate_limit = concurrency.gate().limit
    yield {"root": tmp_path, "ws": ws, "lib": tmp_path / "music_library"}
    # The suggest_track_tags worker tests run real tasks on the shared LLM gate:
    # cancel any still-active AI-tag task and drain the gate before the next test.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module == "music-ai-tags" and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    stuck = []
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module == "music-ai-tags" and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"music-ai-tags tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0
    concurrency.set_concurrency(gate_limit)
    core_config.reset_config_cache()


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


def _wait_terminal(mgr, tid: str, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = mgr.get(tid)
        if t.status in TERMINAL:
            return t.status
        time.sleep(0.02)
    raise AssertionError(f"task {tid} not terminal within {timeout}s")


def _upload(name: str, data: bytes = b"fake-audio-bytes") -> dict:
    """Upload a (fake) music file directly through the endpoint."""
    uf = UploadFile(filename=name, file=io.BytesIO(data))
    return asyncio.run(api_music.upload_track(file=uf))


def _write_analysis(ws: Path, data: dict) -> Path:
    d = ws / "08_bgm"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "chapter_music_analysis.json"
    f.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return f


def _write_assignments(ws: Path, data: dict) -> Path:
    d = ws / "08_bgm"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "bgm_assignments.json"
    f.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return f


# --------------------------------------------------------------------------- #
# pure functions
# --------------------------------------------------------------------------- #

def test_normalize_track_tags_in_vocabulary_and_order():
    registry = music_engine._default_index()["tags"]
    raw = {"scene": ["战斗", "日常"], "mood": ["紧张"], "emotion": [], "custom": []}
    out = music_engine.normalize_track_tags(raw, registry)
    assert out == {
        "scene": ["战斗", "日常"], "mood": ["紧张"], "emotion": [], "custom": [],
    }


def test_normalize_track_tags_out_of_vocabulary_folds_to_custom():
    registry = music_engine._default_index()["tags"]
    raw = {"scene": ["不存在的标签", "战斗"], "mood": ["也不存在"], "custom": ["我的标签"]}
    out = music_engine.normalize_track_tags(raw, registry)
    assert out["scene"] == ["战斗"]
    assert out["mood"] == []
    # non-custom out-of-vocab names + the client's own custom list, first-seen order
    assert out["custom"] == ["不存在的标签", "也不存在", "我的标签"]


def test_normalize_track_tags_dedup_and_garbage():
    registry = music_engine._default_index()["tags"]
    raw = {"scene": ["战斗", "战斗", " 战斗 "], "mood": [None, 5, "", "紧张"], "custom": "bad"}
    out = music_engine.normalize_track_tags(raw, registry)
    assert out["scene"] == ["战斗"]
    assert out["mood"] == ["紧张"]
    assert out["custom"] == []


def test_normalize_track_tags_non_dict_input():
    registry = music_engine._default_index()["tags"]
    assert music_engine.normalize_track_tags(None, registry) == {c: [] for c in music_engine.TAG_CATEGORIES}
    assert music_engine.normalize_track_tags("x", registry) == {c: [] for c in music_engine.TAG_CATEGORIES}


def test_is_generic_track():
    empty = music_engine._empty_track_tags()
    assert music_engine.is_generic_track({"enabled": True, "tags": empty}) is True
    assert music_engine.is_generic_track({"enabled": False, "tags": empty}) is False
    assert music_engine.is_generic_track({"enabled": True, "tags": {**empty, "mood": ["紧张"]}}) is False
    assert music_engine.is_generic_track({"enabled": True}) is False
    # whitespace-only tags count as empty
    assert music_engine.is_generic_track(
        {"enabled": True, "tags": {**empty, "custom": ["  "]}}
    ) is True


def test_find_tag_category_first_bucket_wins():
    registry = music_engine._default_index()["tags"]
    # 悲伤 is registered in BOTH mood and emotion (grandfathered) — mood comes first
    assert music_engine.find_tag_category(registry, "悲伤") == "mood"
    assert music_engine.find_tag_category(registry, "战斗") == "scene"
    assert music_engine.find_tag_category(registry, "未注册") is None


def test_all_tag_names():
    registry = music_engine._default_index()["tags"]
    names = music_engine.all_tag_names(registry)
    assert "悲伤" in names and "战斗" in names
    assert len(names) == sum(len(v) for v in registry.values()) - 1  # 悲伤 counted twice


def _index_with_tracks() -> dict:
    idx = music_engine._default_index()
    idx["tracks"] = {
        "a.mp3": {"duration": 1.0, "enabled": True, "description": "",
                  "tags": {"scene": ["战斗"], "mood": ["紧张"], "emotion": [], "custom": []},
                  "added_at": ""},
        "b.mp3": {"duration": 2.0, "enabled": True, "description": "",
                  "tags": {"scene": [], "mood": ["紧张"], "emotion": ["愤怒"], "custom": []},
                  "added_at": ""},
        "c.mp3": {"duration": 3.0, "enabled": True, "description": "",
                  "tags": {"scene": [], "mood": [], "emotion": [], "custom": []},
                  "added_at": ""},
    }
    return idx


def test_apply_tag_rename_propagates():
    idx = _index_with_tracks()
    affected = music_engine.apply_tag_rename(idx, "mood", "紧张", "紧绷")
    assert affected == 2
    assert "紧绷" in idx["tags"]["mood"] and "紧张" not in idx["tags"]["mood"]
    assert idx["tracks"]["a.mp3"]["tags"]["mood"] == ["紧绷"]
    assert idx["tracks"]["b.mp3"]["tags"]["mood"] == ["紧绷"]
    assert idx["tracks"]["c.mp3"]["tags"]["mood"] == []  # untouched track unchanged
    # other buckets untouched
    assert idx["tracks"]["a.mp3"]["tags"]["scene"] == ["战斗"]


def test_apply_tag_rename_absent_old_is_noop():
    idx = _index_with_tracks()
    assert music_engine.apply_tag_rename(idx, "mood", "不存在", "x") == 0


def test_apply_tag_delete_propagates():
    idx = _index_with_tracks()
    affected = music_engine.apply_tag_delete(idx, "emotion", "愤怒")
    assert affected == 1
    assert "愤怒" not in idx["tags"]["emotion"]
    assert idx["tracks"]["b.mp3"]["tags"]["emotion"] == []
    assert idx["tracks"]["a.mp3"]["tags"]["emotion"] == []


def test_validate_music_name():
    assert music_engine.validate_music_name("calm_01.mp3") == "calm_01.mp3"
    assert music_engine.validate_music_name("drums.WAV") == "drums.WAV"
    for bad in ("", " ", "../evil.mp3", "a/b.mp3", "noext", "song.flac", "song.mp3.txt", None):
        with pytest.raises(ValueError):
            music_engine.validate_music_name(bad)


# --------------------------------------------------------------------------- #
# index IO
# --------------------------------------------------------------------------- #

def test_load_index_missing_is_not_written(sandbox):
    idx = music_engine.load_index()
    assert idx == music_engine._default_index()
    assert not (sandbox["lib"] / "music_index.json").exists()  # reads never write


def test_load_index_corrupt_degrades_to_default(sandbox):
    d = sandbox["lib"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "music_index.json").write_bytes(b"{not json")
    assert music_engine.load_index() == music_engine._default_index()
    (d / "music_index.json").write_bytes(b"[1, 2, 3]")
    assert music_engine.load_index() == music_engine._default_index()


def test_coerce_index_degrades_corrupt_shapes():
    data = {
        "version": 1,
        "tags": {"scene": ["战斗", 42, "  "], "mood": "not-a-list", "unknown_cat": ["x"]},
        "tracks": {
            "a.mp3": {"duration": "bad", "enabled": "no", "description": 7,
                      "tags": {"scene": ["战斗"]}, "added_at": 3},
            "b.mp3": "not-a-dict",
        },
    }
    idx = music_engine._coerce_index(data)
    assert idx["tags"]["scene"] == ["战斗"]
    assert idx["tags"]["mood"] == music_engine.DEFAULT_TAGS["mood"]  # fallback default
    assert "unknown_cat" not in idx["tags"]
    assert idx["tracks"]["a.mp3"]["duration"] == 0.0
    assert idx["tracks"]["a.mp3"]["enabled"] is True
    assert idx["tracks"]["a.mp3"]["description"] == ""
    assert idx["tracks"]["a.mp3"]["tags"]["scene"] == ["战斗"]
    assert idx["tracks"]["a.mp3"]["added_at"] == ""
    assert "b.mp3" not in idx["tracks"]


def test_save_index_write_bytes_no_crlf(sandbox):
    idx = music_engine._default_index()
    idx["tracks"]["战斗曲.mp3"] = {"duration": 1.0, "enabled": True, "description": "中文描述",
                                   "tags": music_engine._empty_track_tags(), "added_at": ""}
    music_engine.save_index(idx)
    p = sandbox["lib"] / "music_index.json"
    raw = p.read_bytes()
    assert b"\r\n" not in raw  # write_bytes — no Windows newline translation
    data = json.loads(raw.decode("utf-8"))
    assert data["tracks"]["战斗曲.mp3"]["description"] == "中文描述"


def test_update_index_mutator_abort_writes_nothing(sandbox):
    def boom(idx):
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        music_engine.update_index(boom)
    assert not (sandbox["lib"] / "music_index.json").exists()

    # pre-existing index is left byte-identical
    music_engine.save_index(music_engine._default_index())
    first = (sandbox["lib"] / "music_index.json").read_bytes()
    with pytest.raises(RuntimeError):
        music_engine.update_index(boom)
    assert (sandbox["lib"] / "music_index.json").read_bytes() == first


def test_update_index_concurrent_no_lost_updates(sandbox):
    def add(i: int):
        def _m(idx):
            idx["tracks"][f"t{i:02d}.mp3"] = {
                "duration": 1.0, "enabled": True, "description": "",
                "tags": music_engine._empty_track_tags(), "added_at": "",
            }
        music_engine.update_index(_m)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(add, range(8)))
    assert len(music_engine.load_index()["tracks"]) == 8


# --------------------------------------------------------------------------- #
# API: upload / preview / track edits
# --------------------------------------------------------------------------- #

def test_upload_success_and_fields(sandbox):
    r = _upload("a.mp3")
    assert r["name"] == "a.mp3"
    tr = r["track"]
    assert tr["enabled"] is True
    assert tr["tags"] == music_engine._empty_track_tags()
    assert tr["duration"] == 0.0  # fake bytes -> probe failure -> 0 (non-blocking)
    assert (sandbox["lib"] / "a.mp3").exists()
    assert "a.mp3" in music_engine.load_index()["tracks"]


def test_upload_same_name_409_and_no_overwrite(sandbox):
    _upload("a.mp3", b"original")
    with pytest.raises(HTTPException) as ei:
        _upload("a.mp3", b"replacement")
    assert ei.value.status_code == 409
    assert "a.mp3" in str(ei.value.detail)
    assert (sandbox["lib"] / "a.mp3").read_bytes() == b"original"  # never overwritten


def test_upload_bad_name_or_empty_400(sandbox):
    with pytest.raises(HTTPException) as ei:
        _upload("song.flac")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        _upload("empty.mp3", b"")
    assert ei.value.status_code == 400


def test_preview_ok_and_errors(sandbox):
    _upload("a.mp3")
    resp = api_music.preview_track("a.mp3")
    assert resp.media_type == "audio/mpeg"
    with pytest.raises(HTTPException) as ei:
        api_music.preview_track("../a.mp3")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        api_music.preview_track("missing.mp3")
    assert ei.value.status_code == 404


def test_update_track_partial_and_out_of_vocab_fold(sandbox):
    _upload("a.mp3")
    r = api_music.update_track(
        "a.mp3",
        api_music.TrackUpdate(tags={"scene": ["战斗", "编造的"], "mood": ["紧张"]}, enabled=False),
    )
    tr = r["track"]
    assert tr["enabled"] is False
    assert tr["tags"]["scene"] == ["战斗"]
    assert tr["tags"]["mood"] == ["紧张"]
    assert tr["tags"]["custom"] == ["编造的"]  # out-of-vocab folded
    # description untouched by this patch
    assert tr["description"] == ""
    with pytest.raises(HTTPException) as ei:
        api_music.update_track("missing.mp3", api_music.TrackUpdate(enabled=True))
    assert ei.value.status_code == 404


def test_delete_track_unlocked_reference_does_not_block(sandbox):
    _upload("a.mp3")
    _write_assignments(
        sandbox["ws"],
        {"version": 1, "mode": "llm", "chapters": {
            "第 001 章 测试": {"tags": {}, "music": "a.mp3", "locked": False,
                              "manual": False, "score": 3, "reason": "", "matched_at": ""}}},
    )
    r = api_music.delete_track("a.mp3")
    assert r == {"deleted": ["a.mp3"], "skipped": [], "missing": []}
    assert not (sandbox["lib"] / "a.mp3").exists()


def test_delete_track_locked_reference_skipped(sandbox):
    _upload("a.mp3")
    _write_assignments(
        sandbox["ws"],
        {"version": 1, "mode": "llm", "chapters": {
            "第 001 章 测试": {"tags": {}, "music": "a.mp3", "locked": True,
                              "manual": False, "score": 3, "reason": "", "matched_at": ""}}},
    )
    r = api_music.delete_track("a.mp3")
    assert r["deleted"] == []
    assert len(r["skipped"]) == 1
    assert r["skipped"][0]["name"] == "a.mp3"
    assert "被 1 章锁定引用" in r["skipped"][0]["reason"]
    assert "第 001 章 测试" in r["skipped"][0]["reason"]
    assert (sandbox["lib"] / "a.mp3").exists()  # file stays


# --------------------------------------------------------------------------- #
# API: batch ops
# --------------------------------------------------------------------------- #

def test_batch_enable(sandbox):
    _upload("a.mp3")
    _upload("b.wav")
    with pytest.raises(HTTPException) as ei:
        api_music.batch_enable(api_music.BatchNames(names=[], enabled=True))
    assert ei.value.status_code == 400
    r = api_music.batch_enable(api_music.BatchNames(names=["a.mp3", "ghost.mp3"], enabled=False))
    assert r["updated"] == 1
    assert r["missing"] == ["ghost.mp3"]
    idx = music_engine.load_index()
    assert idx["tracks"]["a.mp3"]["enabled"] is False
    assert idx["tracks"]["b.wav"]["enabled"] is True


def test_batch_tags_new_contract(sandbox):
    _upload("a.mp3")
    _upload("b.wav")
    # guards
    with pytest.raises(HTTPException) as ei:
        api_music.batch_tags(api_music.BatchTags(tracks=[], names=["紧张"], category="mood"))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        api_music.batch_tags(api_music.BatchTags(tracks=["a.mp3"], names=["紧张"], category="bogus"))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        api_music.batch_tags(api_music.BatchTags(tracks=["a.mp3"], names=[" "], category="mood"))
    assert ei.value.status_code == 400
    # add (deduped) to the SELECTED track only
    r = api_music.batch_tags(api_music.BatchTags(tracks=["a.mp3"], names=["紧张"], category="mood", op="add"))
    assert r["updated"] == 1 and r["missing"] == []
    idx = music_engine.load_index()
    assert idx["tracks"]["a.mp3"]["tags"]["mood"] == ["紧张"]
    assert idx["tracks"]["b.wav"]["tags"]["mood"] == []
    # add again -> idempotent
    api_music.batch_tags(api_music.BatchTags(tracks=["a.mp3"], names=["紧张"], category="mood"))
    assert music_engine.load_index()["tracks"]["a.mp3"]["tags"]["mood"] == ["紧张"]
    # remove
    api_music.batch_tags(api_music.BatchTags(tracks=["a.mp3"], names=["紧张"], category="mood", op="remove"))
    assert music_engine.load_index()["tracks"]["a.mp3"]["tags"]["mood"] == []


def test_batch_delete_shared_skip_semantics(sandbox):
    _upload("a.mp3")
    _upload("b.mp3")
    _write_assignments(
        sandbox["ws"],
        {"version": 1, "mode": "llm", "chapters": {
            "第 001 章 测试": {"tags": {}, "music": "a.mp3", "locked": True,
                              "manual": False, "score": 3, "reason": "", "matched_at": ""}}},
    )
    with pytest.raises(HTTPException) as ei:
        api_music.batch_delete(api_music.BatchNames(names=[]))
    assert ei.value.status_code == 400
    r = api_music.batch_delete(
        api_music.BatchNames(names=["a.mp3", "b.mp3", "../evil.mp3", "ghost.mp3"])
    )
    assert r["deleted"] == ["b.mp3"]
    assert [s["name"] for s in r["skipped"]] == ["a.mp3"]  # locked ref, per item, not aborting
    # names that cannot exist in the index (traversal / unknown) are reported missing
    assert r["missing"] == ["../evil.mp3", "ghost.mp3"]
    assert (sandbox["lib"] / "a.mp3").exists()
    assert not (sandbox["lib"] / "b.mp3").exists()


# --------------------------------------------------------------------------- #
# API: tag management (+ analysis-cache propagation)
# --------------------------------------------------------------------------- #

def test_create_tag_global_uniqueness_409(sandbox):
    r = api_music.create_tag(api_music.TagCreate(category="custom", name="夜雨"))
    assert "夜雨" in r["tags"]["custom"]
    with pytest.raises(HTTPException) as ei:
        api_music.create_tag(api_music.TagCreate(category="scene", name="夜雨"))
    assert ei.value.status_code == 409  # name exists in ANY bucket
    with pytest.raises(HTTPException) as ei:
        api_music.create_tag(api_music.TagCreate(category="scene", name="战斗"))
    assert ei.value.status_code == 409  # built-in vocab
    with pytest.raises(HTTPException) as ei:
        api_music.create_tag(api_music.TagCreate(category="bogus", name="x"))
    assert ei.value.status_code == 400


def test_rename_tag_propagates_tracks_and_analysis(sandbox):
    _upload("a.mp3")
    api_music.update_track(
        "a.mp3", api_music.TrackUpdate(tags={"mood": ["紧张"], "scene": ["战斗"]})
    )
    analysis = _write_analysis(
        sandbox["ws"],
        {"version": 1, "chapters": {
            "第 001 章 夜袭": {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                              "custom": [], "analyzed_at": "", "edited": False},
            "第 002 章 无关": {"scene": [], "mood": ["平静"], "emotion": [],
                              "custom": [], "analyzed_at": "", "edited": False}}},
    )
    assignments = _write_assignments(
        sandbox["ws"],
        {"version": 1, "mode": "llm", "chapters": {
            "第 001 章 夜袭": {"tags": {"mood": ["紧张"]}, "music": "a.mp3", "locked": False,
                              "manual": False, "score": 3, "reason": "mood 命中 紧张(+3)",
                              "matched_at": ""}}},
    )
    with pytest.raises(HTTPException) as ei:
        api_music.rename_tag(api_music.TagRename(category="mood", name="不存在", new_name="x"))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        api_music.rename_tag(api_music.TagRename(category="mood", name="紧张", new_name="战斗"))
    assert ei.value.status_code == 409  # new name already exists (scene bucket)

    r = api_music.rename_tag(api_music.TagRename(category="mood", name="紧张", new_name="紧绷"))
    assert r["affected_tracks"] == 1
    assert "紧绷" in r["tags"]["mood"]

    idx = music_engine.load_index()
    assert idx["tracks"]["a.mp3"]["tags"]["mood"] == ["紧绷"]
    data = json.loads(analysis.read_bytes().decode("utf-8"))
    assert data["chapters"]["第 001 章 夜袭"]["mood"] == ["紧绷"]
    assert data["chapters"]["第 002 章 无关"]["mood"] == ["平静"]
    # assignments snapshots are a HISTORICAL record — NOT rewritten
    assert json.loads(assignments.read_bytes().decode("utf-8"))["chapters"]["第 001 章 夜袭"]["tags"] == {
        "mood": ["紧张"]
    }


def test_rename_tag_same_name_noop(sandbox):
    r = api_music.rename_tag(api_music.TagRename(category="mood", name="紧张", new_name="紧张"))
    assert "紧张" in r["tags"]["mood"]


def test_delete_tag_propagates_and_keeps_files(sandbox):
    _upload("a.mp3")
    api_music.update_track("a.mp3", api_music.TrackUpdate(tags={"mood": ["紧张"]}))
    analysis = _write_analysis(
        sandbox["ws"],
        {"version": 1, "chapters": {
            "第 001 章 夜袭": {"scene": [], "mood": ["紧张"], "emotion": [],
                              "custom": [], "analyzed_at": "", "edited": False}}},
    )
    with pytest.raises(HTTPException) as ei:
        api_music.delete_tag("mood", "不存在")
    assert ei.value.status_code == 404
    r = api_music.delete_tag("mood", "紧张")
    assert "紧张" not in r["tags"]["mood"]
    idx = music_engine.load_index()
    assert idx["tracks"]["a.mp3"]["tags"]["mood"] == []
    data = json.loads(analysis.read_bytes().decode("utf-8"))
    assert data["chapters"]["第 001 章 夜袭"]["mood"] == []
    assert (sandbox["lib"] / "a.mp3").exists()  # 删除标签绝不删音乐文件


# --------------------------------------------------------------------------- #
# API: suggest-tags (fake LLM)
# --------------------------------------------------------------------------- #

def _fake_llm(reply):
    calls = {"n": 0}

    def _call(*args, **kwargs):
        calls["n"] += 1
        if isinstance(reply, Exception):
            raise reply
        return reply, "stop", None

    return _call, calls


def test_suggest_tags_model_empty_400(sandbox):
    _upload("a.mp3")
    with pytest.raises(HTTPException) as ei:
        api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3"))
    assert ei.value.status_code == 400
    assert "model_name" in str(ei.value.detail)


def test_suggest_tags_missing_track_404(sandbox):
    core_config.update_config({"llm": {"model_name": "test-model"}})
    with pytest.raises(HTTPException) as ei:
        api_music.suggest_tags(api_music.SuggestTagsReq(name="missing.mp3"))
    assert ei.value.status_code == 404


def test_suggest_tags_filters_to_vocabulary_and_caps(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    reply = (
        '```json\n'
        '{"scene": ["战斗", "编造的"], "mood": ["紧张", "热血", "史诗", "恐怖"], '
        '"emotion": ["愤怒", "希望", "喜悦"]}\n'
        '```'
    )
    fake, calls = _fake_llm(reply)
    monkeypatch.setattr(api_music, "_llm_chat_completion", fake)
    r = api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3", description="激烈的鼓点"))
    assert calls["n"] == 1
    # out-of-vocabulary dropped; per-bucket caps scene≤2 / mood≤3 / emotion≤2 (first-seen order)
    assert r["tags"]["scene"] == ["战斗"]
    assert r["tags"]["mood"] == ["紧张", "热血", "史诗"]
    assert r["tags"]["emotion"] == ["愤怒", "希望"]


def test_suggest_tags_retry_then_success(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})

    seq = {"i": 0}

    def _flaky(*args, **kwargs):
        seq["i"] += 1
        if seq["i"] == 1:
            raise RuntimeError("connection reset")
        return '{"scene": ["战斗"]}', "stop", None

    monkeypatch.setattr(api_music, "_llm_chat_completion", _flaky)
    r = api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3"))
    assert seq["i"] == 2
    assert r["tags"]["scene"] == ["战斗"]


def test_suggest_tags_all_fail_502(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    fake, calls = _fake_llm(RuntimeError("boom"))
    monkeypatch.setattr(api_music, "_llm_chat_completion", fake)
    with pytest.raises(HTTPException) as ei:
        api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3"))
    assert ei.value.status_code == 502
    assert calls["n"] == 2
    assert "boom" in str(ei.value.detail)

    # unparseable replies (both attempts) -> generic 502
    fake2, calls2 = _fake_llm("这不是 JSON")
    monkeypatch.setattr(api_music, "_llm_chat_completion", fake2)
    with pytest.raises(HTTPException) as ei:
        api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3"))
    assert ei.value.status_code == 502
    assert calls2["n"] == 2
    assert str(ei.value.detail) == "AI 推荐失败，请手动打标。"


def test_suggest_tags_non_dict_reply_502(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    fake, _calls = _fake_llm('["scene", "战斗"]')
    monkeypatch.setattr(api_music, "_llm_chat_completion", fake)
    with pytest.raises(HTTPException) as ei:
        api_music.suggest_tags(api_music.SuggestTagsReq(name="a.mp3"))
    assert ei.value.status_code == 502


# --------------------------------------------------------------------------- #
# AI suggestion prompts / parsing (shared by the sync endpoint and the worker)
# --------------------------------------------------------------------------- #

def test_build_suggestion_prompts_pinned():
    registry = music_engine._default_index()["tags"]
    system, user = music_engine.build_suggestion_prompts("battle_01", "激烈的鼓点", registry)
    assert "只能从词表中选择，禁止创造新词" in system
    assert "scene 最多 2 个、mood 最多 3 个、emotion 最多 2 个" in system
    # the file STEM (no extension) + description feed the user prompt
    assert user.startswith("文件名：battle_01\n用户描述：激烈的鼓点")
    assert "词表：" in user
    assert "scene: 日常、战斗、冒险" in user  # registry order, 、-joined
    assert "mood: 轻松、温馨" in user
    assert "emotion: 希望、喜悦" in user
    # empty description -> （无）
    _, user2 = music_engine.build_suggestion_prompts("calm_01", "", registry)
    assert "用户描述：（无）" in user2


def test_parse_suggestion_reply_filters_caps_and_dedup():
    registry = music_engine._default_index()["tags"]
    reply = (
        '```json\n'
        '{"scene": [" 战斗 ", "战斗", "编造的"], "mood": ["紧张", "热血", "史诗", "恐怖"], '
        '"emotion": ["愤怒", "希望", "喜悦"]}\n'
        '```'
    )
    out = music_engine.parse_suggestion_reply(reply, registry)
    assert out == {
        "scene": ["战斗"],           # stripped + deduped, out-of-vocab dropped
        "mood": ["紧张", "热血", "史诗"],   # cap 3 (恐怖 dropped, first-seen order)
        "emotion": ["愤怒", "希望"],       # cap 2
    }


def test_parse_suggestion_reply_non_object_is_none():
    registry = music_engine._default_index()["tags"]
    assert music_engine.parse_suggestion_reply("[1, 2]", registry) is None
    assert music_engine.parse_suggestion_reply("这不是 JSON", registry) is None
    assert music_engine.parse_suggestion_reply('"只是一个字符串"', registry) is None


# --------------------------------------------------------------------------- #
# suggestions cache (music_tag_suggestions.json — candidates only)
# --------------------------------------------------------------------------- #

def test_load_suggestions_missing_not_written(sandbox):
    assert music_engine.load_suggestions() == {"version": 1, "tracks": {}}
    assert not (sandbox["lib"] / "music_tag_suggestions.json").exists()  # reads never write


def test_load_suggestions_corrupt_degrades(sandbox):
    d = sandbox["lib"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "music_tag_suggestions.json").write_bytes(b"{not json")
    assert music_engine.load_suggestions() == {"version": 1, "tracks": {}}
    (d / "music_tag_suggestions.json").write_bytes(b"[1, 2, 3]")
    assert music_engine.load_suggestions() == {"version": 1, "tracks": {}}


def test_coerce_suggestions_degrades_corrupt_shapes():
    data = {
        "version": 1,
        "tracks": {
            "a.mp3": {"tags": {"scene": ["战斗", 42, "  "], "mood": "bad", "emotion": None},
                      "suggested_at": 7, "model": None},
            "b.mp3": "not-a-dict",
        },
    }
    out = music_engine._coerce_suggestions(data)
    assert out["tracks"]["a.mp3"] == {"tags": {"scene": ["战斗"], "mood": [], "emotion": []},
                                      "suggested_at": "", "model": ""}
    assert "b.mp3" not in out["tracks"]
    assert music_engine._coerce_suggestions([1, 2]) == {"version": 1, "tracks": {}}


def test_save_suggestions_write_bytes_no_crlf(sandbox):
    music_engine.save_suggestions({
        "version": 1,
        "tracks": {"战斗曲.mp3": {"tags": {"scene": ["战斗"], "mood": ["紧张"], "emotion": []},
                                  "suggested_at": "t0", "model": "m"}},
    })
    raw = (sandbox["lib"] / "music_tag_suggestions.json").read_bytes()
    assert b"\r\n" not in raw  # write_bytes — no Windows newline translation
    data = json.loads(raw.decode("utf-8"))
    assert data["tracks"]["战斗曲.mp3"]["tags"]["mood"] == ["紧张"]


def test_update_suggestions_abort_writes_nothing(sandbox):
    def boom(d):
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        music_engine.update_suggestions(boom)
    assert not (sandbox["lib"] / "music_tag_suggestions.json").exists()


def test_clear_suggestion_noop_when_absent(sandbox):
    music_engine.clear_suggestion("a.mp3")
    assert not (sandbox["lib"] / "music_tag_suggestions.json").exists()  # no entry -> no write


def test_clear_suggestion_removes_only_named_entry(sandbox):
    _upload("a.mp3")
    _upload("b.mp3")
    music_engine.update_suggestions(
        lambda d: d["tracks"].update({
            "a.mp3": {"tags": {"scene": ["战斗"], "mood": [], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
            "b.mp3": {"tags": {"scene": [], "mood": ["紧张"], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
        }))
    music_engine.clear_suggestion("a.mp3")
    tracks = music_engine.load_suggestions()["tracks"]
    assert list(tracks) == ["b.mp3"]


# --------------------------------------------------------------------------- #
# API: /library suggestions + tag decision consumes the candidate
# --------------------------------------------------------------------------- #

def test_library_suggestions_filtered_to_existing_tracks(sandbox):
    _upload("a.mp3")
    _upload("b.mp3")
    music_engine.save_suggestions({
        "version": 1,
        "tracks": {
            "a.mp3": {"tags": {"scene": ["战斗"], "mood": [], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
            # orphan of a deleted track — hidden, never removed
            "ghost.mp3": {"tags": {"scene": [], "mood": ["紧张"], "emotion": []},
                           "suggested_at": "t0", "model": "m"},
        },
    })
    r = api_music.get_library()
    assert set(r["suggestions"]) == {"a.mp3"}
    assert r["suggestions"]["a.mp3"]["tags"]["scene"] == ["战斗"]
    assert r["tracks"]["a.mp3"]["tags"] == music_engine._empty_track_tags()  # candidates NOT applied


def test_update_track_tags_consumes_suggestion(sandbox):
    _upload("a.mp3")

    def seed():
        music_engine.update_suggestions(
            lambda d: d["tracks"].__setitem__("a.mp3", {
                "tags": {"scene": ["战斗"], "mood": [], "emotion": []},
                "suggested_at": "t0", "model": "m"}))

    # a tags patch = the user made a tag decision -> the candidate is consumed
    seed()
    api_music.update_track("a.mp3", api_music.TrackUpdate(tags={"mood": ["紧张"]}))
    assert "a.mp3" not in music_engine.load_suggestions()["tracks"]

    # a non-tags patch leaves the candidate in place
    seed()
    api_music.update_track("a.mp3", api_music.TrackUpdate(enabled=False))
    assert "a.mp3" in music_engine.load_suggestions()["tracks"]


# --------------------------------------------------------------------------- #
# API: apply-suggestions（AI 推荐采用 — untagged tracks only, never overwrites
# a manual decision）
# --------------------------------------------------------------------------- #

def test_apply_suggestions_untagged_only(sandbox):
    _upload("a.mp3")  # candidate + untagged -> applied + candidate consumed
    _upload("b.mp3")  # manual tags THEN a fresh candidate -> NEVER overwritten
    _upload("c.mp3")  # no candidate at all -> no_suggestion
    _upload("d.mp3")  # candidate with a stale (since-removed) name -> custom fold
    # b.mp3: the user made a manual tag decision first…
    api_music.update_track("b.mp3", api_music.TrackUpdate(tags={"emotion": ["希望"]}))
    # …then ran AI recognition again (the batch endpoint does not block tagged
    # tracks) -> b now has manual tags AND a live candidate.
    music_engine.update_suggestions(
        lambda d: d["tracks"].update({
            "a.mp3": {"tags": {"scene": ["战斗"], "mood": ["紧张"], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
            "b.mp3": {"tags": {"scene": [], "mood": ["轻松"], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
            "d.mp3": {"tags": {"scene": ["已改名的标签"], "mood": [], "emotion": []},
                       "suggested_at": "t0", "model": "m"},
        }))
    r = api_music.apply_suggestions(
        api_music.ApplySuggestionsReq(names=["a.mp3", "b.mp3", "c.mp3", "d.mp3"]))
    assert r["applied"] == ["a.mp3", "d.mp3"]
    assert r["skipped_manual"] == ["b.mp3"]
    assert r["no_suggestion"] == ["c.mp3"]
    assert r["missing"] == []
    idx = music_engine.load_index()
    # applied: the candidates are written into the track tags as-is (in-vocab)
    assert idx["tracks"]["a.mp3"]["tags"]["scene"] == ["战斗"]
    assert idx["tracks"]["a.mp3"]["tags"]["mood"] == ["紧张"]
    # a stale candidate name is folded into custom (same as a user-confirmed PUT)
    assert idx["tracks"]["d.mp3"]["tags"]["scene"] == []
    assert idx["tracks"]["d.mp3"]["tags"]["custom"] == ["已改名的标签"]
    # b.mp3: manual tags byte-identical, candidate KEPT (still confirmable)
    assert idx["tracks"]["b.mp3"]["tags"]["emotion"] == ["希望"]
    assert idx["tracks"]["b.mp3"]["tags"]["mood"] == []
    sugg = music_engine.load_suggestions()["tracks"]
    assert "a.mp3" not in sugg and "d.mp3" not in sugg  # consumed
    assert "b.mp3" in sugg  # untouched


def test_apply_suggestions_guards_and_dedupe(sandbox):
    _upload("a.mp3")
    music_engine.update_suggestions(
        lambda d: d["tracks"].__setitem__("a.mp3", {
            "tags": {"scene": ["战斗"], "mood": [], "emotion": []},
            "suggested_at": "t0", "model": "m"}))
    with pytest.raises(HTTPException) as e:
        api_music.apply_suggestions(api_music.ApplySuggestionsReq(names=[]))
    assert e.value.status_code == 400
    for bad in ("../evil.mp3", "a/b.mp3", "song.flac"):
        with pytest.raises(HTTPException) as e:
            api_music.apply_suggestions(api_music.ApplySuggestionsReq(names=[bad]))
        assert e.value.status_code == 400
    # missing tracks are reported, not an error (batch-endpoint precedent);
    # duplicates are applied once.
    r = api_music.apply_suggestions(
        api_music.ApplySuggestionsReq(names=["a.mp3", "a.mp3", "ghost.mp3"]))
    assert r["applied"] == ["a.mp3"]
    assert r["missing"] == ["ghost.mp3"]
    assert "a.mp3" not in music_engine.load_suggestions()["tracks"]  # consumed


# --------------------------------------------------------------------------- #
# suggest_track_tags worker e2e (fake LLM, real TaskManager + shared LLM gate)
# --------------------------------------------------------------------------- #

def test_suggest_track_tags_success_writes_only_own_entry(sandbox, monkeypatch):
    _upload("a.mp3")
    _upload("b.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    reply = ('{"scene": ["战斗", "编造的"], "mood": ["紧张", "热血", "史诗", "恐怖"], '
             '"emotion": ["愤怒", "希望", "喜悦"]}')
    fake, calls = _fake_llm(reply)
    monkeypatch.setattr(music_engine, "_llm_chat_completion", fake)
    cfg = core_config.get_config()
    mgr = get_task_manager()
    tid = mgr.create("music-ai-tags", "AI 推荐标签：a.mp3",
                     music_engine.suggest_track_tags, "a.mp3", cfg.llm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    assert calls["n"] == 1
    t = mgr.get(tid)
    # vocabulary filter + caps applied (same semantics as the sync endpoint)
    assert t.result == {"scene": ["战斗"], "mood": ["紧张", "热血", "史诗"], "emotion": ["愤怒", "希望"]}
    # candidates land ONLY in the suggestions cache, only this track's entry
    sugg = music_engine.load_suggestions()["tracks"]
    assert list(sugg) == ["a.mp3"]
    assert sugg["a.mp3"]["tags"] == t.result
    assert sugg["a.mp3"]["model"] == "test-model" and sugg["a.mp3"]["suggested_at"]
    # the index is NOT touched — nothing reaches track tags until user confirmation
    idx = music_engine.load_index()
    assert idx["tracks"]["a.mp3"]["tags"] == music_engine._empty_track_tags()
    assert idx["tracks"]["b.mp3"]["tags"] == music_engine._empty_track_tags()
    assert concurrency.gate().active == 0  # slot released


def test_suggest_track_tags_all_fail_task_failed(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    fake, calls = _fake_llm(RuntimeError("boom"))
    monkeypatch.setattr(music_engine, "_llm_chat_completion", fake)
    cfg = core_config.get_config()
    mgr = get_task_manager()
    tid = mgr.create("music-ai-tags", "AI 推荐标签：a.mp3",
                     music_engine.suggest_track_tags, "a.mp3", cfg.llm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert calls["n"] == 2  # both attempts made, then the task fails (UI offers 重试)
    assert "AI 推荐失败" in mgr.get(tid).error and "boom" in mgr.get(tid).error
    assert not (sandbox["lib"] / "music_tag_suggestions.json").exists()  # zero writes
    assert concurrency.gate().active == 0


def test_suggest_track_tags_unparseable_replies_failed(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    fake, calls = _fake_llm("这不是 JSON")
    monkeypatch.setattr(music_engine, "_llm_chat_completion", fake)
    cfg = core_config.get_config()
    mgr = get_task_manager()
    tid = mgr.create("music-ai-tags", "AI 推荐标签：a.mp3",
                     music_engine.suggest_track_tags, "a.mp3", cfg.llm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert calls["n"] == 2
    assert "回复不可解析" in mgr.get(tid).error


def test_suggest_track_tags_model_empty_fast_fail(sandbox):
    _upload("a.mp3")
    assert not core_config.get_config().llm.model_name
    cfg = core_config.get_config()
    mgr = get_task_manager()
    tid = mgr.create("music-ai-tags", "AI 推荐标签：a.mp3",
                     music_engine.suggest_track_tags, "a.mp3", cfg.llm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "尚未配置 LLM 模型" in mgr.get(tid).error


def test_suggest_track_tags_missing_file_failed(sandbox):
    core_config.update_config({"llm": {"model_name": "test-model"}})
    cfg = core_config.get_config()
    mgr = get_task_manager()
    tid = mgr.create("music-ai-tags", "AI 推荐标签：ghost.mp3",
                     music_engine.suggest_track_tags, "ghost.mp3", cfg.llm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "音乐库中找不到" in mgr.get(tid).error


def test_suggest_track_tags_cancel_while_queued(sandbox, monkeypatch):
    _upload("a.mp3")
    core_config.update_config({"llm": {"model_name": "test-model"}})
    monkeypatch.setattr(music_engine, "_llm_chat_completion", _fake_llm("{}"))
    g = concurrency.gate()
    concurrency.set_concurrency(1)
    g.acquire()  # the test holds the only slot
    try:
        cfg = core_config.get_config()
        mgr = get_task_manager()
        t0 = time.time()
        tid = mgr.create("music-ai-tags", "AI 推荐标签：a.mp3",
                         music_engine.suggest_track_tags, "a.mp3", cfg.llm).id
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.RUNNING, timeout=3)
        mgr.control(tid, "cancel")
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.CANCELLED, timeout=3)
        assert time.time() - t0 < 2.5  # one stop_check poll (0.2 s) + overhead
        assert g.active == 1  # the worker never took the slot (no release underflow)
        assert not (sandbox["lib"] / "music_tag_suggestions.json").exists()  # 零落盘
    finally:
        g.release()
        assert g.active == 0
