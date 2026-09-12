"""TTS + character-voice + batch + merge endpoints (modules: TTS / 角色配音 / 音频合成 / 音频合并).

``GET /status`` reports readiness. The stage endpoints (``POST /prepare-foundations``,
``POST /make-clones``, ``GET /voices``, ``POST /batch``, ``POST /merge``) each start a
long-running
:class:`Task` that drives the isolated Qwen3-TTS engine (see
``backend/engines/tts.py`` / ``voices.py`` / ``tts_batch.py`` / ``merge.py``) and
return ``{"task_id"}``; the UI streams the task's progress/logs over SSE and plays
the resulting audio via the shared ``GET /api/files/download/05_audio_chunk/{name}``
route. Any failing task is marked failed and isolated — it never takes the console
down (requirement #7).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.paths import ALL_PARSED_JSON, get_layout, resolve_parsed_json, resolve_parsed_json_all
from ..core.tasks import get_task_manager
from ..engines import merge as Merge
from ..engines import tts as T
from ..engines import tts_batch as Batch
from ..engines import voices as V
from . import _common

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Let the UI show an accurate badge (ready vs. engine-not-installed)."""
    return {
        "implemented": T.IMPLEMENTED,
        "message": T.NOT_READY_MSG if not T.IMPLEMENTED else "TTS 合成可用。",
    }


# ---------------------------------------------------------------------------
# 角色配音（voice preparation）
# ---------------------------------------------------------------------------

class PrepareFoundationsRequest(BaseModel):
    # Phase 1 (LLM only): None -> every character; a list -> only those (single-char regen).
    speakers: list[str] | None = None
    # True -> regenerate only characters without a foundation yet.
    new_only: bool = False
    # speaker -> user-supplied voice description (skips the LLM for that character).
    overrides: dict[str, str] | None = None
    # Which parsed JSON (in 03_parsed_json/) to read; None -> most recent (resolve_parsed_json).
    script: str | None = None


class MakeClonesRequest(BaseModel):
    # Phase 2 (TTS only): None -> every foundation-bearing character; a list -> only those.
    speakers: list[str] | None = None
    # True -> limit to characters not yet holding a usable clone (also retries failed ones).
    new_only: bool = False
    # Number of parallel TTS subprocesses (each loads the model once; VRAM scales with it).
    concurrency: int | None = None
    # Which parsed JSON to read for the character set; None -> most recent.
    script: str | None = None


def _scope_suffix(script: str | None) -> str:
    """A short task-label suffix naming the parsed-JSON scope ("" for the default, most-recent)."""
    return "（全部解析文件）" if script == ALL_PARSED_JSON else ""


@router.post("/prepare-foundations")
def prepare_foundations(req: PrepareFoundationsRequest) -> dict:
    """Start Phase 1: batch-generate every character's voice foundation (LLM only, no TTS)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新生成 {len(req.speakers)} 个角色语音推理基础{suffix}"
    elif req.new_only:
        label = f"生成新增角色语音推理基础{suffix}"
    else:
        label = f"生成所有角色语音推理基础{suffix}"
    task = get_task_manager().create(
        "voices-foundation", label,
        V.prepare_foundations,
        req.speakers, req.new_only, req.overrides or {}, req.script,
    )
    return {"task_id": task.id}


@router.post("/make-clones")
def make_clones(req: MakeClonesRequest) -> dict:
    """Start Phase 2: batch-render every character's clone seed WAV (TTS only, no LLM)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新制作 {len(req.speakers)} 个角色克隆音频{suffix}"
    elif req.new_only:
        label = f"制作新增角色克隆音频{suffix}"
    else:
        label = f"制作所有角色克隆音频{suffix}"
    task = get_task_manager().create(
        "voices-clone", label,
        V.make_clones,
        req.speakers, req.new_only, req.concurrency, req.script,
    )
    return {"task_id": task.id}


def _voice_usable(entry: dict) -> bool:
    """Whether a stored voice entry actually yields a usable voice at synthesis time."""
    vtype = entry.get("type", "")
    if vtype == "clone":
        return bool(entry.get("ref_audio"))
    if vtype == "design":
        return bool((entry.get("description") or "").strip())
    if vtype == "custom":
        return True  # uses a named preset / default voice
    return False


def _foundation_status(entry: dict) -> str:
    """The character's voice-foundation state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``foundation_status`` written by Phase 1; otherwise infers it so
    entries created before the two-phase split (a stored description ⇒ a foundation exists)
    still report sensibly.
    """
    entry = entry or {}
    st = entry.get("foundation_status")
    if st in ("done", "failed"):
        return st
    if (entry.get("description") or "").strip():
        return "done"
    return "none"


def _clone_status(entry: dict) -> str:
    """The character's clone-audio state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``clone_status`` written by Phase 2; otherwise a stored
    ``type: clone`` + ``ref_audio`` (a usable clone seed) infers ``done``.
    """
    st = entry.get("clone_status")
    if st in ("done", "failed"):
        return st
    if entry.get("type") == "clone" and entry.get("ref_audio"):
        return "done"
    return "none"


def _fold_script(order: list[str], counts: dict[str, int], data) -> bool:
    """Fold one parsed script (a list of entries) into the shared ``order``/``counts``.

    Dedupes by speaker name (falling back to ``type``), sums line counts, and keeps
    first-appearance order — the same folding the single-file path did inline, now shared
    with the whole-book aggregate. Returns True when the file held a non-empty list (the
    ``has_script`` signal).
    """
    if not isinstance(data, list) or not data:
        return False
    for entry in data:
        sp = (entry.get("speaker") or entry.get("type") or "").strip()
        if not sp:
            continue
        if sp not in counts:
            counts[sp] = 0
            order.append(sp)
        counts[sp] += 1
    return True


