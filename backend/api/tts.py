"""TTS + character-voice + batch + merge endpoints (modules: TTS / 角色配音 / 音频合成 / 音频合并).

``GET /status`` reports readiness. The stage endpoints (``POST /prepare-foundations``,
``POST /make-clones``, ``GET /voices``, ``POST /batch``, ``POST /merge``) each start a
long-running
:class:`Task` that drives the isolated Qwen3-TTS engine (see
``backend/engines/tts.py`` / ``voices.py`` / ``tts_batch.py`` / ``merge.py``) and
return ``{"task_id"}``; the UI streams the task's progress/logs over SSE and plays
the resulting audio via the shared ``GET /api/files/download/05_audio_chunk/{name}``
route. Any failing task is marked failed and isolated — it never takes the console
down (requirement #7).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from ..core import pathio
from ..core.paths import ALL_PARSED_JSON, get_layout, resolve_parsed_json, resolve_parsed_json_all
from ..core.tasks import TERMINAL, get_task_manager
from ..engines import merge as Merge
from ..engines import tts as T
from ..engines import tts_batch as Batch
from ..engines import voices as V
from . import _common

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Let the UI show an accurate badge (ready vs. engine-not-installed)."""
    return {
        "implemented": T.IMPLEMENTED,
        "message": T.NOT_READY_MSG if not T.IMPLEMENTED else "TTS 合成可用。",
    }


# ---------------------------------------------------------------------------
# 角色配音（voice preparation）
# ---------------------------------------------------------------------------

class PrepareFoundationsRequest(BaseModel):
    # Phase 1 (LLM only): None -> every character; a list -> only those (single-char regen).
    speakers: list[str] | None = None
    # True -> regenerate only characters without a foundation yet.
    new_only: bool = False
    # speaker -> user-supplied voice description (skips the LLM for that character).
    overrides: dict[str, str] | None = None
    # Which parsed JSON (in 03_parsed_json/) to read; None -> most recent (resolve_parsed_json).
    script: str | None = None


class MakeClonesRequest(BaseModel):
    # Phase 2 (TTS only): None -> every foundation-bearing character; a list -> only those.
    speakers: list[str] | None = None
    # True -> limit to characters not yet holding a usable clone (also retries failed ones).
    new_only: bool = False
    # 批内行数（上限，1..64，不是并发进程数）：单个长驻 design-batch 子进程内的 GPU 张量批
    # 上限；None -> config.tts.batch_concurrency。
    concurrency: int | None = None
    # Which parsed JSON to read for the character set; None -> most recent.
    script: str | None = None
    # Per-character clone-candidate count: None = auto (the absolute log-scale ladder on
    # each character's OWN line count — see voices.auto_candidate_count); else 2/4/6/8.
    candidate_count: int | None = None

    @field_validator("candidate_count")
    @classmethod
    def _check_candidate_count(cls, v):
        if v is not None and v not in (2, 4, 6, 8):
            raise ValueError("candidate_count 须为 null（自动）或 2/4/6/8 之一")
        return v


def _scope_suffix(script: str | None) -> str:
    """A short task-label suffix naming the parsed-JSON scope ("" for the default, most-recent)."""
    return "（全部解析文件）" if script == ALL_PARSED_JSON else ""


@router.post("/prepare-foundations")
def prepare_foundations(req: PrepareFoundationsRequest) -> dict:
    """Start Phase 1: batch-generate every character's voice foundation (LLM only, no TTS)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新生成 {len(req.speakers)} 个角色语音推理基础{suffix}"
    elif req.new_only:
        label = f"生成新增角色语音推理基础{suffix}"
    else:
        label = f"生成所有角色语音推理基础{suffix}"
    task = get_task_manager().create(
        "voices-foundation", label,
        V.prepare_foundations,
        req.speakers, req.new_only, req.overrides or {}, req.script,
    )
    return {"task_id": task.id}


@router.post("/make-clones")
def make_clones(req: MakeClonesRequest) -> dict:
    """Start Phase 2: batch-render every character's clone seed WAV (TTS only, no LLM)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新制作 {len(req.speakers)} 个角色克隆音频{suffix}"
    elif req.new_only:
        label = f"制作新增角色克隆音频{suffix}"
    else:
        label = f"制作所有角色克隆音频{suffix}"
    label += " · 备选 自动" if req.candidate_count is None else f" · 备选 {req.candidate_count}"
    task = get_task_manager().create(
        "voices-clone", label,
        V.make_clones,
        req.speakers, req.new_only, req.concurrency, req.script, req.candidate_count,
    )
    return {"task_id": task.id}


