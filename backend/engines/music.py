"""The global music library engine (背景音乐系统 · 音乐库).

The library lives at the PROJECT ROOT (``core_paths.MUSIC_LIBRARY_DIR``), OUTSIDE
any workspace: music files + one shared index (``music_index.json``) are
project-level resources shared by every workspace. It is deliberately a fixed
code constant — config is per-workspace, so a per-project setting that pinned a
global path would be self-contradictory.

Design notes
------------
* **Single index, not per-track sidecars**: tag rename / delete / batch ops all
  need to read-then-atomically-write every track at once; a sidecar mid-failure
  would leave inconsistent state. At library scale (tens to hundreds of
  tracks) one small JSON file is a non-issue.
* **Tags are 4-bucketed** (scene / mood / emotion / custom): the built-in
  vocabulary contains a name in two buckets (悲伤 = mood + emotion), so bucket
  identity — not name identity — disambiguates at match time. Weights come from
  the bucket (mood 3 > scene 2 > emotion 1, custom +1). New/rename still 409 on
  a name that already exists in *any* bucket (no third occurrence); the
  built-in cross-bucket pair is grandfathered.
* **Atomic writes**: ``write_bytes`` + ``os.replace`` under a module lock (the
  voice_config.json precedent). Reads never write; a missing index degrades to
  the DEFAULT_TAGS-seeded empty index in memory (not persisted), a corrupt one
  degrades with a WARNING.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from backend.core import paths as core_paths
from backend.core.concurrency import gate
from backend.core.tasks import TaskCancelled
from backend.engines.script import _llm_chat_completion
from backend.engines.voices import extract_json_object

log = logging.getLogger("audiobook.music")

# -- tag model --------------------------------------------------------------- #

#: The four tag categories (buckets), in display order.
TAG_CATEGORIES = ("scene", "mood", "emotion", "custom")

#: Match weights per category (气氛 > 场景 > 情绪; custom participates at +1).
TAG_WEIGHTS = {"mood": 3, "scene": 2, "emotion": 1, "custom": 1}

#: Built-in tag vocabulary (plan.md §三). Note: 悲伤 intentionally appears in
#: both mood and emotion — bucketing disambiguates; new/rename 409s only on a
#: name already present in ANY bucket.
DEFAULT_TAGS: dict[str, list[str]] = {
    "scene": ["日常", "战斗", "冒险", "旅行", "宫廷", "城市", "森林", "夜晚", "宴会", "爱情"],
    "mood": ["轻松", "温馨", "欢快", "平静", "神秘", "紧张", "压抑", "悲伤", "恐怖", "庄严", "热血", "史诗"],
    "emotion": ["希望", "喜悦", "愤怒", "悲伤", "孤独", "浪漫", "激动", "绝望", "沉重"],
    "custom": [],
}

#: Uploadable audio extensions (lowercase, with dot).
ALLOWED_EXTS = {".mp3", ".wav"}

_INDEX_NAME = "music_index.json"

# Module-level lock for the read-modify-write transactions on the index.
_INDEX_LOCK = threading.RLock()


def _empty_track_tags() -> dict[str, list[str]]:
    return {c: [] for c in TAG_CATEGORIES}


# -- pure functions ---------------------------------------------------------- #

def normalize_track_tags(raw: dict | None, registry: dict[str, list[str]]) -> dict[str, list[str]]:
    """Normalise a client-supplied tag set into the four buckets.

    For each non-custom bucket only names present in that bucket's registry
    vocabulary are kept (first-appearance order, de-duplicated); everything
    else (out-of-vocabulary names, garbage, the client's own custom list) is
    folded into ``custom``. A non-dict / missing input yields four empty
    buckets. The registry is the live index's ``tags`` section.
    """
    out = _empty_track_tags()
    if not isinstance(raw, dict):
        return out
    for cat in TAG_CATEGORIES:
        vals = raw.get(cat)
        if not isinstance(vals, list):
            continue
        vocab = registry.get(cat) if isinstance(registry.get(cat), list) else []
        for v in vals:
            if not isinstance(v, str):
                continue
            name = v.strip()
            if not name:
                continue
            if cat == "custom" or name in vocab:
                if name not in out[cat]:
                    out[cat].append(name)
            elif name not in out["custom"]:
                out["custom"].append(name)
    return out


def is_generic_track(track: dict) -> bool:
    """A usable fallback: enabled with ALL FOUR buckets empty (「通用音乐」)."""
    if not isinstance(track, dict) or not track.get("enabled", False):
        return False
    tags = track.get("tags")
    if not isinstance(tags, dict):
        return False
    return all(
        not [t for t in (tags.get(c) or []) if isinstance(t, str) and t.strip()]
        for c in TAG_CATEGORIES
    )


def find_tag_category(registry: dict[str, list[str]], name: str) -> str | None:
    """The FIRST category (in :data:`TAG_CATEGORIES` order) whose registry
    bucket contains ``name`` — or ``None`` when the name is not registered."""
    for cat in TAG_CATEGORIES:
        vals = registry.get(cat)
        if isinstance(vals, list) and name in vals:
            return cat
    return None


def all_tag_names(registry: dict[str, list[str]]) -> set[str]:
    """Every registered tag name (across all four buckets)."""
    names: set[str] = set()
    for cat in TAG_CATEGORIES:
        vals = registry.get(cat)
        if isinstance(vals, list):
            names.update(v for v in vals if isinstance(v, str) and v.strip())
    return names


def apply_tag_rename(
    index: dict, category: str, old: str, new: str
) -> int:
    """Rename a registered tag ``old`` -> ``new`` IN ``category`` (mutates
    ``index`` in place; the caller holds the lock / persists).

    Propagates to the registry bucket and to every track's ``tags[category]``.
    Returns the number of tracks whose tag list changed. Pre-conditions (old
    present, new absent globally) are the API layer's job.
    """
    bucket = index.get("tags", {}).get(category)
    if isinstance(bucket, list) and old in bucket:
        bucket[bucket.index(old)] = new
    tracks = index.get("tracks")
    if not isinstance(tracks, dict):
        return 0
    affected = 0
    for tr in tracks.values():
        if not isinstance(tr, dict):
            continue
        ttags = tr.get("tags")
        if not isinstance(ttags, dict):
            continue
        lst = ttags.get(category)
        if isinstance(lst, list) and old in lst:
            ttags[category] = [new if v == old else v for v in lst]
            affected += 1
    return affected


def apply_tag_delete(index: dict, category: str, name: str) -> int:
    """Remove a tag from the registry bucket and every track (mutates
    ``index`` in place). Returns the number of tracks whose tag list changed.
    Never touches music files (plan.md: 删除标签仅移除标签)."""
    bucket = index.get("tags", {}).get(category)
    if isinstance(bucket, list) and name in bucket:
        bucket.remove(name)
    tracks = index.get("tracks")
    if not isinstance(tracks, dict):
        return 0
    affected = 0
    for tr in tracks.values():
        if not isinstance(tr, dict):
            continue
        ttags = tr.get("tags")
        if not isinstance(ttags, dict):
            continue
        lst = ttags.get(category)
        if isinstance(lst, list) and name in lst:
            ttags[category] = [v for v in lst if v != name]
            affected += 1
    return affected


def validate_music_name(name: str) -> str:
    """Guard a music file name: no traversal, allowed extension.

    Returns the bare name; raises ``ValueError`` (-> HTTP 400) otherwise.
    """
    if not isinstance(name, str):
        raise ValueError("非法音乐文件名。")
    bare = Path(name).name
    if not name or bare != name or not bare.strip():
        raise ValueError(f"非法音乐文件名：{name!r}")
    ext = Path(bare).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"仅支持 MP3 / WAV（收到 {ext or '无扩展名'} 文件）。")
    return bare


# -- index IO (module lock + atomic os.replace; reads never write) ----------- #

def _library_dir() -> Path:
    # Module-attribute access at call time: tests monkeypatch
    # ``core_paths.MUSIC_LIBRARY_DIR`` and it must take effect here.
    return core_paths.MUSIC_LIBRARY_DIR


def _index_path() -> Path:
    return _library_dir() / _INDEX_NAME


def _default_index() -> dict:
    return {
        "version": 1,
        "tags": copy.deepcopy(DEFAULT_TAGS),
        "tracks": {},
    }


def _coerce_duration(v) -> float:
    """A finite float, else 0.0 (a corrupt duration string must not nuke the
    whole index — per-field degradation)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return 0.0


def _coerce_index(data) -> dict:
    """Coerce parsed JSON into a well-formed index (degrading corrupt shapes)."""
    if not isinstance(data, dict):
        return _default_index()
    idx = _default_index()
    if isinstance(data.get("tags"), dict):
        for cat in TAG_CATEGORIES:
            vals = data["tags"].get(cat)
            if isinstance(vals, list):
                idx["tags"][cat] = [v for v in vals if isinstance(v, str) and v.strip()]
        # Preserve any registered names we know how to bucket; unknown
        # category keys are dropped (the schema is fixed at four buckets).
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        for name, tr in tracks.items():
            if not isinstance(name, str) or not isinstance(tr, dict):
                continue
            tags = tr.get("tags")
            idx["tracks"][name] = {
                "duration": _coerce_duration(tr.get("duration")),
                "enabled": bool(tr.get("enabled", True)),
                "description": tr.get("description") if isinstance(tr.get("description"), str) else "",
                "tags": normalize_track_tags(tags, idx["tags"]),
                "added_at": tr.get("added_at") if isinstance(tr.get("added_at"), str) else "",
            }
    return idx


def load_index() -> dict:
    """Read the index. Missing -> DEFAULT_TAGS-seeded empty index (NOT written
    to disk — reads never write). Corrupt -> the same degraded index + WARNING."""
    p = _index_path()
    if not p.exists():
        return _default_index()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        return _coerce_index(data)
    except Exception as e:
        log.warning("音乐库索引损坏，降级为空索引：%s", e)
        return _default_index()


def save_index(index: dict) -> None:
    """Atomically write the index (write_bytes + os.replace). Caller holds the
    lock (or accepts concurrent writers — os.replace is atomic either way)."""
    d = _library_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".music_index_", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, _index_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_index(mutator) -> dict:
    """Atomic read -> mutate -> write transaction on the index (shared by
    upload / tag edits / batch ops / tag management).

    ``mutator(index)`` may raise to abort the transaction (nothing is written;
    the file, if already written by the mutator, is the mutator's concern).
    Returns the (saved) index.
    """
    with _INDEX_LOCK:
        idx = load_index()
        mutator(idx)
        save_index(idx)
        return idx


# --------------------------------------------------------------------------- #
# AI tag suggestions cache (music_tag_suggestions.json — candidates only)
#
# Batch AI tag recognition (one Task per track, module ``music-ai-tags``)
# writes its CANDIDATE tags here — never into the index. Track tags change
# only when the user confirms in the UI (PUT /tracks/{name} with tags, which
# also consumes the entry). Orphan entries (track deleted afterwards) are
# harmless dead weight — the UI joins rows against the index and the API
# filters them out of GET /library (same orphan policy as BGM assignments).
# --------------------------------------------------------------------------- #

_SUGGESTIONS_NAME = "music_tag_suggestions.json"

_SUGGESTIONS_LOCK = threading.RLock()


def _suggestions_path() -> Path:
    return _library_dir() / _SUGGESTIONS_NAME


def _default_suggestions() -> dict:
    return {"version": 1, "tracks": {}}


def _coerce_suggestions(data) -> dict:
    """Coerce parsed JSON into a well-formed suggestions cache (degrading
    corrupt shapes)."""
    if not isinstance(data, dict):
        return _default_suggestions()
    out = _default_suggestions()
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        for name, entry in tracks.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            out["tracks"][name] = {
                "tags": _coerce_suggestion_tags(entry.get("tags")),
                "suggested_at": entry.get("suggested_at") if isinstance(entry.get("suggested_at"), str) else "",
                "model": entry.get("model") if isinstance(entry.get("model"), str) else "",
            }
    return out


def _coerce_suggestion_tags(tags) -> dict[str, list[str]]:
    """The three LLM buckets (scene/mood/emotion), lists of strings. The
    suggestions cache holds the LLM's RAW candidates — no registry filtering
    here (the registry may legitimately outdate a cached suggestion; the
    vocabulary filter already happened at LLM reply time)."""
    out: dict[str, list[str]] = {}
    for cat in ("scene", "mood", "emotion"):
        vals = tags.get(cat) if isinstance(tags, dict) else None
        out[cat] = [v for v in vals if isinstance(v, str) and v.strip()] if isinstance(vals, list) else []
    return out


def load_suggestions() -> dict:
    """Read the suggestions cache. Missing -> empty cache in memory (NOT
    written to disk — reads never write). Corrupt -> degraded + WARNING."""
    p = _suggestions_path()
    if not p.exists():
        return _default_suggestions()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        return _coerce_suggestions(data)
    except Exception as e:
        log.warning("AI 标签推荐缓存损坏，降级为空缓存：%s", e)
        return _default_suggestions()


def save_suggestions(data: dict) -> None:
    """Atomically write the suggestions cache (write_bytes + os.replace)."""
    d = _library_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".music_suggest_", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, _suggestions_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_suggestions(mutator) -> dict:
    """Atomic read -> mutate -> write transaction on the suggestions cache
    (parallel AI tasks each rewrite the whole file — the voice_config.json /
    08_bgm analysis precedent)."""
    with _SUGGESTIONS_LOCK:
        data = load_suggestions()
        mutator(data)
        save_suggestions(data)
        return data


def clear_suggestion(name: str) -> None:
    """Drop one track's candidate entry (the user made a tag decision).
    No-op when absent — reads never write, the file is only touched when an
    entry actually exists."""
    if name in (load_suggestions().get("tracks") or {}):
        update_suggestions(lambda d: d["tracks"].pop(name, None))


# --------------------------------------------------------------------------- #
# AI tag recommendation prompts / parsing (shared by the sync single-track
# endpoint and the per-track Task worker — text-only: filename + description
# + vocabulary, the LLM never reads the audio)
# --------------------------------------------------------------------------- #

#: Per-bucket caps for LLM-suggested tags (防 LLM 造词/过量).
SUGGESTION_CAPS = {"scene": 2, "mood": 3, "emotion": 2}


def build_suggestion_prompts(stem: str, description: str,
                             registry: dict[str, list[str]]) -> tuple[str, str]:
    """(system, user) prompt for tag suggestion from the file NAME (``stem``)
    + user DESCRIPTION + the tag vocabulary."""
    vocab = {c: registry.get(c, []) for c in TAG_CATEGORIES if c != "custom"}
    vocab_text = "\n".join(f"{c}: {'、'.join(v)}" for c, v in vocab.items())
    system = (
        "你是有声书背景音乐标签助手。根据音乐文件名和用户描述，从给定词表中选择标签。"
        "只能从词表中选择，禁止创造新词。输出 JSON：{\"scene\": [...], \"mood\": [...], "
        "\"emotion\": [...]}，scene 最多 2 个、mood 最多 3 个、emotion 最多 2 个，"
        "选不出就留空数组。只输出 JSON，不要解释。"
    )
    user = f"文件名：{stem}\n用户描述：{description or '（无）'}\n\n词表：\n{vocab_text}"
    return system, user


def parse_suggestion_reply(content: str, registry: dict[str, list[str]]) -> dict | None:
    """Parse an LLM suggestion reply (a JSON OBJECT — ``extract_json_object``)
    into the three tag buckets, keeping ONLY in-vocabulary names per category
    and capping each bucket (anti word-coining / overflow). ``None`` when the
    reply is not a JSON object (the caller retries)."""
    data = extract_json_object(content)
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for cat in ("scene", "mood", "emotion"):
        vals = data.get(cat)
        vocab = [v for v in (registry.get(cat) or []) if isinstance(v, str)]
        keep: list[str] = []
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, str) and v.strip() and v.strip() in vocab and v.strip() not in keep:
                    keep.append(v.strip())
        out[cat] = keep[: SUGGESTION_CAPS[cat]]
    return out