@router.get("/voices")
def list_voices(script: str | None = None) -> dict:
    """Detected characters + their voice-config state (ready/pending) + preview paths.

    ``script`` names the parsed JSON (in ``03_parsed_json/``) to read; when omitted the
    most recently written one is used (see ``resolve_parsed_json``). With no workspace
    set it degrades to an empty result. ``preview`` is a path relative to
    ``04_voice_profiles/`` so the UI can play it through the shared
    ``download/04_voice_profiles/{name}`` route; empty when there is nothing to preview.
    """
    layout = get_layout()
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"has_script": False, "script_path": "", "voice_config_path": "", "speakers": []}
    out_voices = layout.voice_profiles
    vc_path = out_voices / "voice_config.json"

    # Which script(s) to read: every parsed JSON (the whole-book "all files" request) or
    # the single named / most-recent one. ``script_path_out`` is only a display label.
    if script == ALL_PARSED_JSON:
        script_paths = resolve_parsed_json_all()
        script_path_out = ""
    else:
        script_paths = [resolve_parsed_json(script)]
        script_path_out = str(script_paths[0])

    has_script = False
    order: list[str] = []
    counts: dict[str, int] = {}
    for sp in script_paths:
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt file just contributes no speakers
            data = []
        if _fold_script(order, counts, data):
            has_script = True

    voice_config: dict = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception:  # noqa: BLE001
            voice_config = {}

    names = order if has_script else list(voice_config.keys())
    speakers: list[dict] = []
    for sp in names:
        entry = voice_config.get(sp, {})
        vtype = entry.get("type", "")
        alias_of = entry.get("alias_of", "")
        ready = bool(alias_of) or _voice_usable(entry)
        preview = ""
        ref = entry.get("ref_audio", "")
        if ref:
            try:
                p = Path(ref)
                if p.exists():
                    try:
                        preview = str(p.relative_to(out_voices))
                    except ValueError:
                        preview = str(p)
            except Exception:  # noqa: BLE001
                pass
        speakers.append({
            "name": sp,
            "line_count": counts.get(sp, 0),
            "status": "ready" if ready else "pending",
            "foundation_status": _foundation_status(entry),
            "clone_status": _clone_status(entry),
            "type": vtype,
            "alias_of": alias_of,
            "description": entry.get("description", ""),
            "preview": preview,
        })

    return {
        "has_script": has_script,
        "script_path": script_path_out,
        "voice_config_path": str(vc_path),
        "speakers": speakers,
    }


# ---------------------------------------------------------------------------
# 音频合成 + 音频合并
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    # None -> every script line; a list of line indices -> only those.
    indices: list[int] | None = None
    # Which parsed JSON (in 03_parsed_json/) to synthesize; None -> most recent.
    script: str | None = None
    # Concurrent segments (1..32); None -> the persisted default (config.tts.batch_concurrency).
    concurrency: int | None = None
    # True -> re-synthesize EVERY line (clears the resume skip); False (default) -> resume
    # (synthesize only the not-yet-done segments, skipping existing audio).
    force_all: bool = False


@router.post("/batch")
def run_batch(req: BatchRequest) -> dict:
    _common.require_workspace()
    if req.script == ALL_PARSED_JSON:
        raise HTTPException(status_code=400, detail="音频合成仅支持单个解析 JSON（“全部”只用于「角色配音」）。")
    if req.force_all:
        label = "音频合成（重新全部）"
    elif req.indices:
        label = f"音频合成（{len(req.indices)} 段）"
    else:
        label = "音频合成（续合）"
    if req.concurrency:
        label += f" · 并发 {req.concurrency}"
    task = get_task_manager().create("tts-batch", label, Batch.synthesize, req.indices, req.script, req.concurrency, req.force_all)
    return {"task_id": task.id}


@router.get("/batch-status")
def batch_status(script: str | None = None) -> dict:
    """The chosen script's synthesis progress: ``{total, completed, remaining}``.

    ``completed`` counts segments already synthesized (a manifest entry ``ok`` whose file is
    still on disk) — the number behind the 待合成 card's 【已合成 / 总段落】, refreshed live while
    a run streams (the manifest is written incrementally). ``script`` names the parsed JSON to
    read; omitted -> most recent. Degrades to zeros with no workspace / no script, like ``/voices``.
    """
    layout = get_layout()
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"total": 0, "completed": 0, "remaining": 0}
    src = resolve_parsed_json(script)
    if not src.exists():
        return {"total": 0, "completed": 0, "remaining": 0}
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports nothing
        return {"total": 0, "completed": 0, "remaining": 0}
    if not isinstance(data, list) or not data:
        return {"total": 0, "completed": 0, "remaining": 0}
    out_dir = layout.audio_chunk / Batch.package_for(src)
    return Batch.count_completion(Batch._build_segments(data), Batch.load_manifest(out_dir))


class MergeRequest(BaseModel):
    m4b: bool = False  # M4B output is a later phase; MP3 is produced for now.
    # Which package (a sub-folder in 05_audio_chunk/, one per source JSON) to merge;
    # None -> the most recent package (see merge._find_manifest).
    package: str | None = None


@router.post("/merge")
def run_merge(req: MergeRequest) -> dict:
    _common.require_workspace()
    label = ("合并 M4B" if req.m4b else "合并音频（Merge）") + (f"：{req.package}" if req.package else "")
    task = get_task_manager().create("merge", label, Merge.run, req.m4b, req.package)
    return {"task_id": task.id}
