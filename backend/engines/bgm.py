"""The BGM engine (背景音乐系统 · 阶段 3/4): chapter mood analysis (LLM),
deterministic tag matching, and the final per-chapter mix.

Artifacts (all under the workspace's ``08_bgm/``):
* ``chapter_music_analysis.json`` — LLM mood tags per chapter (a CACHE: tag
  edits / re-matching never re-call the LLM);
* ``bgm_assignments.json`` — per-chapter match result (music = a bare music
  library FILE NAME — the library lives outside the workspace, so it never
  enters the pathio migration chain);
* ``<stem>.mp3`` — the final mixed audio (chapters without BGM are a plain
  copy of the 06 narration — still a finished product).

The music library itself (``music_library/`` at the project root) is managed
by :mod:`backend.engines.music`; this module only reads it (for matching) and
references it by file name (for mixing).
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from backend.core import paths as core_paths
from backend.core.concurrency import gate, merge_gate
from backend.core.tasks import TaskCancelled
from backend.engines.audio import probe_duration
from backend.engines.book import decode_buffer
from backend.engines import music as music_engine
from backend.engines.script import _llm_chat_completion
from backend.engines.voices import extract_json_object

log = logging.getLogger("audiobook.bgm")

# -- artifact file names (inside ``08_bgm/``) -------------------------------- #

ANALYSIS_NAME = "chapter_music_analysis.json"
ASSIGNMENTS_NAME = "bgm_assignments.json"

#: Per-bucket caps for LLM chapter-analysis tags (anti overflow).
_ANALYSIS_CAPS = {"scene": 2, "mood": 3, "emotion": 2, "custom": 2}

#: Match weights, heaviest first (reason strings list categories in this order).
_WEIGHT_ORDER = ("mood", "scene", "emotion", "custom")

# Module lock for the two 08_bgm JSON caches (parallel analysis tasks each
# rewrite the whole analysis file — the voice_config.json precedent).
_BGMS_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# pure functions (rng injectable — everything here is unit-testable)
# --------------------------------------------------------------------------- #

def sample_chapter_text(text: str, n: int) -> str:
    """The text handed to the LLM: the whole text when it fits in ``n`` chars,
    else head / middle / tail windows of ``n // 3`` joined by ``\\n……\\n``."""
    n = max(0, int(n))
    if len(text) <= n:
        return text
    w = max(1, n // 3)
    head = text[:w]
    mid_start = max(0, (len(text) - w) // 2)
    mid = text[mid_start:mid_start + w]
    tail = text[-w:]
    parts = [s for s in (head, mid, tail) if s]
    # a window may repeat when the text is barely longer than n — keep first-seen
    seen: set[str] = set()
    out: list[str] = []
    for s in parts:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "\n……\n".join(out)


def parse_analysis_reply(content: str) -> dict | None:
    """Parse an LLM analysis reply into the four tag buckets.

    Returns ``{scene, mood, emotion, custom}`` (each capped at
    :data:`_ANALYSIS_CAPS`, de-duplicated, stripped) or ``None`` when the reply
    is not a JSON object (the caller retries). Out-of-vocabulary names are
    KEPT (chapter tags are free-form analysis the user can edit — unlike
    suggest-tags, which is strict in-vocabulary).
    """
    data = extract_json_object(content)
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for cat in music_engine.TAG_CATEGORIES:
        vals = data.get(cat)
        if not isinstance(vals, list):
            out[cat] = []
            continue
        kept: list[str] = []
        for v in vals:
            if not isinstance(v, str):
                continue
            name = v.strip()
            if name and name not in kept:
                kept.append(name)
        out[cat] = kept[: _ANALYSIS_CAPS[cat]]
    return out


def score_track(chapter_tags: dict, track_tags: dict) -> tuple[int, str]:
    """Weighted tag-overlap score between a chapter's analysis and one track.

    Same-bucket intersection × category weight (mood 3 / scene 2 / emotion 1 /
    custom 1). The reason string is pinned: ``"mood 命中 紧张, 热血(+6)；scene
    命中 战斗(+2)"`` — one segment per hit category, heaviest first.
    """
    total = 0
    parts: list[str] = []
    if isinstance(chapter_tags, dict) and isinstance(track_tags, dict):
        for cat in _WEIGHT_ORDER:
            a = chapter_tags.get(cat)
            b = track_tags.get(cat)
            if not isinstance(a, list) or not isinstance(b, list):
                continue
            b_set = {t for t in b if isinstance(t, str) and t.strip()}
            hit = [t for t in dict.fromkeys(a) if isinstance(t, str) and t in b_set]
            if hit:
                pts = len(hit) * music_engine.TAG_WEIGHTS[cat]
                total += pts
                parts.append(f"{cat} 命中 {', '.join(hit)}(+{pts})")
    return total, "；".join(parts)


def match_chapter(
    chapter_tags: dict,
    tracks: list[tuple[str, dict]],
    min_score: int = 1,
    prev_music: str | None = None,
    next_music: str | None = None,
    rng: random.Random | None = None,
    mode: str = "llm",
) -> dict:
    """Pick one track (or None) for a chapter.

    ``tracks`` = ``[(name, track_entry), …]`` from the library index (enabled
    filtering is re-applied defensively here). Order:
    1. highest score (random among ties, ``rng``); a score below ``min_score``
       yields no tagged candidate;
    2. else the GENERIC pool (enabled + all four buckets empty), same
       adjacent-chapter de-dup;
    3. else ``music=None``.

    Adjacent de-dup: the ``prev_music`` / ``next_music`` file names are
    excluded from the candidate set; when that blocks the WHOLE set the
    exclusion is relaxed (and noted in the reason).

    ``mode="random"`` ignores tags entirely: every enabled track is a
    candidate with score 0, reason 「全章节随机」 (still adjacent-de-duped).
    """
    rng = rng or random.Random()
    exclude = {m for m in (prev_music, next_music) if m}
    enabled = [
        (name, t) for name, t in tracks
        if isinstance(t, dict) and t.get("enabled")
    ]

    def _pick(pool: list[str]) -> tuple[str | None, bool]:
        if not pool:
            return None, False
        ok = [n for n in pool if n not in exclude]
        if ok:
            return rng.choice(ok), False
        return rng.choice(pool), True  # fully blocked by neighbours — relax

    if mode == "random":
        music, relaxed = _pick([n for n, _ in enabled])
        if music is None:
            return {"music": None, "via": "none", "score": 0,
                    "reason": "无候选：音乐库中没有启用的曲目"}
        return {"music": music, "via": "random", "score": 0,
                "reason": ("全章节随机" if not relaxed else "全章节随机（相邻章去重放宽）")}

    scored = []
    for name, t in enabled:
        s, reason = score_track(chapter_tags, t.get("tags") or {})
        if s >= max(1, int(min_score)):
            scored.append((s, name, reason))
    if scored:
        best = max(s for s, _n, _r in scored)
        top = [n for s, n, _r in scored if s == best]
        reason = next(r for s, _n, r in scored if s == best)
        music, relaxed = _pick(top)
        if relaxed:
            reason += "（相邻章去重放宽）"
        return {"music": music, "via": "tags", "score": best, "reason": reason}

    generic = [n for n, t in enabled if music_engine.is_generic_track(t)]
    music, relaxed = _pick(generic)
    if music is not None:
        return {"music": music, "via": "generic", "score": 0,
                "reason": "无标签命中，使用通用音乐" + ("（相邻章去重放宽）" if relaxed else "")}
    return {"music": None, "via": "none", "score": 0,
            "reason": "无候选：无标签命中且无通用音乐"}


def _fmt_time(x: float) -> str:
    return f"{max(0.0, float(x)):.3f}"


def build_mix_cmd(
    ffmpeg: str,
    narration: Path,
    music: Path,
    out: Path,
    duration: float,
    cfg,
    music_duration: float,
) -> list[str]:
    """The one-shot ffmpeg command that mixes one chapter (pinned element-wise).

    * both inputs are resampled to 44100 (mp3 44.1k / wav 48k — prevents an
      amix sample-rate conflict);
    * ``loop=True`` → ``-stream_loop -1`` (input-level repeat; aloop's size
      parameter is in samples and ambiguous), ``loop=False`` → ``-stream_loop -0``;
    * the music is trimmed to the narration duration with BOTH fades clamped
      ``min(fade, duration/2)`` (short chapters must never produce overlapping
      fades / an out-of-range ``st``);
    * ``amix duration=first`` — the narration length wins.
    """
    duration = float(duration)
    fade_in = round(min(float(cfg.fade_in), duration / 2), 3)
    fade_out = round(min(float(cfg.fade_out), duration / 2), 3)
    st = round(duration - fade_out, 3)
    vol = float(cfg.volume)
    loop_arg = "-1" if cfg.loop else "-0"
    filter_complex = (
        "[0:a]aresample=44100[nar];"
        f"[1:a]aresample=44100,atrim=0:{_fmt_time(duration)},"
        f"afade=t=in:d={_fmt_time(fade_in)},"
        f"afade=t=out:st={_fmt_time(st)}:d={_fmt_time(fade_out)},"
        f"volume={vol:g}[bgm];"
        "[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0[out]"
    )
    cmd = [ffmpeg, "-y", "-i", str(narration), "-stream_loop", loop_arg, "-i", str(music),
           "-filter_complex", filter_complex, "-map", "[out]", "-c:a", "libmp3lame", str(out)]
    return cmd


# --------------------------------------------------------------------------- #
# 08_bgm JSON caches (module lock + tmp + os.replace; reads never write)
# --------------------------------------------------------------------------- #

def _bgm_dir(layout) -> Path:
    return layout.bgm


def _default_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _default_assignments() -> dict:
    return {"version": 1, "mode": "llm", "updated_at": "", "chapters": {}}


def _atomic_write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".bgm_", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_cached(layout, name: str, default_factory) -> dict:
    p = _bgm_dir(layout) / name
    if not p.exists():
        return default_factory()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        chapters = data.get("chapters")
        if not isinstance(chapters, dict):
            data["chapters"] = {}
        return data
    except Exception as e:
        log.warning("08_bgm 缓存 %s 损坏，降级为空：%s", name, e)
        return default_factory()


def load_analysis(layout) -> dict:
    return _load_cached(layout, ANALYSIS_NAME, _default_analysis)


def load_assignments(layout) -> dict:
    return _load_cached(layout, ASSIGNMENTS_NAME, _default_assignments)


def save_analysis(layout, data: dict) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ANALYSIS_NAME, data)


def save_assignments(layout, data: dict) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ASSIGNMENTS_NAME, data)