def _voice_usable(entry: dict) -> bool:
    """Whether a stored voice entry actually yields a usable voice at synthesis time."""
    vtype = entry.get("type", "")
    if vtype == "clone":
        return bool(entry.get("ref_audio"))
    if vtype == "design":
        return bool((entry.get("description") or "").strip())
    if vtype == "custom":
        return True  # uses a named preset / default voice
    return False


def _foundation_status(entry: dict) -> str:
    """The character's voice-foundation state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``foundation_status`` written by Phase 1; otherwise infers it so
    entries created before the two-phase split (a stored description ⇒ a foundation exists)
    still report sensibly.
    """
    entry = entry or {}
    st = entry.get("foundation_status")
    if st in ("done", "failed"):
        return st
    if (entry.get("description") or "").strip():
        return "done"
    return "none"


def _clone_status(entry: dict) -> str:
    """The character's clone-audio state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``clone_status`` written by Phase 2; otherwise a stored
    ``type: clone`` + ``ref_audio`` (a usable clone seed) infers ``done``.
    """
    st = entry.get("clone_status")
    if st in ("done", "failed"):
        return st
    if entry.get("type") == "clone" and entry.get("ref_audio"):
        return "done"
    return "none"


def _fold_script(order: list[str], counts: dict[str, int], data) -> bool:
    """Fold one parsed script (a list of entries) into the shared ``order``/``counts``.

    Dedupes by speaker name (falling back to ``type``), sums line counts, and keeps
    first-appearance order — the same folding the single-file path did inline, now shared
    with the whole-book aggregate. Returns True when the file held a non-empty list (the
    ``has_script`` signal).
    """
    if not isinstance(data, list) or not data:
        return False
    for entry in data:
        sp = (entry.get("speaker") or entry.get("type") or "").strip()
        if not sp:
            continue
        if sp not in counts:
            counts[sp] = 0
            order.append(sp)
        counts[sp] += 1
    return True


@router.get("/voices")
def list_voices(script: str | None = None) -> dict:
    """Detected characters + their voice-config state (ready/pending) + preview paths.

    ``script`` names the parsed JSON (in ``03_parsed_json/``) to read; when omitted the
    most recently written one is used (see ``resolve_parsed_json``). With no workspace
    set it degrades to an empty result. ``preview`` is a path relative to
    ``04_voice_profiles/`` so the UI can play it through the shared
    ``download/04_voice_profiles/{name}`` route; empty when there is nothing to preview.
    ``speakers`` is sorted by ``line_count`` descending (stable — ties keep
    first-appearance order), so the leads top the page list.
    """
    layout = get_layout()
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"has_script": False, "script_path": "", "voice_config_path": "", "speakers": []}
    out_voices = layout.voice_profiles
    vc_path = out_voices / "voice_config.json"

    # Which script(s) to read: every parsed JSON (the whole-book "all files" request) or
    # the single named / most-recent one. ``script_path_out`` is only a display label.
    if script == ALL_PARSED_JSON:
        script_paths = resolve_parsed_json_all()
        script_path_out = ""
    else:
        script_paths = [resolve_parsed_json(script)]
        script_path_out = str(script_paths[0])

    has_script = False
    order: list[str] = []
    counts: dict[str, int] = {}
    for sp in script_paths:
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt file just contributes no speakers
            data = []
        if _fold_script(order, counts, data):
            has_script = True

    voice_config: dict = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception:  # noqa: BLE001
            voice_config = {}
        # Lazy migration of legacy absolute ref_audio values (the file is rewritten in the
        # workspace-relative form on first read after the upgrade).
        _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
        if migrated is not None:
            voice_config = migrated

    # Display order: line count descending (leads first, cameos last) — a stable sort, so
    # ties keep first-appearance (or voice_config) order.
    names = sorted(order if has_script else list(voice_config.keys()),
                   key=lambda sp: -counts.get(sp, 0))

    def _preview_of(ref: str) -> str:
        """A stored reference-audio path, relative to 04_voice_profiles/ (the UI plays it
        through the shared download route); '' when there is nothing to preview."""
        if not ref:
            return ""
        # Resolved against the current workspace root (relative form; a legacy absolute
        # value still works) so the preview keeps working after the workspace moves.
        try:
            p = pathio.resolve_path(ref, layout.workspace, strict=False)
        except pathio.PathOutsideWorkspace:
            p = None
        if p is None or not p.exists():
            return ""
        try:
            return str(p.relative_to(out_voices)).replace(os.sep, "/")
        except ValueError:
            return str(p)

    speakers: list[dict] = []
    for sp in names:
        entry = voice_config.get(sp, {})
        vtype = entry.get("type", "")
        alias_of = entry.get("alias_of", "")
        ready = bool(alias_of) or _voice_usable(entry)
        # Every clone candidate the character has (legacy entries synthesise one from
        # their clone reference), plus the user's pick (None = the default first one).
        cands = V.effective_candidates(entry)
        speakers.append({
            "name": sp,
            "line_count": counts.get(sp, 0),
            "status": "ready" if ready else "pending",
            "foundation_status": _foundation_status(entry),
            "clone_status": _clone_status(entry),
            "type": vtype,
            "alias_of": alias_of,
            "description": entry.get("description", ""),
            "preview": _preview_of(entry.get("ref_audio", "")),
            "candidates": [{"id": c["id"], "preview": _preview_of(c["ref_audio"]), "seed": c["seed"]}
                           for c in cands],
            "selected_audio_id": entry.get("selected_audio_id") or None,
        })

    return {
        "has_script": has_script,
        "script_path": script_path_out,
        "voice_config_path": str(vc_path),
        "speakers": speakers,
    }


