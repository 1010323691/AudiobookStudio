"""TTS endpoints (module: TTS 合成).

``GET /status`` reports readiness. ``POST /synthesize`` starts a long-running Task
that drives the isolated Qwen3-TTS engine (see ``backend/engines/tts.py``) and
returns ``{"task_id"}``; the UI streams the task and plays the resulting mp3 via the
shared ``GET /api/files/download/tts/{name}`` route. A failing TTS task is marked
failed and isolated — it never takes the console down (requirement #7).
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.paths import get_layout
from ..core.tasks import get_task_manager
from ..engines import tts as T

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Let the UI show an accurate badge (ready vs. engine-not-installed)."""
    return {
        "implemented": T.IMPLEMENTED,
        "message": T.NOT_READY_MSG if not T.IMPLEMENTED else "TTS 合成可用。",
    }


class SynthesizeRequest(BaseModel):
    text: str  # 要合成的文本
    speaker: str = ""  # empty -> config.tts.speaker
    language: str = ""  # empty -> config.tts.language
    instruct: str = ""  # 风格 / 演绎指令


@router.post("/synthesize")
def synthesize(req: SynthesizeRequest) -> dict:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "请输入要合成的文本。")
    # Name the output so rapid requests never collide (timestamp + short id).
    out_path = get_layout().output_tts / f"tts_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.mp3"
    task = get_task_manager().create(
        "tts", f"TTS 合成（{len(text)} 字）",
        T.synthesize,
        text, str(out_path), req.speaker, req.language, req.instruct,
    )
    return {"task_id": task.id}
