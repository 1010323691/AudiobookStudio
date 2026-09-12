"""Unified, persistent configuration.

Two files, one pointer:

* ``<project>/app.json`` — the root file. It is both the *generic default
  template* (its non-pointer fields seed every new workspace's config) and the
  *bootstrap pointer*: ``paths.working_dir`` names the active workspace. The
  pointer is the ONLY field the app ever writes there — all other values are
  read-only at runtime.
* ``<workspace>/config/app.json`` — the active project's config, created by
  copying the root template when a workspace is set (never overwritten if it
  already exists). Once a workspace exists, every config read and write targets
  this file, and it is rewritten on each save (requirement #4).

The pointer must live in the root file: to load the workspace config the app
must first know *which* workspace is active, and that can only be read from a
stable location that does not itself depend on the workspace (otherwise it is a
chicken-and-egg loop — the config file chasing its own setting).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import PROJECT_ROOT


class PathsConfig(BaseModel):
    working_dir: str = ""  # the workspace pointer (empty -> no workspace, pipeline locked)


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
    # Local Qwen3-TTS engine — runs in the isolated ``.venv-tts`` (Python 3.10) as a
    # one-shot subprocess (see ``backend/engines/tts.py``); the 3.14 app backend never
    # imports torch.
    enabled: bool = True
    model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"  # CustomVoice model id
    # The other two Qwen3-TTS 1.7B variants, loaded by the worker for clone / design.
    base_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"  # voice cloning
    design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"  # text -> voice
    speaker: str = "serena"  # default voice / speaker
    language: str = "chinese"  # default language
    device: str = "auto"  # auto | cuda | cpu | mps
    # Merge pause defaults (ported from the source project's TTS config): silence
    # inserted between segments, per-speaker vs. speaker-change (ms).
    pause_between_speakers_ms: int = 500
    pause_same_speaker_ms: int = 250
    # 角色配音·阶段 2（克隆）的并行 TTS 子进程数（每个子进程各自加载一次模型，显存随之增加）。
    parallel_workers: int = 1
    # 音频合成（一键合成 / batch）的并发段数：单个 TTS 子进程内用线程池并行合成，
    # 同时最多 ``batch_concurrency`` 段在合成，模型只加载一次。1 = 逐段串行（原行为）。
    # 使用处钳制到 [1, 32]。与 parallel_workers（并行子进程）是两种不同的并行方式。
    batch_concurrency: int = 4
    # Legacy API-provider fields, unused by the local engine, kept so an existing
    # config/app.json still loads (and round-trips) cleanly.
    api_base: str = ""
    api_key: str = ""
    voice: str = ""
    concurrency: int = 1


class PersonaPromptsConfig(BaseModel):
    # Voice-design (persona) prompts for the "角色配音" stage. Empty values fall back
    # to the bundled defaults in ``backend/engines/persona_prompts.py``.
    system_prompt: str = ""
    user_prompt: str = ""
    advanced_prompt: str = ""


class FFmpegConfig(BaseModel):
    ffmpeg_path: str = ""  # empty -> resolve from PATH
    ffprobe_path: str = ""  # empty -> resolve from PATH


class LogConfig(BaseModel):
    level: str = "INFO"


class UIConfig(BaseModel):
    theme: str = "system"  # system | light | dark


class LLMConfig(BaseModel):
    # OpenAI-compatible ``chat/completions`` endpoint (default: a local Ollama server).
    # The LLM HTTP call uses stdlib ``urllib`` — see ``backend/engines/script.py``.
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "local"  # local servers ignore it; remote APIs need the real key
    model_name: str = ""  # left blank on purpose — the user sets their own model
    # Stream the completion (``stream: true``) so the 文本解析 page can show the model's
    # raw output token-by-token (the 「流式反馈」 panel). When a server rejects
    # ``stream: true``, set this false in ``config/app.json`` to fall back to the
    # non-streaming call (the stream panel then stays empty).
    stream: bool = True


class PromptsConfig(BaseModel):
    # Empty values fall back to the bundled defaults
    # (``backend/resources/default_prompts.txt``) at read time — see ``backend/api/config.py``.
    system_prompt: str = ""
    user_prompt: str = ""


class SpeakerCheckConfig(BaseModel):
    # Speaker 检查 (post-parse) settings — fully independent of the 解析 prompts above.
    # batch_size: how many TARGET entries are re-judged per LLM call (one batch); each batch
    # is flanked by ``±context_window`` context entries, so a full batch sends at most
    # ``batch_size + 2*context_window`` entries (clamped down at the file's start / end).
    # A batch of 50 with a window of 4 → 50 targets + 4 before + 4 after = ≤ 58 entries.
    batch_size: int = 20
    # context_window: how many surrounding entries (on EACH side of the target block) are
    # sent as context when checking a batch (they are marked non-target: used to reason,
    # never re-judged).
    context_window: int = 4
    # Dedicated 检查 prompts. Empty values fall back to the bundled defaults
    # (``backend/engines/check_prompts.py`` / ``resources/default_check_prompts.txt``).
    system_prompt: str = ""
    user_prompt: str = ""


class GenerationConfig(BaseModel):
    chunk_size: int = 3000  # chars per chunk sent to the LLM
    max_tokens: int = 4096  # max completion tokens per call
    temperature: float = 0.6
    top_p: float = 0.8
    top_k: int = 0  # 0 -> not sent (OpenAI ignores it; some local servers use it)
    min_p: float = 0.0  # 0 -> not sent
    presence_penalty: float = 0.0
    banned_tokens: list = Field(default_factory=list)
    # Max files parsed in parallel (LLM jobs); the rest of a batch queue behind a
    # shared gate (see ``core/concurrency.py``). 0 / negative is clamped to 1.
    max_concurrency: int = 3


class AppConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    book: BookConfig = Field(default_factory=BookConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    persona_prompts: PersonaPromptsConfig = Field(default_factory=PersonaPromptsConfig)
    speaker_check: SpeakerCheckConfig = Field(default_factory=SpeakerCheckConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


# The root file: generic default config template + the workspace pointer (the only writable field).
TEMPLATE_FILE = PROJECT_ROOT / "app.json"

_lock = threading.RLock()
_config: AppConfig | None = None


class WorkspaceNotSetError(RuntimeError):
    """A config write was attempted with no workspace selected (-> HTTP 409)."""


# -- bootstrap pointer (the only thing read from the root before the workspace) -- #

def _read_root_pointer() -> str:
    """Read ``paths.working_dir`` from the root file (empty when absent/invalid)."""
    if not TEMPLATE_FILE.exists():
        return ""
    try:
        data = json.loads(TEMPLATE_FILE.read_text("utf-8"))
    except Exception:
        return ""
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, dict):
        return ""
    return str(paths.get("working_dir") or "")


def _workspace_path() -> Path | None:
    """The active workspace as an absolute path, or ``None`` when unset.

    This is the single bootstrap-safe source of truth for "which workspace".
    Relative pointers resolve against ``PROJECT_ROOT``.
    """
    working = _read_root_pointer().strip()
    if not working:
        return None
    ws = Path(working)
    if not ws.is_absolute():
        ws = PROJECT_ROOT / ws
    return ws


def _active_config_file() -> Path:
    """The file that is the source of truth for the full config."""
    ws = _workspace_path()
    if ws is None:
        return TEMPLATE_FILE  # unset -> the (read-only) root template
    return ws / "config" / "app.json"


# -- loading ------------------------------------------------------------------ #

def _load_config_file(file: Path) -> AppConfig | None:
    if not file.exists():
        return None
    try:
        return AppConfig.model_validate(json.loads(file.read_text("utf-8")))
    except Exception:
        return None


def _load_unlocked() -> AppConfig:
    """Resolve the active config without acquiring the lock (callers hold it).

    Workspace set -> the workspace's ``config/app.json``; otherwise the root
    template (a read-only view). A missing workspace file falls back to the root
    template's values, then to pure code defaults — reads never write.
    """
    return (
        _load_config_file(_active_config_file())
        or _load_config_file(TEMPLATE_FILE)
        or AppConfig()
    )


def get_config() -> AppConfig:
    """Return the in-memory config, loading it on first access (see ``_load_unlocked``)."""
    global _config
    with _lock:
        if _config is None:
            _config = _load_unlocked()
        return _config


def reset_config_cache() -> None:
    """Drop the in-memory config; the next read re-resolves the source file.

    Called after the workspace pointer changes (set / clear) so reads switch from
    the old workspace's config to the new one (or the root template).
    """
    global _config
    with _lock:
        _config = None


# -- writing ------------------------------------------------------------------ #

def _write_config_file(file: Path, config: AppConfig) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2), "utf-8"
    )


def _ensure_template() -> None:
    """Seed the root ``app.json`` from code defaults on a fresh clone (missing file)."""
    if not TEMPLATE_FILE.exists():
        _write_config_file(TEMPLATE_FILE, AppConfig())


def set_workspace_pointer(path: str) -> None:
    """Persist the workspace pointer in the ROOT file (its only writable field).

    Every other template value is preserved verbatim; the config cache is reset so
    subsequent reads target the (new) workspace.
    """
    with _lock:
        _ensure_template()
        try:
            data = json.loads(TEMPLATE_FILE.read_text("utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.setdefault("paths", {})["working_dir"] = path
        _write_config_file(TEMPLATE_FILE, AppConfig.model_validate(data))
        reset_config_cache()


def clear_workspace() -> None:
    """Clear the pointer (re-locks the pipeline); config falls back to the template."""
    set_workspace_pointer("")


def init_workspace_config(ws: Path) -> None:
    """Give a fresh workspace its own ``config/app.json`` — a copy of the root
    template with ``working_dir`` set to the workspace. NEVER overwrites an
    existing workspace config."""
    with _lock:
        _ensure_template()
        target = ws / "config" / "app.json"
        if target.exists():
            return
        base = _load_config_file(TEMPLATE_FILE) or AppConfig()
        base.paths.working_dir = str(ws)
        _write_config_file(target, base)


def _deep_update(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def update_config(patch: dict[str, Any]) -> AppConfig:
    """Merge a (possibly partial) update into the ACTIVE (workspace) config and
    persist it there. Requires a workspace (raises ``WorkspaceNotSetError`` ->
    HTTP 409). ``paths.working_dir`` is forced to the workspace itself, so the
    workspace can only be changed via the workspace endpoint, never a settings
    write. The root template is never touched."""
    global _config
    with _lock:
        ws = _workspace_path()
        if ws is None:
            raise WorkspaceNotSetError(
                "尚未设置工作空间——配置随工程，请先在「开始」页选择文件夹。"
            )
        current = _config if _config is not None else _load_unlocked()
        data = current.model_dump()
        _deep_update(data, patch)
        new = AppConfig.model_validate(data)
        new.paths.working_dir = str(ws)
        _config = new
        _write_config_file(ws / "config" / "app.json", new)
        return new