class SelectVoiceRequest(BaseModel):
    speaker: str
    # Candidate id to make active; None/empty = clear the pick (the default first
    # candidate becomes active again).
    audio_id: str | None = None


def _phase_task_active() -> bool:
    """Whether a voices-foundation / voices-clone task is in flight. Both rewrite
    ``voice_config.json`` as a whole file, so a concurrent selection write could be
    clobbered (or vice versa) — refuse the write while one runs."""
    for t in get_task_manager().list():
        if t.module in ("voices-foundation", "voices-clone") and t.status not in TERMINAL:
            return True
    return False


@router.put("/voices/select")
def select_voice(req: SelectVoiceRequest) -> dict:
    """Record the user's clone-candidate pick for a character.

    Synchronous (no Task): the chosen candidate becomes the ACTIVE clone reference —
    ``selected_audio_id`` plus the top-level ``ref_audio`` are updated together in one
    file rewrite, so every downstream stage (音频合成 / worker) uses the picked take.
    """
    _common.require_workspace()
    if _phase_task_active():
        raise HTTPException(409, "配音任务进行中，请待其结束后再选择音色。")
    layout = get_layout()
    vc_path = layout.voice_profiles / "voice_config.json"
    if not vc_path.exists():
        raise HTTPException(404, "未找到声音配置，请先运行阶段 1 / 阶段 2。")
    try:
        voice_config = json.loads(vc_path.read_text("utf-8"))
        if not isinstance(voice_config, dict):
            raise ValueError
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "声音配置已损坏，无法更新选择。")
    # Lazy migration of legacy absolute ref_audio values (same as the other read paths).
    _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
    if isinstance(migrated, dict):
        voice_config = migrated
    entry = voice_config.get(req.speaker)
    if not isinstance(entry, dict):
        raise HTTPException(404, f"声音配置中没有角色：{req.speaker}")
    cands = V.effective_candidates(entry)
    if not cands:
        raise HTTPException(400, "该角色没有可选择的候选音频，请先在阶段 2 生成。")
    aid = (req.audio_id or "").strip() or None
    chosen = None
    if aid is not None:
        chosen = next((c for c in cands if c["id"] == aid), None)
        if chosen is None:
            raise HTTPException(400, f"无效的候选编号：{aid}")
    active = chosen or cands[0]
    entry["selected_audio_id"] = aid
    # Keep the active reference in sync so downstream synthesis uses the picked take.
    entry["ref_audio"] = active["ref_audio"]
    vc_path.write_bytes(json.dumps(voice_config, indent=2, ensure_ascii=False).encode("utf-8"))
    return {"ok": True, "speaker": req.speaker, "selected_audio_id": aid,
            "ref_audio": active["ref_audio"]}


# ---------------------------------------------------------------------------
# 音频合成 + 音频合并
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    # None -> every script line; a list of line indices -> only those (single file only).
    indices: list[int] | None = None
    # Which parsed JSON (in 03_parsed_json/) to synthesize; None -> most recent.
    script: str | None = None
    # Multi-file run (the 待合成 card's selection): parsed-JSON file names, synthesized one
    # by one in a single task (each file = its own package; a per-file failure is isolated).
    # Takes precedence over ``script``; an empty list falls back to ``script`` / most recent.
    scripts: list[str] | None = None
    # Concurrent segments (1..32); None -> the persisted default (config.tts.batch_concurrency).
    concurrency: int | None = None
    # Reproducible seed for the run: >=0 seeds each sub-batch (seed + sub-batch seq); None ->
    # the persisted default (config.tts.batch_seed); -1 -> random.
    seed: int | None = None