def list_chapter_stems(layout) -> list[str]:
    """Sorted stems of ``02_split_text/*.txt`` (includes the whole-book file)."""
    d = layout.split_text
    if d is None or not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.txt") if p.is_file())


# --------------------------------------------------------------------------- #
# Task worker 1: LLM chapter mood analysis (module ``bgm-analysis``)
# --------------------------------------------------------------------------- #

def _vocab_text(tags: dict[str, list[str]]) -> str:
    return "\n".join(
        f"{c}: {'、'.join(tags.get(c) or [])}" for c in music_engine.TAG_CATEGORIES
    )


def analyze_chapter(handle, stem: str, llm_cfg, bgm_cfg) -> dict:
    """Task worker: LLM-analyze ONE chapter's mood tags (3 attempts; a total
    failure leaves an empty-tag record + WARNING — a chapter is never blocked).

    Slot scope = the whole task (no check phase): the LLM call holds one shared
    LLM slot; a cancel while queued aborts without taking a slot.
    """
    handle.check()
    layout = core_paths.get_layout()
    if layout.split_text is None:
        raise RuntimeError("未设置工作空间。")
    src = layout.split_text / f"{stem}.txt"
    if not src.is_file():
        raise RuntimeError(f"未找到章节文件（02_split_text/{stem}.txt）。")
    if not llm_cfg.model_name:
        raise RuntimeError("尚未配置 LLM 模型（设置 → LLM → model_name）。")

    text = decode_buffer(src.read_bytes())[0]
    sample = sample_chapter_text(text, bgm_cfg.analysis_chars)
    idx = music_engine.load_index()
    system = (
        "你是有声书章节气氛分析助手。根据章节文本，从给定词表中选择标签。"
        "只能从词表中选择，禁止创造新词。输出 JSON：{\"scene\": [...], \"mood\": [...], "
        "\"emotion\": [...]}，scene 最多 2 个、mood 最多 3 个、emotion 最多 2 个，"
        "选不出就留空数组。只输出 JSON，不要解释。"
    )
    user = f"章节文本节选：\n{sample}"

    handle.progress(0.05, "排队中（等待并发槽位）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取槽，不进入 try、不 release
    try:
        parsed: dict | None = None
        last_err: str | None = None
        for attempt in range(3):
            handle.check()  # TaskCancelled 永不重试、直接上抛
            handle.log(f"LLM 分析（第 {attempt + 1}/3 次）…")
            try:
                content, _finish, _usage = _llm_chat_completion(
                    llm_cfg.base_url, llm_cfg.api_key, llm_cfg.model_name,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    temperature=0.2, top_p=0.9, presence_penalty=0.0, max_tokens=512,
                )
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001 — 3 次全败 → 空标签兜底
                last_err = str(e)
                continue
            parsed = parse_analysis_reply(content)
            if parsed is not None:
                break
            last_err = "回复不可解析"
            parsed = None

        entry = {
            "scene": (parsed or {}).get("scene", []),
            "mood": (parsed or {}).get("mood", []),
            "emotion": (parsed or {}).get("emotion", []),
            "custom": (parsed or {}).get("custom", []),
            "analyzed_at": datetime.now().isoformat(timespec="seconds"),
            "edited": False,
        }
        if parsed is None:
            handle.log(f"3 次分析均失败（{last_err}），写入空标签记录。", "warning")
            log.warning("章节气氛分析 3 次失败 %s：%s", stem, last_err)
        else:
            parts = [f"{c} {', '.join(entry[c])}"
                     for c in music_engine.TAG_CATEGORIES if entry[c]]
            handle.log("分析完成：" + ("、".join(parts) if parts else "（无标签）"))
        data = load_analysis(layout)
        data["model"] = llm_cfg.model_name
        data["chapters"][stem] = entry
        save_analysis(layout, data)
        handle.progress(1.0, "完成")
        return entry
    finally:
        gate().release()  # acquire 成功才进入 try——排队中被取消的路径未取槽、不到这里


# --------------------------------------------------------------------------- #
# Task worker 2: final mix (module ``bgm-mix``)
# --------------------------------------------------------------------------- #

def _find_narration(layout, stem: str) -> Path | None:
    """06 旁白：``<stem>.mp3``（``.wav`` 兜底）。"""
    d = layout.audio_merge
    if d is None or not d.exists():
        return None
    for ext in (".mp3", ".wav"):
        p = d / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def mix_chapter(handle, stem: str, bgm_cfg, ffmpeg_cfg) -> dict:
    """Task worker: mix ONE chapter (06 narration + the matched music →
    ``08_bgm/<stem>.mp3``).

    * ``music=None`` (matched but no BGM) → plain ``shutil.copy2`` of the 06
      narration (still a finished product);
    * the music slot holds the process-wide MERGE gate for the whole ffmpeg run
      (ffmpeg/CPU/disk bound — same gate as audio merge);
    * cancel → kill ffmpeg + TaskCancelled (a partial output stays on disk —
      a rerun with ``-y`` self-heals, the merge precedent).
    """
    handle.check()
    layout = core_paths.get_layout()
    if layout.bgm is None:
        raise RuntimeError("未设置工作空间。")
    narration = _find_narration(layout, stem)
    if narration is None:
        raise RuntimeError(
            f"未找到旁白音频（06_audio_merge/{stem}.mp3），请先完成音频合并。"
        )

    data = load_assignments(layout)
    entry = (data.get("chapters") or {}).get(stem)
    if not isinstance(entry, dict):
        raise RuntimeError("该章从未匹配，请先匹配。")
    music_name = entry.get("music")

    out = layout.bgm / f"{stem}.mp3"

    if not music_name:
        # 无 BGM 章：直接复制旁白（成品已生成，mix_exists=True）。
        handle.log("该章无 BGM，直接复制旁白音频。")
        layout.bgm.mkdir(parents=True, exist_ok=True)
        shutil.copy2(narration, out)
        handle.progress(1.0, "完成")
        return {"stem": stem, "file": out.name, "path": str(out),
                "duration": None, "music": None}

    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    music_path = lib_dir / music_name
    if not music_path.is_file():
        raise RuntimeError(f"音乐库中找不到 {music_name}，请先到「音乐库」页检查。")

    ffprobe = ffmpeg_cfg.ffprobe_path
    dur, err = probe_duration(narration, ffprobe)
    if not (dur > 0):
        raise RuntimeError(f"无法探测旁白时长（{err or 'ffprobe 失败'}）。")
    m_dur, _m_err = probe_duration(music_path, ffprobe)
    if not (m_dur > 0):
        raise RuntimeError(f"无法探测音乐时长（{_m_err or 'ffprobe 失败'}）。")

    cmd = build_mix_cmd(
        ffmpeg_cfg.ffmpeg_path or "ffmpeg", narration, music_path, out,
        dur, bgm_cfg, m_dur,
    )
    handle.log("开始混音：" + " ".join(cmd[:4]) + " …")

    if not merge_gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取闸，下面 finally 不得 release
    acquired = True
    proc = None
    try:
        handle.progress(0.05, "混音中")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=str(core_paths.PROJECT_ROOT),
        )
        start = time.monotonic()
        last_size = 0.0
        # 0.2 s 轮询：取消响应 + 进度粗估（输出文件增长 0.05→0.95）。
        while proc.poll() is None:
            handle.check()  # TaskCancelled → finally kill
            elapsed = time.monotonic() - start
            if out.exists():
                size = out.stat().st_size
                if size > last_size and dur > 0:
                    last_size = size
                    frac = 0.05 + 0.90 * min(1.0, elapsed / max(1.0, dur))
                    handle.progress(min(0.95, frac), "混音中")
            time.sleep(0.2)
        rc = proc.poll()
        stderr_tail = ""
        if proc.stderr:
            try:
                stderr_tail = (proc.stderr.read() or b"").decode("utf-8", "replace")[-300:]
            except Exception:
                pass
            proc.stderr.close()
        if rc != 0:
            raise RuntimeError(f"混音失败（ffmpeg 退出码 {rc}）：{stderr_tail.strip()[-300:]}")
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError(
                f"混音输出异常（缺失或小于 1KiB）：{stderr_tail.strip()[-300:]}"
            )
        handle.progress(1.0, "完成")
        return {"stem": stem, "file": out.name, "path": str(out),
                "duration": round(dur, 3), "music": music_name}
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            if proc.stderr:
                proc.stderr.close()
        if acquired:
            merge_gate().release()


