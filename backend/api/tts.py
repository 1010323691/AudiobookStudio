"""TTS endpoints (module: TTS 合成) — placeholder.

The routes exist so the four-module nav is complete and the pipeline seam is wired;
they report "not implemented" (501) until a real provider is added. Nothing here
blocks or affects the other three modules (requirement #7: error isolation).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..engines import tts as T

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Let the UI show an accurate "即将推出" badge rather than a dead button."""
    return {
        "implemented": T.IMPLEMENTED,
        "message": T.NOT_READY_MSG if not T.IMPLEMENTED else "TTS 合成可用。",
    }


class SynthesizeRequest(BaseModel):
    path: str
    # Reserved for the future provider (mirrors config.tts):
    # voice: str = ""
    # model: str = ""
    # concurrency: int = 1


@router.post("/synthesize")
def synthesize(req: SynthesizeRequest) -> None:
    if not T.IMPLEMENTED:
        raise HTTPException(501, T.NOT_READY_MSG)
    T.synthesize(req.path)
