"""Unified, persistent configuration (``config/app.json``).

One JSON file, loaded at startup and rewritten on every save, so the console
restores its settings on restart (requirement #4). The file lives in a fixed
location (the default workspace) independent of the working directory, which
avoids a config-file-chasing-its-own-setting cycle.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from pydantic import BaseModel, Field

from .paths import DEFAULT_WORKSPACE


class PathsConfig(BaseModel):
    working_dir: str = ""  # empty -> default workspace


class TextConfig(BaseModel):
    # 10 formatting toggles (ported from TextFormatter's ``tf.config``).
    keep_single_space: bool = False
    sentence_break: bool = True
    dialogue_separate: bool = True
    detect_chapters: bool = True
    punct_ellipsis: bool = True
    punct_repeated: bool = True
    punct_lone_ascii: bool = False
    punct_quotes: bool = False
    punct_dash: bool = False
    live: bool = True  # UI-only: reformat immediately on change


class BookConfig(BaseModel):
    target_chars: int = 100000  # per-volume target (from BookChunker)


class AudioConfig(BaseModel):
    target_duration: str = "10:00"
    naming_format: str = "第 {} 集"
    start_number: str = "1"
    smart_align: bool = True
    align_tolerance: int = 15


class TTSConfig(BaseModel):
    # Placeholder for the future TTS module.
    enabled: bool = False
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    voice: str = ""
    concurrency: int = 1


class FFmpegConfig(BaseModel):
    ffmpeg_path: str = ""  # empty -> resolve from PATH
    ffprobe_path: str = ""  # empty -> resolve from PATH


class LogConfig(BaseModel):
    level: str = "INFO"


class UIConfig(BaseModel):
    theme: str = "system"  # system | light | dark


class AppConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    book: BookConfig = Field(default_factory=BookConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


_CONFIG_FILE = DEFAULT_WORKSPACE / "config" / "app.json"

_lock = threading.Lock()
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Return the in-memory config, loading from disk on first access."""
    global _config
    with _lock:
        if _config is None:
            _config = AppConfig()
            if _CONFIG_FILE.exists():
                try:
                    _config = AppConfig.model_validate(
                        json.loads(_CONFIG_FILE.read_text("utf-8"))
                    )
                except Exception:
                    _config = AppConfig()
        return _config


def _deep_update(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def update_config(patch: dict[str, Any]) -> AppConfig:
    """Merge a (possibly partial) update into the config and persist it."""
    global _config
    with _lock:
        current = _config or AppConfig()
        data = current.model_dump()
        _deep_update(data, patch)
        _config = AppConfig.model_validate(data)
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(
            json.dumps(_config.model_dump(), ensure_ascii=False, indent=2), "utf-8"
        )
        return _config
