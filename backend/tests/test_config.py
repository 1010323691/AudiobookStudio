"""Offline tests for the config model changes backing the voice / batch / merge
stages (``backend/core/config.py``) — the new TTS fields, the persona-prompt block,
and the pure deep-merge that ``update_config`` relies on.

``update_config`` itself writes the real ``config/app.json`` and is therefore exercised
in the manual run, not here; instead this pins the schema defaults and the pure
``_deep_update`` merge in isolation.
"""
from __future__ import annotations

import json

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.config import AppConfig, TTSConfig, _deep_update


# --------------------------------------------------------------------------- #
# TTSConfig defaults
# --------------------------------------------------------------------------- #

def test_tts_config_model_ids():
    t = TTSConfig()
    assert t.model == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert t.base_model == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert t.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"


def test_tts_config_pause_defaults():
    t = TTSConfig()
    assert t.pause_between_speakers_ms == 500
    assert t.pause_same_speaker_ms == 250


def test_tts_config_concurrency_placeholder():
    assert TTSConfig().parallel_workers == 1


def test_tts_config_keeps_legacy_fields():
    # An existing config/app.json still round-trips: legacy API fields survive.
    t = TTSConfig()
    assert t.api_base == ""
    assert t.api_key == ""
    assert t.voice == ""
    assert t.concurrency == 1


# --------------------------------------------------------------------------- #
# AppConfig: persona prompts + round-trip
# --------------------------------------------------------------------------- #

def test_app_config_includes_persona_prompts():
    cfg = AppConfig()
    assert cfg.persona_prompts.system_prompt == ""
    assert cfg.persona_prompts.user_prompt == ""
    assert cfg.persona_prompts.advanced_prompt == ""


def test_app_config_round_trips():
    data = AppConfig().model_dump()
    back = AppConfig.model_validate(data)
    assert back.tts.pause_between_speakers_ms == 500
    assert back.tts.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert back.persona_prompts is not None


# --------------------------------------------------------------------------- #
# _deep_update (the merge update_config uses)
# --------------------------------------------------------------------------- #

def test_deep_update_replaces_scalars():
    base = {"a": 1, "b": 2}
    _deep_update(base, {"b": 20, "c": 3})
    assert base == {"a": 1, "b": 20, "c": 3}


def test_deep_update_merges_nested_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    _deep_update(base, {"a": {"y": 20}, "c": 4})
    assert base == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}


def test_deep_update_replaces_dict_with_scalar():
    # A dict value replaced by a non-dict is overwritten, not merged into.
    base = {"a": {"x": 1}}
    _deep_update(base, {"a": 5})
    assert base == {"a": 5}


# --------------------------------------------------------------------------- #
# Bootstrap: root pointer + template + per-workspace config
# --------------------------------------------------------------------------- #

@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point the config module at a throwaway project root with a clean,
    pointer-less root ``app.json``; reset the in-memory cache around each test."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


def _read(file):
    return json.loads(file.read_text("utf-8"))


def test_get_config_unset_returns_root_template(sandbox):
    # With no pointer, the config is the (read-only) root template.
    cfg = core_config.get_config()
    assert cfg.paths.working_dir == ""


def test_set_workspace_pointer_updates_only_pointer(sandbox):
    # A distinct template value survives; only the pointer field changes.
    data = _read(sandbox / "app.json")
    data["book"] = {"target_chars": 123456}
    (sandbox / "app.json").write_text(json.dumps(data), encoding="utf-8")
    core_config.reset_config_cache()

    core_config.set_workspace_pointer(str(sandbox / "MyBook"))

    after = _read(sandbox / "app.json")
    assert after["paths"]["working_dir"] == str(sandbox / "MyBook")
    assert after["book"]["target_chars"] == 123456  # template value preserved


def test_get_config_set_reads_workspace_config(sandbox):
    ws = sandbox / "MyBook"
    core_config.set_workspace_pointer(str(ws))
    # Give the workspace its own (distinct) config:
    ws_cfg = AppConfig()
    ws_cfg.paths.working_dir = str(ws)
    ws_cfg.book.target_chars = 999999
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "app.json").write_text(
        json.dumps(ws_cfg.model_dump()), encoding="utf-8"
    )
    core_config.reset_config_cache()

    cfg = core_config.get_config()
    assert cfg.paths.working_dir == str(ws)
    assert cfg.book.target_chars == 999999  # from the workspace config, not the template


def test_update_config_requires_workspace(sandbox):
    with pytest.raises(core_config.WorkspaceNotSetError):
        core_config.update_config({"book": {"target_chars": 1}})


def test_update_config_writes_workspace_and_forces_pointer(sandbox):
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)  # seeds ws/config/app.json
    core_config.set_workspace_pointer(str(ws))

    cfg = core_config.update_config({"book": {"target_chars": 777}})
    assert cfg.book.target_chars == 777
    assert cfg.paths.working_dir == str(ws)  # forced to the workspace itself

    # The value landed in the workspace config; the ROOT template is untouched:
    assert _read(ws / "config" / "app.json")["book"]["target_chars"] == 777
    assert _read(sandbox / "app.json")["paths"]["working_dir"] == str(ws)


def test_init_workspace_config_copies_template_and_sets_pointer(sandbox):
    ws = sandbox / "NewBook"
    core_config.init_workspace_config(ws)
    target = ws / "config" / "app.json"
    assert target.exists()
    assert _read(target)["paths"]["working_dir"] == str(ws)


def test_init_workspace_config_never_overwrites(sandbox):
    ws = sandbox / "NewBook"
    (ws / "config").mkdir(parents=True)
    marker = {"paths": {"working_dir": str(ws)}, "book": {"target_chars": 424242}}
    (ws / "config" / "app.json").write_text(json.dumps(marker), encoding="utf-8")

    core_config.init_workspace_config(ws)  # must NOT overwrite an existing config

    assert _read(ws / "config" / "app.json")["book"]["target_chars"] == 424242


def test_template_seeded_from_defaults_when_missing(sandbox):
    # Remove the root file; the next pointer write re-seeds it from code defaults.
    (sandbox / "app.json").unlink()
    core_config.reset_config_cache()
    core_config.set_workspace_pointer(str(sandbox / "X"))
    assert (sandbox / "app.json").exists()
    data = _read(sandbox / "app.json")
    assert data["paths"]["working_dir"] == str(sandbox / "X")
    assert "tts" in data and "book" in data  # the full model was seeded
