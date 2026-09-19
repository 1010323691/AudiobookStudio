"""BGM endpoints (背景音乐 · 章节气氛分析 / 匹配 / 手动干预 / 混音).

Label contracts (承重 — three pinned sites: backend regex ↔ frontend derivation
↔ tests): analysis module ``bgm-analysis`` with label ``章节气氛分析：{stem}``;
mix module ``bgm-mix`` with label ``背景音乐混音：{stem}``. The in-flight guards
extract the stem from the label tail with ``re.search(r"：(.+)$")`` — the same
shape as the merge page's ``pkgOfLabel`` (take everything after the FIRST
full-width colon; a stem may itself contain one).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import paths as core_paths
from ..core.config import get_config
from ..core.concurrency import gate, merge_gate, set_concurrency, set_merge_concurrency
from ..core.paths import get_layout
from ..core.tasks import TERMINAL, TaskStatus, get_task_manager
from ..engines import bgm as Bgm
from ..engines import merge as Merge
from ..engines import music as music_engine
from . import _common

router = APIRouter(prefix="/api/bgm", tags=["bgm"])

# Same prefetch invariant as the merge batch: tasks waiting for a slot stay ≤ 4.
BGM_PREFETCH_DEPTH = 4

ANALYSIS_MODULE = "bgm-analysis"
MIX_MODULE = "bgm-mix"
ANALYSIS_LABEL = "章节气氛分析"
MIX_LABEL = "背景音乐混音"


# --------------------------------------------------------------------------- #
# in-flight guards + coordinator (no registry / no stop flag — cancel = the
# frontend's per-task control; a cancelled PENDING shell terminates in place and
# drops out of the dispatch search naturally)
# --------------------------------------------------------------------------- #

def _inflight_bgm_stems(module: str) -> set[str]:
    """Stems (the label tail ``：{stem}``) of non-terminal tasks of ``module``.

    The same-chapter in-flight guard: two analysis (or mix) tasks on one chapter
    would race on the same JSON entry / output file. Non-conflicting chapters may
    still join a running batch — their tasks simply queue behind the gate.
    """
    out = set()
    for t in get_task_manager().list():
        if t.module == module and t.status not in TERMINAL:
            m = re.search(r"：(.+)$", t.label)
            if m:
                out.add(m.group(1))
    return out


def _run_bgm_coordinator(ordered: list[str], gate_fn) -> None:
    """Dispatch a batch's PENDING shells in order, keeping the prefetch bounded.

    Invariant maintained each round: the number of batch tasks *waiting for a
    slot* is < BGM_PREFETCH_DEPTH. Slot holders are read from the process-wide
    gate (``gate()`` for analysis — only analysis workers hold LLM slots here —
    or ``merge_gate()`` for mix), so the gate is the hard cap and this thread
    only paces the start-ups.
    """
    mgr = get_task_manager()
    while True:
        tasks = [mgr.get(tid) for tid in ordered]
        if all(t is None or t.status in TERMINAL for t in tasks):
            return
        no_slot = sum(
            1 for t in tasks
            if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
        )
        waiting = max(0, no_slot - gate_fn().active)
        while waiting < BGM_PREFETCH_DEPTH:
            next_tid = next(
                (tid for tid, t in zip(ordered, tasks)
                 if t is not None and t.status is TaskStatus.PENDING),
                None,
            )
            if next_tid is None:
                break
            try:
                mgr.start(next_tid)
            except (ValueError, KeyError):
                break  # raced with a cancel — it will no longer be PENDING
            tasks = [mgr.get(tid) for tid in ordered]
            no_slot = sum(
                1 for t in tasks
                if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
            )
            waiting = max(0, no_slot - gate_fn().active)
        time.sleep(0.2)


def _validated_stems(layout, stems: list[str]) -> list[str]:
    """Guard + dedupe chapter stems: no traversal, the 02 file must exist."""
    out: list[str] = []
    for s in stems:
        if not isinstance(s, str) or not s or s != Path(s).name:
            raise HTTPException(400, f"非法章节名：{s!r}")
        if not (layout.split_text / f"{s}.txt").is_file():
            raise HTTPException(400, f"未找到章节文件（02_split_text/{s}.txt）。")
        if s not in out:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# rows (read-only — no 409; a missing workspace degrades to empty rows)
# --------------------------------------------------------------------------- #

@router.get("/chapters")
def list_chapters() -> dict:
    """One row per ``02_split_text`` stem (sorted), joined with the two 08_bgm
    JSON caches and the disk facts the frontend badges need:

    * ``narration_exists`` — 06 旁白 present (mixing's input);
    * ``mix_exists`` — 08_bgm/<stem>.mp3 present (a no-BGM chapter's copy2 counts);
    * ``music_missing`` — the assignment points at a music file that no longer
      exists in the library (frontend: 「⚠ 已删除」, mixing blocked, re-match allowed).
    """
    layout = get_layout()
    if layout.split_text is None:
        return {"chapters": [], "mode": "llm"}
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    analysis = (Bgm.load_analysis(layout).get("chapters") or {})
    data = Bgm.load_assignments(layout)
    chapters = data.get("chapters") or {}
    rows = []
    for stem in Bgm.list_chapter_stems(layout):
        a = analysis.get(stem)
        a_out = None
        if isinstance(a, dict):
            a_out = {c: [t for t in (a.get(c) or []) if isinstance(t, str)]
                     for c in music_engine.TAG_CATEGORIES}
            a_out["analyzed_at"] = a.get("analyzed_at", "")
            a_out["edited"] = bool(a.get("edited", False))
        e = chapters.get(stem)
        e_out = None
        music_missing = False
        if isinstance(e, dict):
            tags = e.get("tags") or {}
            e_out = {
                "tags": {c: [t for t in (tags.get(c) or []) if isinstance(t, str)]
                         for c in music_engine.TAG_CATEGORIES},
                "music": e.get("music") if isinstance(e.get("music"), str) else None,
                "locked": bool(e.get("locked", False)),
                "manual": bool(e.get("manual", False)),
                "score": e.get("score"),
                "reason": e.get("reason", ""),
                "matched_at": e.get("matched_at", ""),
            }
            if e_out["music"] and not (lib_dir / e_out["music"]).is_file():
                music_missing = True
        rows.append({
            "stem": stem,
            "narration_exists": Bgm._find_narration(layout, stem) is not None,
            "mix_exists": (layout.bgm / f"{stem}.mp3").is_file(),
            "analysis": a_out,
            "assignment": e_out,
            "music_missing": music_missing,
        })
    return {"chapters": rows, "mode": data.get("mode", "llm")}


# --------------------------------------------------------------------------- #
# LLM 章节气氛分析（PENDING 壳 + 协调者，共享 LLM 闸）
# --------------------------------------------------------------------------- #

class AnalyzeRequest(BaseModel):
    chapters: list[str]


@router.post("/analyze")
def run_analyze(req: AnalyzeRequest) -> dict:
    """Start one analysis Task per selected chapter (LLM slot = the shared
    process-wide gate; slot scope = the whole task — no check phase).

    Guards: no workspace 409 → empty selection 400 → per-stem (traversal /
    missing file) 400 → model not configured 400 → same-chapter analysis
    in-flight 409 (non-conflicting chapters allowed). Returns
    ``{"task_ids": [...], "chapters": [{stem, task_id}, ...]}``.
    """
    _common.require_workspace()
    layout = get_layout()
    stems = _validated_stems(layout, req.chapters or [])
    if not stems:
        raise HTTPException(400, "请选择要分析的章节。")
    cfg = get_config()
    if not cfg.llm.model_name:
        raise HTTPException(400, "尚未配置 LLM 模型（设置 → LLM → model_name）。")
    conflicts = [s for s in stems if s in _inflight_bgm_stems(ANALYSIS_MODULE)]
    if conflicts:
        raise HTTPException(409, "以下章节已有分析任务在途：" + "、".join(conflicts))
    set_concurrency(cfg.generation.max_concurrency)
    mgr = get_task_manager()
    created = [
        {"stem": s, "task_id": mgr.create(
            ANALYSIS_MODULE, f"{ANALYSIS_LABEL}：{s}",
            Bgm.analyze_chapter, s, cfg.llm, cfg.bgm, start=False,
        ).id}
        for s in stems
    ]
    threading.Thread(
        target=_run_bgm_coordinator,
        args=([c["task_id"] for c in created], gate), daemon=True,
    ).start()
    return {"task_ids": [c["task_id"] for c in created], "chapters": created}


# --------------------------------------------------------------------------- #
# 匹配（同步、确定性、不经任务系统）
# --------------------------------------------------------------------------- #

class MatchRequest(BaseModel):
    chapters: list[str] | None = None  # None = all existing 02 chapters
    mode: str = "llm"  # "llm" | "random" | ("segment" = 后续版本, 400)


@router.post("/match")
def run_match(req: MatchRequest) -> dict:
    """(Re-)match the selected chapters (default: ALL existing) and rewrite the
    assignments file once.

    Guards: no workspace 409 → ``mode="segment"`` 400 (placeholder feature) →
    unknown mode 400 → per-stem 400 → same-chapter ANALYSIS in-flight 409
    (would read a half-written analysis; mix in-flight is NOT blocked). Locked
    chapters are skipped wholesale (their entry is preserved verbatim).
    """
    _common.require_workspace()
    layout = get_layout()
    if req.mode == "segment":
        raise HTTPException(400, "段落级匹配为后续版本功能。")
    if req.mode not in ("llm", "random"):
        raise HTTPException(400, f"未知匹配模式：{req.mode!r}")
    if req.chapters is None:
        stems = Bgm.list_chapter_stems(layout)
        if not stems:
            raise HTTPException(400, "未找到任何章节文件（02_split_text/）。")
    else:
        stems = _validated_stems(layout, req.chapters)
        if not stems:
            raise HTTPException(400, "请选择要匹配的章节。")
    conflicts = [s for s in stems if s in _inflight_bgm_stems(ANALYSIS_MODULE)]
    if conflicts:
        raise HTTPException(409, "以下章节有分析任务在途（避免读取半成品分析）：" + "、".join(conflicts))
    return Bgm.match_stems(layout, stems, req.mode, get_config().bgm.min_match_score)


# --------------------------------------------------------------------------- #
# 手动干预（同步写）：编辑标签 / 手动选曲 / 锁定
# --------------------------------------------------------------------------- #

class ChapterUpdateRequest(BaseModel):
    tags: dict[str, list[str]] | None = None
    # Absent = untouched; null = clear (music=None); string = manual pick.
    music: str | None = None
    locked: bool | None = None


@router.put("/chapters/{stem}")
def update_chapter(stem: str, req: ChapterUpdateRequest) -> dict:
    """Apply a manual edit to one chapter (sync write, not a task):

    * ``tags`` → the analysis entry (``edited: true`` + ``edited_at``; the
      original ``analyzed_at`` is kept) and the assignment's tag snapshot;
    * ``music`` (key present) → validated against the library (missing 400);
      ``null`` clears; sets ``manual: true`` / ``score: null`` / reason
      「手动指定」;
    * ``locked`` → the lock flag (locked chapters are skipped by any re-match).
    """
    _common.require_workspace()
    layout = get_layout()
    if not stem or stem != Path(stem).name:
        raise HTTPException(400, f"非法章节名：{stem!r}")
    idx = music_engine.load_index()
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    now = datetime.now().isoformat(timespec="seconds")

    tags_norm = None
    if req.tags is not None:
        tags_norm = music_engine.normalize_track_tags(req.tags, idx["tags"])

    music_set = "music" in req.model_fields_set
    if music_set:
        m = (req.music or "").strip()
        if m:
            if m != Path(m).name:
                raise HTTPException(400, f"非法音乐文件名：{m!r}")
            if not (lib_dir / m).is_file():
                raise HTTPException(400, f"音乐库中找不到 {m}。")
        # m == "" / None → clear

    if tags_norm is not None:
        a_data = Bgm.load_analysis(layout)
        a_chapters = a_data.setdefault("chapters", {})
        prev = a_chapters.get(stem) if isinstance(a_chapters.get(stem), dict) else {}
        a_entry = {c: list(tags_norm.get(c) or []) for c in music_engine.TAG_CATEGORIES}
        a_entry["analyzed_at"] = prev.get("analyzed_at", "")
        a_entry["edited"] = True
        a_entry["edited_at"] = now
        a_chapters[stem] = a_entry
        Bgm.save_analysis(layout, a_data)

    data = Bgm.load_assignments(layout)
    chapters = data.setdefault("chapters", {})
    e = chapters.get(stem)
    if not isinstance(e, dict):
        a = (Bgm.load_analysis(layout).get("chapters") or {}).get(stem) or {}
        e = {
            "tags": {c: [t for t in (a.get(c) or []) if isinstance(t, str)]
                     for c in music_engine.TAG_CATEGORIES},
            "music": None, "locked": False, "manual": False,
            "score": None, "reason": "未匹配", "matched_at": now,
        }
    if tags_norm is not None:
        e["tags"] = {c: list(tags_norm.get(c) or []) for c in music_engine.TAG_CATEGORIES}
    if music_set:
        e["music"] = ((req.music or "").strip() or None)
        e["manual"] = True
        e["score"] = None
        e["reason"] = "手动指定"
        e["matched_at"] = now
    if req.locked is not None:
        e["locked"] = bool(req.locked)
    chapters[stem] = e
    Bgm.save_assignments(layout, data)
    return e


# --------------------------------------------------------------------------- #
# 混音（PENDING 壳 + 协调者，进程级 merge 闸）
# --------------------------------------------------------------------------- #

class MixRequest(BaseModel):
    chapters: list[str]


@router.post("/mix")
def run_mix(req: MixRequest) -> dict:
    """Start one mix Task per selected chapter (gate = the process-wide
    ``merge_gate()`` — ffmpeg/CPU/disk bound, same gate as audio merge).

    Guards: no workspace 409 → empty selection 400 → per-stem (traversal /
    missing 02 file / never matched / narration missing / music deleted) 400
    → same-chapter mix in-flight 409. A matched-but-no-BGM chapter (``music``
    null) is ALLOWED — its task takes the copy2 path (the 06 narration copied
    verbatim is a finished product).
    """
    _common.require_workspace()
    layout = get_layout()
    stems = _validated_stems(layout, req.chapters or [])
    if not stems:
        raise HTTPException(400, "请选择要混音的章节。")
    data = Bgm.load_assignments(layout)
    chapters = data.get("chapters") or {}
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    for s in stems:
        e = chapters.get(s)
        if not isinstance(e, dict):
            raise HTTPException(400, f"该章从未匹配，请先匹配（{s}）。")
        if Bgm._find_narration(layout, s) is None:
            raise HTTPException(400,
                f"未找到旁白音频（06_audio_merge/{s}.mp3），请先完成音频合并。")
        m = e.get("music")
        if m and not (lib_dir / m).is_file():
            raise HTTPException(400,
                f"音乐库中找不到 {m}，请先到「音乐库」页检查或重新匹配（{s}）。")
    conflicts = [s for s in stems if s in _inflight_bgm_stems(MIX_MODULE)]
    if conflicts:
        raise HTTPException(409, "以下章节已有混音任务在途：" + "、".join(conflicts))
    cfg = get_config()
    set_merge_concurrency(Merge.concurrency_limit())
    mgr = get_task_manager()
    created = [
        {"stem": s, "task_id": mgr.create(
            MIX_MODULE, f"{MIX_LABEL}：{s}",
            Bgm.mix_chapter, s, cfg.bgm, cfg.ffmpeg, start=False,
        ).id}
        for s in stems
    ]
    threading.Thread(
        target=_run_bgm_coordinator,
        args=([c["task_id"] for c in created], merge_gate), daemon=True,
    ).start()
    return {"task_ids": [c["task_id"] for c in created], "chapters": created}