@router.post("/batch")
def run_batch(req: BatchRequest) -> dict:
    _common.require_workspace()
    if req.script == ALL_PARSED_JSON or any(s == ALL_PARSED_JSON for s in (req.scripts or [])):
        raise HTTPException(status_code=400, detail="音频合成仅支持逐个解析 JSON（“全部”只用于「角色配音」）。")
    scripts = req.scripts or ([req.script] if req.script else [])
    if len(scripts) > 1 and req.indices:
        raise HTTPException(status_code=400, detail="按段选择（indices）仅支持单个文件。")
    if req.indices:
        label = f"音频合成（{len(req.indices)} 段）"
    else:
        label = "音频合成（续合）"
    if len(scripts) == 1:
        label += f" · {scripts[0]}"
    elif len(scripts) > 1:
        label += f" · {len(scripts)} 个文件"
    if req.concurrency:
        label += f" · 批内 {req.concurrency}"
    if len(scripts) > 1:
        task = get_task_manager().create(
            "tts-batch", label,
            Batch.synthesize_multi, scripts, req.concurrency, req.seed,
        )
    else:  # 0 or 1 file: the legacy single-file path, byte-identical behaviour
        task = get_task_manager().create(
            "tts-batch", label,
            Batch.synthesize, req.indices, scripts[0] if scripts else req.script,
            req.concurrency, req.seed,
        )
    return {"task_id": task.id}


class ResetBatchRequest(BaseModel):
    # Parsed-JSON file names (03_parsed_json/). Each one's synthesis package — the folder
    # ``05_audio_chunk/<包名>/`` with its mp3s and manifest.json — is deleted, so the
    # following ordinary run (default resume) re-synthesizes every segment.
    scripts: list[str]


def _batch_task_active() -> bool:
    """Whether a tts-batch (音频合成) task is in flight. Its engine writes the package folders
    while running, so deleting a package mid-run would tear out the files / manifest it is
    producing — refuse the reset while one runs (the same guard shape as voice selection)."""
    for t in get_task_manager().list():
        if t.module == "tts-batch" and t.status not in TERMINAL:
            return True
    return False


@router.post("/batch-reset")
def reset_batch(req: ResetBatchRequest) -> dict:
    """「重新全部合成」第一步（同步、非任务）：删除选中解析 JSON 的合成包
    （``05_audio_chunk/<包名>/``：逐行 mp3 + manifest.json），使随后的一键合成请求
    （与默认续合同一条线路、同一请求形状）从头重做全部段落。

    Deleting generated, regenerable output is the app's only deliberate delete path —
    user-initiated here, and confined by construction to ``05_audio_chunk/<包名>/``: the
    package name is the file name's stem minus ``_checked`` (never a separator), so it can
    not escape the directory. Guards: no workspace 409 (write guard) -> ``__all__`` /
    empty list 400 -> in-flight tts-batch task 409 (its engine is writing those folders).
    """
    _common.require_workspace()
    if any(s == ALL_PARSED_JSON for s in req.scripts):
        raise HTTPException(status_code=400, detail="“全部文件”只用于「角色配音」——请逐个列出解析 JSON。")
    if not req.scripts:
        raise HTTPException(status_code=400, detail="没有要重置的文件。")
    if _batch_task_active():
        raise HTTPException(409, "合成任务进行中，请待其结束后再重置。")
    layout = get_layout()
    removed = []
    for name in req.scripts:
        pkg_name = Batch.package_for(Path(name))
        pkg = layout.audio_chunk / pkg_name
        if pkg.exists():
            shutil.rmtree(pkg)
            removed.append(pkg_name)
    return {"ok": True, "removed": removed}


