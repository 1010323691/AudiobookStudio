"""Tests for the single-root Layout (everything follows the user's workspace).

Every test points the layout/config at throwaway ``tmp_path`` directories (via
monkeypatched module globals + a pointer-less root ``app.json``), so the real
project's files are never touched.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

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


# -- resolve_parsed_json (which parsed JSON the downstream stages read) ---------

def test_resolve_parsed_json_unset_returns_placeholder(sandbox, set_pointer):
    set_pointer("")
    # No workspace -> an inert relative placeholder (read-only callers see "no script").
    assert core_paths.resolve_parsed_json() == Path("annotated_script.json")
    assert core_paths.resolve_parsed_json("ignored.json") == Path("annotated_script.json")


def test_resolve_parsed_json_named_file(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    assert core_paths.resolve_parsed_json("第一册.json") == ws / "03_parsed_json" / "第一册.json"


def test_resolve_parsed_json_most_recent_when_unnamed(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    older = d / "older.json"
    newer = d / "newer.json"
    older.write_text("[]", encoding="utf-8")
    newer.write_text("[]", encoding="utf-8")
    old_t = time.time() - 1000
    new_t = time.time()
    os.utime(older, (old_t, old_t))
    os.utime(newer, (new_t, new_t))
    # With no explicit name, the most recently written JSON wins.
    assert core_paths.resolve_parsed_json() == newer


def test_resolve_parsed_json_legacy_fallback_when_empty(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    # No JSON files yet (the dir may exist, empty) -> the legacy single-file name.
    assert core_paths.resolve_parsed_json() == ws / "03_parsed_json" / "annotated_script.json"


# -- resolve_parsed_json: Speaker-check ``_checked.json`` preference -------------

def test_resolve_parsed_json_named_prefers_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册.json").write_text("[]", encoding="utf-8")
    (d / "第一册_checked.json").write_text("[]", encoding="utf-8")
    assert core_paths.resolve_parsed_json("第一册.json") == d / "第一册_checked.json"


def test_resolve_parsed_json_named_falls_back_when_no_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册.json").write_text("[]", encoding="utf-8")
    assert core_paths.resolve_parsed_json("第一册.json") == d / "第一册.json"


def test_resolve_parsed_json_named_checked_not_double_suffixed(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册_checked.json").write_text("[]", encoding="utf-8")
    # Asking for the _checked file directly returns it as-is (no ``_checked_checked``).
    assert core_paths.resolve_parsed_json("第一册_checked.json") == d / "第一册_checked.json"


def test_resolve_parsed_json_autopick_most_recent_base_without_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    (d / "a_checked.json").write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    # b is the most recent *base* file and has no checked copy -> resolves to b.
    os.utime(a, (0, 0))
    os.utime(b, (10, 10))
    assert core_paths.resolve_parsed_json() == b


def test_resolve_parsed_json_autopick_uses_checked_when_present(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    ac = d / "a_checked.json"; ac.write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    # ``a`` is the most recent base (``a_checked`` is excluded from the base pool even
    # though it is newer) and has a checked copy -> resolves to ``a_checked``.
    os.utime(a, (10, 10))
    os.utime(ac, (20, 20))
    os.utime(b, (5, 5))
    assert core_paths.resolve_parsed_json() == ac


def test_resolve_parsed_json_autopick_only_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    c1 = d / "x_checked.json"; c1.write_text("[]", encoding="utf-8")
    c2 = d / "y_checked.json"; c2.write_text("[]", encoding="utf-8")
    os.utime(c1, (5, 5)); os.utime(c2, (10, 10))
    # No base files at all -> degrade to the most recent _checked file.
    assert core_paths.resolve_parsed_json() == c2


# -- resolve_parsed_json_all (whole-book "all files" aggregate) -----------------

def test_resolve_parsed_json_all_unset_is_empty(sandbox, set_pointer):
    set_pointer("")
    assert core_paths.resolve_parsed_json_all() == []


def test_resolve_parsed_json_all_empty_dir_is_empty(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    assert core_paths.resolve_parsed_json_all() == []


def test_resolve_parsed_json_all_order_is_mtime_then_name(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    # Names sort a < b < c < d, but the mtimes are the reverse — so reading order
    # (mtime) yields d, c, b, a, proving mtime (not name) drives the order.
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    c = d / "c.json"; c.write_text("[]", encoding="utf-8")
    (d / "c_checked.json").write_text("[]", encoding="utf-8")
    dd = d / "d.json"; dd.write_text("[]", encoding="utf-8")
    os.utime(a, (30, 30)); os.utime(b, (20, 20))
    os.utime(c, (10, 10)); os.utime(dd, (5, 5))
    # c has a _checked copy -> upgraded; the rest return their base file.
    assert core_paths.resolve_parsed_json_all() == [dd, d / "c_checked.json", b, a]


def test_resolve_parsed_json_all_orphan_checked_degrades_to_them(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    x = d / "x_checked.json"; x.write_text("[]", encoding="utf-8")
    y = d / "y_checked.json"; y.write_text("[]", encoding="utf-8")
    os.utime(x, (5, 5)); os.utime(y, (10, 10))
    # No base files -> return the standalone _checked files (mtime order).
    assert core_paths.resolve_parsed_json_all() == [x, y]


def test_resolve_parsed_json_all_ignores_non_json_and_dirs(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    good = d / "good.json"; good.write_text("[]", encoding="utf-8")
    (d / "notes.txt").write_text("not json", encoding="utf-8")
    (d / "weird.json").mkdir()  # a *directory* named *.json -> excluded by is_file()
    assert core_paths.resolve_parsed_json_all() == [good]
