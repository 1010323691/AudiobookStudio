"""Unified directory layout for the AudiobookStudio backend.

Layout (under the working directory)::

    input/
    output/{text,books,tts,audio}
    temp/
    logs/
    config/

The working directory defaults to ``<project>/workspace`` and can be overridden via
the ``paths.working_dir`` setting. The app's own config file always lives in the
*default* workspace so it stays stable regardless of the working directory.
"""
from __future__ import annotations

from pathlib import Path

# backend/core/paths.py  ->  parents[0]=core  parents[1]=backend  parents[2]=<project>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace"


class Layout:
    """Resolved, on-disk directory tree for one working directory."""

    def __init__(self, base: Path):
        self.base = base
        self.input = base / "input"
        self.output = base / "output"
        self.output_text = self.output / "text"
        self.output_books = self.output / "books"
        self.output_tts = self.output / "tts"
        self.output_audio = self.output / "audio"
        self.temp = base / "temp"
        self.logs = base / "logs"
        self.config = base / "config"

    def ensure(self) -> "Layout":
        for p in (
            self.input,
            self.output,
            self.output_text,
            self.output_books,
            self.output_tts,
            self.output_audio,
            self.temp,
            self.logs,
            self.config,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_layout() -> Layout:
    """Return the ensured Layout for the configured working directory."""
    from .config import get_config  # local import to avoid a cycle

    cfg = get_config()
    working = (cfg.paths.working_dir or "").strip()
    if working:
        base = Path(working)
        if not base.is_absolute():
            base = PROJECT_ROOT / base
    else:
        base = DEFAULT_WORKSPACE
    return Layout(base).ensure()
