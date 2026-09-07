"""Unified logging: a rotating file (``logs/app.log``) plus console.

Every module logs through :func:`get_logger`; the level is taken from the
config so it can be changed at runtime (requirement #6).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    global _configured
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if _configured:
        # Level may have changed; handlers are already attached.
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")

    file_handler = RotatingFileHandler(
        logs_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Keep routine request access lines out of the log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
