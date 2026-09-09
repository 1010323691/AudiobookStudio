"""Tests for the single-root Layout (everything follows the user's workspace).

Every test points the layout/config at throwaway ``tmp_path`` directories (via
monkeypatched module globals + a pointer-less root ``app.json``), so the real
project's files are never touched.
"""
from __future__ import annotations

import json

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect the project-root globals into ``tmp_path`` and start from a clean
    (pointer-less) root ``app.json``. The in-memory config cache is reset around
    each test so it re-resolves against the sandbox."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


@pytest.fixture
def set_pointer(sandbox):
    """Write the workspace pointer into the sandboxed root ``app.json``."""

    def _set(working_dir: str):
        core_config.set_workspace_pointer(working_dir)

    return _set


# -- Layout construction ------------------------------------------------------

def test_layout_maps_single_root(tmp_path):
    ws = tmp_path / "ws"
    layout = core_paths.Layout(ws)
    # The seven artifact dirs + scratch all follow the workspace:
    assert layout.input == ws / "01_input"
    assert layout.split_text == ws / "02_split_text"
    assert layout.parsed_json == ws / "03_parsed_json"
    assert layout.voice_profiles == ws / "04_voice_profiles"
    assert layout.audio_chunk == ws / "05_audio_chunk"
    assert layout.audio_merge == ws / "06_audio_merge"
    assert layout.output == ws / "07_output"
    assert layout.temp == ws / "00_temp"
    # Config + log now live inside the workspace too:
    assert layout.logs == ws / "logs"
    assert layout.config == ws / "config"


def test_layout_unset_is_inert(tmp_path):
    layout = core_paths.Layout(None)
    assert layout.workspace is None
    assert layout.input is None
    assert layout.output is None
    assert layout.logs is None
    assert layout.config is None
    assert layout.dirs() == {}
    # ensure() must not raise or create anything while unset:
    layout.ensure()


def test_dirs_helper_lists_artifact_dirs(tmp_path):
    ws = tmp_path / "ws"
    d = core_paths.Layout(ws).dirs()
    assert set(d) == set(core_paths.WORKSPACE_DIR_NAMES)
    assert d["01_input"] == str(ws / "01_input")
    assert d["06_audio_merge"] == str(ws / "06_audio_merge")


def test_ensure_creates_everything_and_is_idempotent(tmp_path):
    ws = tmp_path / "ws"
    core_paths.Layout(ws).ensure()
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    assert (ws / "logs").is_dir()
    assert (ws / "config").is_dir()
    core_paths.Layout(ws).ensure()  # second pass must not raise


# -- get_layout resolution ----------------------------------------------------

def test_get_layout_unset_is_inert(sandbox, set_pointer):
    set_pointer("")
    layout = core_paths.get_layout()
    assert layout.workspace is None
    # Nothing is planted in the project directory while unset:
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert not (sandbox / name).exists()
    assert not (sandbox / "logs").exists()
    assert not (sandbox / "config").exists()


def test_get_layout_set_creates_workspace_dirs(sandbox, set_pointer):
    ws = sandbox / "MyBook"
    set_pointer(str(ws))
    layout = core_paths.get_layout()
    assert layout.workspace == ws
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    assert (ws / "logs").is_dir()
    assert (ws / "config").is_dir()
    # Second call is a no-op (exist_ok), not an error:
    core_paths.get_layout()


def test_get_layout_relative_working_dir_resolves_against_project(sandbox, set_pointer):
    set_pointer("rel/ws")
    layout = core_paths.get_layout()
    assert layout.workspace == sandbox / "rel" / "ws"
    assert (sandbox / "rel" / "ws" / "01_input").is_dir()


def test_get_layout_is_idempotent_across_calls(sandbox, set_pointer):
    ws = sandbox / "MyBook"
    set_pointer(str(ws))
    first = core_paths.get_layout()
    second = core_paths.get_layout()
    assert first.workspace == second.workspace
    assert first.input == second.input


# -- predicates / invariants ---------------------------------------------------

def test_is_workspace_set_false_when_empty(sandbox, set_pointer):
    set_pointer("")
    assert not core_paths.is_workspace_set()


def test_is_workspace_set_true_when_set(sandbox, set_pointer):
    set_pointer(str(sandbox / "ws"))
    assert core_paths.is_workspace_set()
