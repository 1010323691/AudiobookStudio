"""Default 段落混合检查 (mix-check) prompts — port of the ``check_prompts`` pattern.

Reads the bundled ``backend/resources/default_mix_check_prompts.txt`` and splits it on
``---SEPARATOR---`` into a ``(system_prompt, user_prompt_template)`` pair. The user
template carries a ``{context}`` placeholder that the mix-check engine fills with the
per-entry context window (the engine appends the book-wide character roster and the
actual target-count note after the template). An mtime cache picks up edits without a
restart. Config-supplied mix prompts (``config.mix_check``) override these defaults when
non-empty.

This is deliberately a separate file / loader from the 解析 prompts
(``script_prompts.py`` / ``default_prompts.txt``) and the 角色匹配检查 prompts
(``check_prompts.py`` / ``default_check_prompts.txt``) so the three stages stay fully
independent.
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/engines/mix_check_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_mix_check_prompts.txt"

_prompt_cache: dict = {"mtime": None, "prompts": None}


def load_default_mix_prompts() -> tuple[str, str]:
    """Read ``default_mix_check_prompts.txt`` → ``(system_prompt, user_prompt_template)``.

    Mirrors ``check_prompts.load_default_check_prompts`` with an mtime-based cache so
    edits to the file are picked up without restarting the app.
    """
    if not _PROMPTS_FILE.exists():
        raise RuntimeError(
            f"default_mix_check_prompts.txt not found at {_PROMPTS_FILE}. "
            "This file is required for the mix-check prompt defaults."
        )

    mtime = os.path.getmtime(_PROMPTS_FILE)
    if _prompt_cache["mtime"] == mtime and _prompt_cache["prompts"] is not None:
        return _prompt_cache["prompts"]

    try:
        raw = _PROMPTS_FILE.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error reading default_mix_check_prompts.txt: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            "default_mix_check_prompts.txt is malformed: expected exactly one '---SEPARATOR---' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _prompt_cache["mtime"] = mtime
    _prompt_cache["prompts"] = prompts
    return prompts


# Cached at import time — the fallbacks used when the config carries no custom mix prompts.
DEFAULT_MIX_SYSTEM_PROMPT, DEFAULT_MIX_USER_PROMPT = load_default_mix_prompts()