def _read_voice_config(layout) -> dict:
    """The workspace ``voice_config.json`` as a dict (``{}`` when absent / corrupt) — a read-only
    snapshot (no lazy migration: this endpoint is polled, so it must not write)."""
    vc = layout.voice_profiles / "voice_config.json"
    if not vc.exists():
        return {}
    try:
        loaded = json.loads(vc.read_text("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _file_batch_status(name: str, layout, voice_config: dict) -> dict:
    """One row of the multi-file 待合成 list (``GET /batch-status?scripts=…``).

    Per file: segment completion (same rule as the single-file endpoint — ``completed`` = a
    manifest entry ``ok`` whose file is still on disk) plus character readiness (the same
    ready rule as ``/voices``: an alias or a usable voice entry). ``complete`` marks a file
    whose every synthesizable segment is done (the 已合成 badge; a file with no synthesizable
    segments never gets it). Degrades to a zero row when the file is missing / corrupt /
    empty, or no workspace is set.
    """
    out = {"name": name, "total": 0, "completed": 0, "remaining": 0,
           "complete": False, "speakers": 0, "ready": 0, "missing": []}
    src = resolve_parsed_json(name)
    if not src.exists():
        return out
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports zeros
        return out
    if not isinstance(data, list) or not data:
        return out
    segs = Batch._build_segments(data)
    c = Batch.count_completion(segs, Batch.load_manifest(layout.audio_chunk / Batch.package_for(src)))
    out["total"], out["completed"], out["remaining"] = c["total"], c["completed"], c["remaining"]
    out["complete"] = c["total"] > 0 and c["completed"] == c["total"]
    order: list[str] = []
    _fold_script(order, {}, data)  # distinct speakers, first-appearance order (incl. NARRATOR)
    ready = [sp for sp in order
             if (voice_config.get(sp) or {}).get("alias_of") or _voice_usable(voice_config.get(sp) or {})]
    out["speakers"] = len(order)
    out["ready"] = len(ready)
    out["missing"] = [sp for sp in order if sp not in set(ready)]
    return out


# ``scripts`` MUST be declared as a QUERY param: in this FastAPI version a bare
# ``list[...]`` default is treated as a JSON request body, and the repeated ``?scripts=``
# params are silently ignored (the 待合成 rows would all stay zero). ``Annotated`` keeps
# the plain ``None`` default, so direct (test) calls still work without going through FastAPI.
@router.get("/batch-status")
def batch_status(script: str | None = None,
                 scripts: Annotated[list[str] | None, Query()] = None) -> dict:
    """Synthesis progress of the chosen script(s).

    Single file (``?script=``; neither param -> the most recent one):
    ``{total, completed, remaining}``. ``completed`` counts segments already synthesized (a
    manifest entry ``ok`` whose file is still on disk) — the number behind the 待合成 list's
    per-row 【已合成 / 总段落】, refreshed live while a run streams (the manifest is written
    incrementally).

    Multi-file (repeated ``?scripts=`` params — the 待合成 list): ``{"files": [one row per
    name, in request order]}``; each row additionally carries ``complete`` (every segment
    done — the 已合成 badge), and ``speakers`` / ``ready`` / ``missing`` (character readiness,
    the same rule as ``/voices``). ``"__all__"`` is rejected (400), as in ``POST /batch``.
    Degrades to zeros with no workspace / no files, like ``/voices``.
    """
    layout = get_layout()
    if scripts:
        if any(s == ALL_PARSED_JSON for s in scripts):
            raise HTTPException(status_code=400, detail="“全部文件”只用于「角色配音」——请逐个列出解析 JSON。")
        vc = _read_voice_config(layout) if layout.parsed_json is not None else {}
        return {"files": [_file_batch_status(n, layout, vc) for n in scripts]}
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"total": 0, "completed": 0, "remaining": 0}
    src = resolve_parsed_json(script)
    if not src.exists():
        return {"total": 0, "completed": 0, "remaining": 0}
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports nothing
        return {"total": 0, "completed": 0, "remaining": 0}
    if not isinstance(data, list) or not data:
        return {"total": 0, "completed": 0, "remaining": 0}
    out_dir = layout.audio_chunk / Batch.package_for(src)
    return Batch.count_completion(Batch._build_segments(data), Batch.load_manifest(out_dir))


class MergeRequest(BaseModel):
    m4b: bool = False  # M4B output is a later phase; MP3 is produced for now.
    # Which package (a sub-folder in 05_audio_chunk/, one per source JSON) to merge;
    # None -> the most recent package (see merge._find_manifest).
    package: str | None = None


@router.post("/merge")
def run_merge(req: MergeRequest) -> dict:
    _common.require_workspace()
    label = ("合并 M4B" if req.m4b else "合并音频（Merge）") + (f"：{req.package}" if req.package else "")
    task = get_task_manager().create("merge", label, Merge.run, req.m4b, req.package)
    return {"task_id": task.id}
