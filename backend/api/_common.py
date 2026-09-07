"""Shared helpers for the API routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..engines.book import decode_buffer


def read_decoded_file(path: str) -> tuple[str, str, Path]:
    """Read the file at ``path`` and auto-detect its encoding.

    The frontend is a thin client: it hands the backend an absolute file path
    (picked via the Tauri dialog); the backend does the reading / decoding.
    Returns ``(text, encoding_label, resolved_path)``.
    """
    p = Path(path)
    if not p.exists():
        raise HTTPException(400, f"文件不存在：{path}")
    if not p.is_file():
        raise HTTPException(400, f"不是一个文件：{path}")
    data = p.read_bytes()
    if not data:
        raise HTTPException(400, "文件为空。")
    try:
        text, enc = decode_buffer(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return text, enc, p


def partial_copy(model, overrides: dict):
    """Return ``model`` with only the *known* keys in ``overrides`` applied, so a
    stray/unknown key from the client can't crash the request."""
    valid = set(model.model_fields.keys())
    clean = {k: v for k, v in (overrides or {}).items() if k in valid}
    return model.model_copy(update=clean) if clean else model
