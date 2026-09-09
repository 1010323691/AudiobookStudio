"""TTS + character-voice + batch + merge endpoints (modules: TTS / 角色配音 / 音频合成 / 音频合并).

``GET /status`` reports readiness. The stage endpoints (``POST /prepare-voices``,
``GET /voices``, ``POST /batch``, ``POST /merge``) each start a long-running
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

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.paths import get_layout
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

class PrepareVoicesRequest(BaseModel):
    # None -> every character in the script; a list -> only those (single-char regen).
    speakers: list[str] | None = None
    # True -> skip characters already present in voice_config.json.
    new_only: bool = False
    # speaker -> user-supplied voice description (skips the LLM for that character).
    overrides: dict[str, str] | None = None


@router.post("/prepare-voices")
def prepare_voices(req: PrepareVoicesRequest) -> dict:
    _common.require_workspace()
    if req.speakers:
        label = f"重新生成 {len(req.speakers)} 个角色声音"
    elif req.new_only:
        label = "准备新增角色声音"
    else:
        label = "准备所有角色声音"
    task = get_task_manager().create(
        "voices", label,
        V.prepare,
        req.speakers, req.new_only, req.overrides or {},
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


@router.get("/voices")
def list_voices() -> dict:
    """Detected characters + their voice-config state (ready/pending) + preview paths.

    ``preview`` is a path relative to ``04_voice_profiles/`` so the UI can play it
    through the shared ``download/04_voice_profiles/{name}`` route; empty when there
    is nothing to preview.
    """
    layout = get_layout()
    out_voices = layout.voice_profiles
    script_path = layout.parsed_json / "annotated_script.json"
    vc_path = out_voices / "voice_config.json"

    has_script = False
    order: list[str] = []
    counts: dict[str, int] = {}
    if script_path.exists():
        try:
            script = json.loads(script_path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt script just means "no speakers"
            script = []
        if isinstance(script, list) and script:
            has_script = True
            for entry in script:
                sp = (entry.get("speaker") or entry.get("type") or "").strip()
                if not sp:
                    continue
                if sp not in counts:
                    counts[sp] = 0
                    order.append(sp)
                counts[sp] += 1

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
            "type": vtype,
            "alias_of": alias_of,
            "description": entry.get("description", ""),
            "preview": preview,
        })

    return {
        "has_script": has_script,
        "script_path": str(script_path),
        "voice_config_path": str(vc_path),
        "speakers": speakers,
    }


# ---------------------------------------------------------------------------
# 音频合成 + 音频合并
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    # None -> every script line; a list of line indices -> only those.
    indices: list[int] | None = None


@router.post("/batch")
def run_batch(req: BatchRequest) -> dict:
    _common.require_workspace()
    label = "音频合成（全部）" if not req.indices else f"音频合成（{len(req.indices)} 段）"
    task = get_task_manager().create("tts-batch", label, Batch.synthesize, req.indices)
    return {"task_id": task.id}


class MergeRequest(BaseModel):
    m4b: bool = False  # M4B output is a later phase; MP3 is produced for now.


@router.post("/merge")
def run_merge(req: MergeRequest) -> dict:
    _common.require_workspace()
    label = "合并 M4B" if req.m4b else "合并音频（Merge）"
    task = get_task_manager().create("merge", label, Merge.run, req.m4b)
    return {"task_id": task.id}
