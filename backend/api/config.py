"""Config endpoints — read / write the unified persistent config (requirement #4)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core import config as core_config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config() -> dict:
    """Return the full config (all sections)."""
    return core_config.get_config().model_dump()


@router.put("")
def put_config(patch: dict) -> dict:
    """Merge a (possibly partial) patch into the config and persist it.

    The request body *is* the patch, e.g. ``{"book": {"target_chars": 120000},
    "log": {"level": "DEBUG"}}``. Returns the resulting full config.
    """
    try:
        cfg = core_config.update_config(patch)
    except Exception as exc:  # noqa: BLE001 — surface a clean 400
        raise HTTPException(400, f"配置无效：{exc}")
    return cfg.model_dump()
