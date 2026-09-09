"""Unified logging: a rotating file (``<workspace>/logs/app.log``) plus console.

The file handler *follows the active workspace*: it is (re)targeted whenever the
workspace changes, and is absent (console-only) while no workspace is set — the
app never writes logs into the project directory. The root-logger level is taken
from the config so it can be changed at runtime (requirement #6); routine
``uvicorn.access`` lines are demoted to keep the log readable. (Modules currently
report through the task system's live logs; this setup exists for anything that
does use the standard ``logging``.)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_console_installed = False
_file_handler: RotatingFileHandler | None = None
_file_path: str | None = None

_FMT = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")


def setup_logging(logs_dir: Path | None, level: str = "INFO") -> None:
    """Point logging at ``logs_dir/app.log`` (or console-only when ``logs_dir`` is
    ``None``). Safe to call repeatedly — at startup and on every workspace change:
    only the file handler is re-targeted, the console handler is kept, and a level
    change is applied to the root logger in place.
    """
    global _console_installed, _file_handler, _file_path
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not _console_installed:
        console = logging.StreamHandler()
        console.setFormatter(_FMT)
        root.addHandler(console)
        # Keep routine request access lines out of the log.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        _console_installed = True

    target = str(logs_dir / "app.log") if logs_dir is not None else None
    if target == _file_path:
        return  # already pointed here
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    _file_path = target
    if target is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(_FMT)
        root.addHandler(handler)
        _file_handler = handler