# --------------------------------------------------------------------------- #
# Task worker: one-track AI tag recognition (module ``music-ai-tags``)
# --------------------------------------------------------------------------- #

def suggest_track_tags(handle, name: str, llm_cfg) -> dict:
    """Task worker: LLM-recommend tags for ONE track (2 attempts, same prompt
    semantics as the sync single-track endpoint).

    Slot scope = the whole task (no check phase): the LLM call holds one shared
    LLM slot (``gate()``); a cancel while queued aborts without taking a slot.
    A total failure fails the TASK (the UI offers 重试) — unlike the chapter
    mood analysis, which never blocks a chapter (empty-tag record).

    The candidates land in ``music_tag_suggestions.json`` (only this track's
    entry is rewritten); the index is NOT touched — the user confirms in the
    UI before anything reaches the track's tags.
    """
    handle.check()
    lib_dir = _library_dir()
    if not (lib_dir / name).is_file():
        raise RuntimeError(f"音乐库中找不到 {name}。")
    if not llm_cfg.model_name:
        raise RuntimeError("尚未配置 LLM 模型（设置 → LLM → model_name）。")

    idx = load_index()
    tr = idx["tracks"].get(name)
    description = ((tr or {}).get("description") or "").strip()
    system, user = build_suggestion_prompts(Path(name).stem, description, idx["tags"])

    handle.progress(0.05, "排队中（等待并发槽位）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取槽，不进入 try、不 release
    try:
        parsed: dict | None = None
        last_err: str | None = None
        for attempt in range(2):
            handle.check()  # TaskCancelled 永不重试、直接上抛
            handle.log(f"LLM 识别标签（第 {attempt + 1}/2 次）…")
            try:
                content, _finish, _usage = _llm_chat_completion(
                    llm_cfg.base_url, llm_cfg.api_key, llm_cfg.model_name,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    temperature=0.2, top_p=0.9, presence_penalty=0.0, max_tokens=300,
                )
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001 — 2 次全败 → 任务失败（可重试）
                last_err = str(e)
                continue
            parsed = parse_suggestion_reply(content, idx["tags"])
            if parsed is not None:
                break
            last_err = "回复不可解析"
            parsed = None

        if parsed is None:
            raise RuntimeError(f"AI 推荐失败（{last_err}），请重试或手动打标。")

        update_suggestions(
            lambda d: d["tracks"].__setitem__(name, {
                "tags": parsed,
                "suggested_at": datetime.now().isoformat(timespec="seconds"),
                "model": llm_cfg.model_name,
            })
        )
        parts = [f"{c} {', '.join(parsed[c])}" for c in ("scene", "mood", "emotion") if parsed[c]]
        handle.log("识别完成：" + ("、".join(parts) if parts else "（无标签）"))
        handle.progress(1.0, "完成")
        return parsed
    finally:
        gate().release()  # acquire 成功才进入 try——排队中被取消的路径未取槽、不到这里
