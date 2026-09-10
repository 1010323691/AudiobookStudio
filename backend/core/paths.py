"""Unified directory layout for the AudiobookStudio backend.

One root — the user's chosen workspace (``paths.working_dir`` in the root
``app.json``) holds everything for the project::

    00_temp/           # scratch files for engine subprocesses
    01_input/          # raw uploads + formatted text
    02_split_text/     # book-splitting (volume) results
    03_parsed_json/    # LLM-parsed script JSON
    04_voice_profiles/ # character voice configs / preview WAVs
    05_audio_chunk/    # per-segment batch audio (+ manifest)
    06_audio_merge/    # merged audiobook file
    07_output/         # final episode files
    logs/              # the project's app.log
    config/            # the project's config/app.json

The project root itself holds only ``app.json`` — the generic default config
template plus the bootstrap pointer to the active workspace (see
``core/config.py``). Until a workspace is set there is *no* project location at
all: the pipeline is locked, the config falls back to the (read-only) root
template, and logging is console-only. Nothing here ever deletes anything.
"""
from __future__ import annotations

from pathlib import Path

# backend/core/paths.py  ->  parents[0]=core  parents[1]=backend  parents[2]=<project>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The eight directories (seven artifacts + scratch) that follow the user's
# workspace: (Layout attribute, on-disk directory name), in pipeline order.
WORKSPACE_DIRS = (
    ("temp", "00_temp"),
    ("input", "01_input"),
    ("split_text", "02_split_text"),
    ("parsed_json", "03_parsed_json"),
    ("voice_profiles", "04_voice_profiles"),
    ("audio_chunk", "05_audio_chunk"),
    ("audio_merge", "06_audio_merge"),
    ("output", "07_output"),
)

# On-disk directory names only (for creation / validation).
WORKSPACE_DIR_NAMES = tuple(name for _, name in WORKSPACE_DIRS)


class Layout:
    """Resolved, on-disk directory tree: a single workspace root holding everything.

    ``workspace is None`` (no workspace set) -> every path property is ``None``
    and :meth:`ensure` / :meth:`dirs` are inert, so nothing can be planted in the
    project directory before a workspace exists.
    """

    def __init__(self, workspace: Path | None):
        self.workspace = workspace
        if workspace is None:
            # Unset: no artifact / log / config locations exist.
            self.temp = None
            self.input = None
            self.split_text = None
            self.parsed_json = None
            self.voice_profiles = None
            self.audio_chunk = None
            self.audio_merge = None
            self.output = None
            self.logs = None
            self.config = None
            return
        # Everything lives under the workspace:
        self.temp = workspace / "00_temp"
        self.input = workspace / "01_input"
        self.split_text = workspace / "02_split_text"
        self.parsed_json = workspace / "03_parsed_json"
        self.voice_profiles = workspace / "04_voice_profiles"
        self.audio_chunk = workspace / "05_audio_chunk"
        self.audio_merge = workspace / "06_audio_merge"
        self.output = workspace / "07_output"
        self.logs = workspace / "logs"
        self.config = workspace / "config"

    # -- directory creation (mkdir only — nothing is ever deleted) ------------
    def ensure(self) -> "Layout":
        """Create the eight artifact dirs + logs + config under the workspace."""
        if self.workspace is None:
            return self
        for name in (*WORKSPACE_DIR_NAMES, "logs", "config"):
            (self.workspace / name).mkdir(parents=True, exist_ok=True)
        return self

    def dirs(self) -> dict[str, str]:
        """``{on-disk dir name: absolute path}`` for the artifact dirs (``{}`` when unset)."""
        if self.workspace is None:
            return {}
        return {name: str(getattr(self, attr)) for attr, name in WORKSPACE_DIRS}


def is_workspace_set() -> bool:
    """True once the user has picked a workspace folder (root ``app.json`` pointer)."""
    from .config import _workspace_path  # local import to avoid a cycle

    return _workspace_path() is not None


def get_layout() -> Layout:
    """Return the Layout for the configured workspace.

    The workspace root (artifact dirs + logs + config) is created idempotently only
    when a working directory is actually set — an unset workspace yields an inert
    ``Layout(None)`` and must not plant any folders in the project directory.
    Relative ``working_dir`` values resolve against ``PROJECT_ROOT``.
    """
    from .config import _workspace_path  # local import to avoid a cycle

    workspace = _workspace_path()
    if workspace is None:
        return Layout(None)
    layout = Layout(workspace)
    layout.ensure()
    return layout


def _checked_variant(base: Path) -> Path:
    """``<stem>_checked.json`` next to ``base`` if it exists, else ``base`` itself.

    A Speaker check writes ``<stem>_checked.json`` alongside the original ``<stem>.json``
    (leaving the original untouched); downstream stages read the checked copy when one
    exists.
    """
    checked = base.with_name(base.stem + "_checked.json")
    return checked if checked.exists() else base


def resolve_parsed_json(script: str | None = None) -> Path:
    """The parsed script JSON a downstream stage (角色配音 / 音频合成) should read.

    ``script`` names a file inside ``03_parsed_json/``. When given, that file is used —
    except that a base ``<stem>.json`` is transparently upgraded to ``<stem>_checked.json``
    if the Speaker check has produced one (a name that is already a ``_checked`` file is
    returned as-is, so it is never double-suffixed). When omitted, fall back to the most
    recently modified *base* ``*.json`` (then to its ``_checked`` copy if present), and
    finally to the legacy single-file name ``annotated_script.json`` (so an older project
    that still has it keeps working). With no workspace set, returns a non-existent
    relative placeholder so read-only callers degrade cleanly. Callers validate existence
    and raise a clear error when nothing resolves.
    """
    layout = get_layout()
    d = layout.parsed_json
    if d is None:  # no workspace: inert (read-only callers see "no script")
        return Path("annotated_script.json")
    if script:
        p = d / script
        if script.endswith(".json") and not script.endswith("_checked.json"):
            return _checked_variant(p)
        return p
    if d.exists():
        # Pick the most recent base file (never a _checked file, so it can't be
        # re-suffixed), then transparently upgrade to its _checked copy if present.
        base = [p for p in d.glob("*.json")
                if p.is_file() and not p.name.endswith("_checked.json")]
        if base:
            return _checked_variant(max(base, key=lambda p: p.stat().st_mtime))
        # Only _checked files present (no base) — degrade to the most recent one.
        checked = [p for p in d.glob("*_checked.json") if p.is_file()]
        if checked:
            return max(checked, key=lambda p: p.stat().st_mtime)
    return d / "annotated_script.json"
