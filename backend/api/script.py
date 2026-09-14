"""Script-generation endpoints (module: 文本解析).

``POST /generate-files`` takes the names of one or more files in the workspace's
``02_split_text/`` and starts **one independent Task per file** that runs the
``engines/script.py`` LLM → JSON pipeline (LLM / prompt / generation settings come from
the unified config; empty prompts resolve to the bundled defaults). Concurrency is
bounded by the shared gate (``config.generation.max_concurrency``). It returns the
created ``task_ids``; the UI streams each task over SSE (per-chunk progress + logs) and
reads the resulting ``{speaker, text, instruct}`` entries from ``task.result``. Each
file's JSON is written to ``03_parsed_json/<source-stem>.json`` (one file per source)
and served by the shared ``GET /api/files/download/03_parsed_json/{name}`` route, so it
drops straight into the TTS flow.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import PromptsConfig, get_config
from ..core.concurrency import set_concurrency
from ..core.paths import get_layout
from ..core.tasks import get_task_manager
from ..engines import mix_check as M
from ..engines import script as S
from ..engines import speaker_check as C
from ..engines.script_prompts import load_default_prompts
from . import _common

router = APIRouter(prefix="/api/script", tags=["script"])


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


def _resolve_split_file(name: str) -> Path:
    """Resolve a ``02_split_text`` file name to an absolute path, rejecting traversal.

    The frontend sends bare file names (from ``GET /api/files/list/02_split_text``);
    anything that resolves outside the ``02_split_text`` directory is refused.
    """
    base = get_layout().split_text
    if base is None:
        raise HTTPException(409, "尚未设置工作空间——请先在「开始」页选择文件夹。")
    candidate = base / name
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, f"非法文件路径：{name}")
    if not candidate.is_file():
        raise HTTPException(400, f"文件不存在：{name}")
    return candidate


def _resolve_parsed_file(name: str, prefer_latest: bool = False) -> Path:
    """Resolve a ``03_parsed_json`` file name to an absolute path, rejecting traversal.

    The frontend sends the base ``<stem>.json`` name of an already-parsed chapter. A name
    that already ends in ``_checked.json`` is reduced to its base, so re-checking re-derives
    from the original and never yields ``..._checked_checked.json``. Anything that resolves
    outside ``03_parsed_json`` is refused.

    ``prefer_latest`` (角色匹配检查): when the ``_checked`` artifact exists and is NOT older
    than the base (mtime guard — a re-parse leaves a stale ``_checked`` behind, which must
    not shadow the fresh base), it is returned so the stage updates the mix check's output
    in place; otherwise the base is returned (the stage then (re)creates ``_checked``).
    段落混合检查 always passes ``prefer_latest=False`` — it re-derives from the pristine base.
    """
    if name.endswith("_checked.json"):
        name = name[: -len("_checked.json")] + ".json"
    base = get_layout().parsed_json
    if base is None:
        raise HTTPException(409, "尚未设置工作空间——请先在「开始」页选择文件夹。")
    candidate = base / name
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, f"非法文件路径：{name}")
    if not candidate.is_file():
        raise HTTPException(400, f"文件不存在：{name}")
    if prefer_latest:
        checked = candidate.with_name(candidate.stem + "_checked.json")
        if checked.is_file():
            try:
                if checked.stat().st_mtime >= candidate.stat().st_mtime:
                    return checked
            except OSError:
                pass  # unreadable stat → fall back to the base
    return candidate


class GenerateFilesRequest(BaseModel):
    files: list[str]  # 02_split_text 下的文件名（不含路径）


@router.post("/generate-files")
def generate_files(req: GenerateFilesRequest) -> dict:
    """Start one independent parse Task per selected ``02_split_text`` file.

    Each file runs the same LLM → JSON pipeline as its own task (independent LLM
    requests / status / output), bounded by the shared concurrency gate
    (``config.generation.max_concurrency``). Returns the created task ids so the UI can
    stream each one over SSE.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))  # dedupe, preserving order
    if not names:
        raise HTTPException(400, "请选择要解析的文件。")
    cfg = get_config()
    set_concurrency(cfg.generation.max_concurrency)  # size the shared gate for this batch
    prompts = _resolved_prompts()
    created = []
    for name in names:
        path = _resolve_split_file(name)
        task = get_task_manager().create(
            "script", f"文本解析（{name}）",
            S.generate_file,
            str(path), cfg.llm, prompts, cfg.generation,
        )
        created.append({"file": name, "task_id": task.id})
    return {"task_ids": [c["task_id"] for c in created], "files": created}


class CheckFilesRequest(BaseModel):
    files: list[str]  # 03_parsed_json 下的基文件名 <stem>.json（不含路径）


@router.post("/check-files")
def check_files(req: CheckFilesRequest) -> dict:
    """Start one 角色匹配检查 Task per selected parsed file (``<stem>.json``).

    Each file is re-judged entry-by-entry (a ``±N`` context window per entry,
    ``N = speaker_check.context_window``). Input = the ``_checked`` artifact when it is
    fresh (the 段落混合检查's output; mtime ≥ the base), else the base; the result updates
    ``<stem>_checked.json`` IN PLACE, changing only ``speaker`` — the original
    ``<stem>.json`` is never modified. Bounded by the shared concurrency gate
    (``config.generation.max_concurrency``). Returns the created task ids so the UI can
    stream each one over SSE.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))  # dedupe, preserving order
    if not names:
        raise HTTPException(400, "请选择要检查的文件。")
    cfg = get_config()
    set_concurrency(cfg.generation.max_concurrency)  # share the LLM gate with parsing
    created = []
    for name in names:
        path = _resolve_parsed_file(name, prefer_latest=True)
        task = get_task_manager().create(
            "speaker-check", f"角色匹配检查（{name}）",
            C.check_file,
            str(path), cfg.llm, cfg.speaker_check, cfg.generation,
        )
        created.append({"file": name, "task_id": task.id})
    return {"task_ids": [c["task_id"] for c in created], "files": created}


class MixFilesRequest(BaseModel):
    files: list[str]  # 03_parsed_json 下的基文件名 <stem>.json（不含路径）


@router.post("/mix-files")
def mix_files(req: MixFilesRequest) -> dict:
    """Start one 段落混合检查 Task per selected parsed file (``<stem>.json``).

    Each file is audited entry-by-entry (the SAME batch geometry as 角色匹配检查 —
    ``speaker_check.batch_size`` / ``context_window``) and rewritten to ``<stem>_checked.json``:
    multi-subject entries (narration mixed with a character's line, or lines of 2+
    characters) are split into separate entries in original order, and punctuation-only
    entries are deleted (deterministically, no LLM). The original ``<stem>.json`` is never
    modified; 角色匹配检查 later updates the same ``_checked`` file in place. Bounded by the
    shared concurrency gate. Returns the created task ids so the UI can stream each one.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))  # dedupe, preserving order
    if not names:
        raise HTTPException(400, "请选择要混合检查的文件。")
    cfg = get_config()
    set_concurrency(cfg.generation.max_concurrency)  # share the LLM gate with parsing
    created = []
    for name in names:
        path = _resolve_parsed_file(name)  # BASE only — the mix check re-derives from the base
        task = get_task_manager().create(
            "mix-check", f"段落混合检查（{name}）",
            M.mix_check_file,
            str(path), cfg.llm, cfg.mix_check, cfg.speaker_check, cfg.generation,
        )  # mix (prompts) + speaker_check (batch geometry, shared by design) + generation
        created.append({"file": name, "task_id": task.id})
    return {"task_ids": [c["task_id"] for c in created], "files": created}
