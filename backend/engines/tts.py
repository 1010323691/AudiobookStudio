"""TTS synthesis engine — placeholder.

The console's pipeline is 文本排版 → 分册 → **TTS** → 音频分集, so the four modules
need a TTS stage in the nav even though the TTS engine is not part of this version.
This module marks the seam: the API route, the config section (``config.tts``) and
the nav entry all exist, so wiring a real TTS provider later is an *additive*
change — implement :func:`synthesize`, flip :data:`IMPLEMENTED`, and the pipeline
flows through it without any restructuring.
"""
from __future__ import annotations

IMPLEMENTED = False
NOT_READY_MSG = "TTS 合成尚未实装（本版为占位模块，即将推出）。"


def synthesize(text: str, *args, **kwargs) -> None:
    """Placeholder — raises until a real TTS provider is wired in."""
    raise NotImplementedError(NOT_READY_MSG)
