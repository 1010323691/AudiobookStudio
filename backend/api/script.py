"""Script-generation endpoints (module: 文本解析).

``POST /generate`` takes novel text, reads the LLM / prompt / generation settings from
the unified config (empty prompts resolve to the bundled defaults) and starts an
in-process Task that runs the ``engines/script.py`` LLM → JSON pipeline. It returns
``{"task_id"}``; the UI streams the task (per-chunk progress + logs over SSE) and reads
the resulting ``{speaker, text, instruct}`` entries from ``task.result``. The JSON is
written to the workspace's ``03_parsed_json/annotated_script.json`` and served by the
shared ``GET /api/files/download/03_parsed_json/{name}`` route, so it drops straight
into the TTS flow.

``GET /result`` re-reads the last written file (convenience after a page reload).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import PromptsConfig, get_config
from ..core.paths import get_layout
from ..core.tasks import get_task_manager
from ..engines import script as S
from ..engines.script_prompts import load_default_prompts
from . import _common

router = APIRouter(prefix="/api/script", tags=["script"])


class GenerateRequest(BaseModel):
    text: str  # 小说原文


def _resolved_prompts() -> PromptsConfig:
    """The configured prompts, with empty fields filled from the bundled defaults.

    Mirrors the source ``get_config`` behaviour. Returns a copy, so the shared
    in-memory config is never mutated as a side effect of a generate call.
    """
    prompts = get_config().prompts
    system_prompt, user_prompt = load_default_prompts()
    return prompts.model_copy(update={
        "system_prompt": prompts.system_prompt or system_prompt,
        "user_prompt": prompts.user_prompt or user_prompt,
    })


@router.post("/generate")
def generate(req: GenerateRequest) -> dict:
    _common.require_workspace()
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "请输入要解析的文本。")
    cfg = get_config()
    task = get_task_manager().create(
        "script", f"文本解析（{len(text)} 字）",
        S.generate,
        text, cfg.llm, _resolved_prompts(), cfg.generation,
    )
    return {"task_id": task.id}


@router.get("/result")
def result() -> dict:
    """Return the last generated script (read from the written JSON file)."""
    path = get_layout().parsed_json / "annotated_script.json"
    if not path.is_file():
        return {"entries": [], "output_path": "", "count": 0, "speakers": []}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — surface a clean 500
        raise HTTPException(500, f"读取 annotated_script.json 失败：{exc}")
    if not isinstance(entries, list):
        return {"entries": [], "output_path": str(path), "count": 0, "speakers": []}
    speakers = sorted({(e.get("speaker") or "UNKNOWN") for e in entries if isinstance(e, dict)})
    return {
        "entries": entries,
        "output_path": str(path),
        "count": len(entries),
        "speakers": speakers,
    }
