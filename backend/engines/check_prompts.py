"""Default Speaker-check prompts — port of the ``script_prompts`` pattern.

Reads the bundled ``backend/resources/default_check_prompts.txt`` and splits it on
``---SEPARATOR---`` into a ``(system_prompt, user_prompt_template)`` pair. The user
template carries a ``{context}`` placeholder that the check engine fills with the
per-entry context window. An mtime cache picks up edits without a restart. Config-supplied
check prompts (``config.speaker_check``) override these defaults when non-empty.

This is deliberately a separate file / loader from the 解析 prompts
(``script_prompts.py`` / ``default_prompts.txt``) so the two stay fully independent.
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/engines/check_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_check_prompts.txt"

_prompt_cache: dict = {"mtime": None, "prompts": None}


def load_default_check_prompts() -> tuple[str, str]:
    """Read ``default_check_prompts.txt`` → ``(system_prompt, user_prompt_template)``.

    Mirrors ``script_prompts.load_default_prompts`` with an mtime-based cache so edits
    to the file are picked up without restarting the app.
    """
    if not _PROMPTS_FILE.exists():
        raise RuntimeError(
            f"default_check_prompts.txt not found at {_PROMPTS_FILE}. "
            "This file is required for Speaker-check prompt defaults."
        )

    mtime = os.path.getmtime(_PROMPTS_FILE)
    if _prompt_cache["mtime"] == mtime and _prompt_cache["prompts"] is not None:
        return _prompt_cache["prompts"]

    try:
        raw = _PROMPTS_FILE.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error reading default_check_prompts.txt: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            "default_check_prompts.txt is malformed: expected exactly one '---SEPARATOR---' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _prompt_cache["mtime"] = mtime
    _prompt_cache["prompts"] = prompts
    return prompts


# Cached at import time — the fallbacks used when the config carries no custom check prompts.
DEFAULT_CHECK_SYSTEM_PROMPT, DEFAULT_CHECK_USER_PROMPT = load_default_check_prompts()