# --------------------------------------------------------------------------- #
# matching (deterministic, synchronous — no task)
# --------------------------------------------------------------------------- #

def match_stems(
    layout,
    stems: list[str],
    mode: str,
    min_score: int,
    rng: random.Random | None = None,
) -> dict:
    """Re-match the given chapters (sorted), writing the assignments file ONCE.

    Locked chapters are skipped wholesale (their entry is preserved verbatim,
    ``matched_at`` untouched). The ``prev`` neighbour is the previous 02
    chapter's FRESH result when it is part of this call, else its EXISTING
    assignment (single-chapter re-match boundary — neighbours are never
    touched); ``next`` is always the next chapter's existing value.
    """
    idx = music_engine.load_index()
    tracks = list(idx["tracks"].items())
    analysis = load_analysis(layout).get("chapters") or {}
    data = load_assignments(layout)
    chapters = data.setdefault("chapters", {})
    rng = rng or random.Random()
    data["mode"] = mode
    matched = 0
    no_bgm = 0
    skipped_locked = 0
    fresh: dict[str, str | None] = {}  # stem → music this call (locked = existing)
    now = datetime.now().isoformat(timespec="seconds")
    for stem in sorted(set(stems)):
        cur = chapters.get(stem)
        if isinstance(cur, dict) and cur.get("locked"):
            skipped_locked += 1
            fresh[stem] = cur.get("music")  # a locked neighbour still constrains
            continue
        cur_tags = analysis.get(stem) or {}
        prev_key = _prev_stem_key(stem, layout)
        prev_music = None
        if prev_key:
            if prev_key in fresh:
                prev_music = fresh[prev_key]  # 上一章本次已处理 → 用新鲜结果
            else:
                p = chapters.get(prev_key)
                prev_music = p.get("music") if isinstance(p, dict) else None
        nxt = chapters.get(_next_stem_key(stem, layout))
        next_music = nxt.get("music") if isinstance(nxt, dict) else None
        res = match_chapter(
            cur_tags, tracks, max(1, int(min_score)),
            prev_music, next_music, rng=rng, mode=mode,
        )
        entry = {
            "tags": {c: list(cur_tags.get(c) or []) for c in music_engine.TAG_CATEGORIES},
            "music": res["music"],
            "locked": False,
            "manual": False,
            "score": res["score"],
            "reason": res["reason"],
            "matched_at": now,
        }
        if res["music"] is None:
            entry["score"] = 0
        chapters[stem] = entry
        if res["music"] is None:
            no_bgm += 1
        else:
            matched += 1
        fresh[stem] = res["music"]
    data["updated_at"] = now
    save_assignments(layout, data)
    return {"mode": mode, "matched": matched, "no_bgm": no_bgm,
            "skipped_locked": skipped_locked, "assignments": data}


def _next_stem_key(stem: str, layout) -> str:
    """The next chapter (sorted 02 files) after ``stem`` — its EXISTING
    assignment constrains this chapter's pick (adjacent de-dup)."""
    stems = list_chapter_stems(layout)
    try:
        i = stems.index(stem)
    except ValueError:
        return ""
    if i + 1 >= len(stems):
        return ""
    return stems[i + 1]


def _prev_stem_key(stem: str, layout) -> str:
    """The previous chapter (sorted 02 files) before ``stem`` — its music
    (fresh when it is part of this call, else existing) constrains the pick."""
    stems = list_chapter_stems(layout)
    try:
        i = stems.index(stem)
    except ValueError:
        return ""
    if i <= 0:
        return ""
    return stems[i - 1]
