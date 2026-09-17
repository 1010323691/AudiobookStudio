"""Batch TTS engine — synthesize every script line (subprocess orchestrator).

One long-running Task drives the worker's ``batch`` mode: a single ``.venv-tts``
subprocess loads the needed model(s) once and synthesizes all segments in JSON
order, so a full book runs in one process (models loaded once) while the 3.14
backend still never imports torch. The child's stdout is pumped line-by-line into
the task's progress/log — the real-time "第 i/N 段 · 角色：X · 正在生成" stream — and
each per-segment ``[segment]`` line is recorded, so a single failure never freezes
the run. A manifest of what was produced is written for the Merge stage. The task
log is SSE-only (it vanishes with the session), so the child's full transcript is
also mirrored to ``<workspace>/logs/tts_batch_<timestamp>.log`` — the persistent
forensic trail for a run that dies mid-batch.

``synthesize`` is a Task worker (first arg is a :class:`TaskHandle`), mirroring
``engines/tts.py``: it streams progress/log, honours cooperative cancel (killing
the child), and marks the task FAILED only on a *fatal* error (no segments, engine
down, or every segment failed) — a per-segment failure is a recorded, non-fatal line.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..core import pathio
from ..core.config import get_config
from ..core.paths import get_layout, resolve_parsed_json
from ..core.tasks import TaskCancelled
from .tts import DEFAULT_LANGUAGE, DEFAULT_MODEL, WorkerWatchdogTimeout, resolve_engine, run_worker

IMPLEMENTED = True

# 一键合成「批内段数」上限的上下界（前端输入与后端钳制共用）。这只是上限：worker 运行时按
# 段长分档 + 实测显存动态调节实际每批条数（32/64 永远不会是固定并发数）。
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 64

# 增量 manifest 的落盘节流：内存态逐条更新，整份 JSON 重写最多每 2 秒一次（取消 / 引擎失败 /
# 看门狗重启 / 收尾仍强制落盘）。每行都整份重写 1MB 会在磁盘 / 杀软扫描负载下拖住行处理主
# 循环（任务日志与进度条逐行蠕动，而 worker 实际在全速张量批）。
MANIFEST_FLUSH_INTERVAL = 2.0


def clamp_concurrency(n) -> int:
    """Clamp a requested concurrency to ``[MIN_CONCURRENCY, MAX_CONCURRENCY]``.

    ``None`` / non-integer / out-of-range values collapse to a safe in-range int, so a
    stray config value or request can never spawn a degenerate cap (0) or an unbounded
    one (e.g. 999).
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = MIN_CONCURRENCY
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, v))


# 子批规划检查的规范映射表：(worker ``--disabled-checks`` 里的名字, config.tts 开关字段)。
# 规范序固定 → 拼出的 flag 值确定（与用户开关顺序无关，测试据此钉死）。五个静态检查均可由
# 设置页单独关闭（默认全开 = 现有行为不变）；「批内行数」上限不在其中（合成页手动，恒生效），
# 实测显存的动态调节（VramGovernor）也不可关。
PLANNER_CHECKS = (
    ("length_bands", "planner_length_bands"),
    ("batch_chars", "planner_batch_chars"),
    ("seq_chars", "planner_seq_chars"),
    ("length_ratio", "planner_length_ratio"),
    ("vram", "planner_vram"),
)


def disabled_planner_checks(tts_cfg) -> str:
    """The config-closed planner checks as the worker's ``--disabled-checks`` value (pure).

    Comma list in canonical ``PLANNER_CHECKS`` order; ``""`` when every check is on (the
    flag is then omitted from the command entirely — default behaviour, byte-identical cmd).
    ``getattr(..., True)`` tolerates a partial config object missing the fields (they default
    to on, exactly like a fresh ``TtsConfig``).
    """
    return ",".join(name for name, field in PLANNER_CHECKS
                    if not getattr(tts_cfg, field, True))


def _build_cmd(python, worker, seg_file, vc_path, out_dir, *, language, device,
               model, base_model, design_model, ffmpeg_path, concurrency, seed,
               workspace=None, disabled_checks: str = "") -> list:
    """The one-shot ``.venv-tts`` batch command (pure; factored out for testing).

    ``--concurrency`` is always present (a clamped int) — the worker's *per-batch ceiling*
    (the length bands + measured VRAM governor set the actual size); ``--seed`` (-1 = random)
    makes a run reproducible. Empty model ids are omitted so the worker falls back to its own
    (identical) defaults. ``--workspace`` hands the worker the workspace root so the
    workspace-relative ``ref_audio`` values in ``voice_config.json`` resolve correctly there
    (the worker's own cwd is the project root, not the workspace). ``disabled_checks``
    (``""`` = all checks on) becomes ``--disabled-checks`` only when non-empty, so a default
    config produces the exact command the worker used to receive.
    """
    cmd = [
        str(python), str(worker),
        "--mode", "batch",
        "--segments-file", str(seg_file),
        "--voice-config", str(vc_path),
        "--out-dir", str(out_dir),
        "--language", language or DEFAULT_LANGUAGE,
        "--device", device or "auto",
        "--concurrency", str(concurrency),
        "--seed", str(seed),
    ]
    if workspace:
        cmd += ["--workspace", str(workspace)]
    if model:
        cmd += ["--model", model]
    if base_model:
        cmd += ["--base-model", base_model]
    if design_model:
        cmd += ["--design-model", design_model]
    if ffmpeg_path:
        cmd += ["--ffmpeg", ffmpeg_path]
    if disabled_checks:
        cmd += ["--disabled-checks", disabled_checks]
    return cmd


def _load_script(p):
    if not p.exists():
        raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{p.name} 无法解析：{e}")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{p.name} 为空——请先生成脚本。")
    return data


def _handle_segment(line: str, by_index: dict, total: int, seg_results: dict, handle) -> None:
    """Parse a ``[segment] <index> ok|error <detail>`` line into a result + log line."""
    parts = line[len("[segment]"):].split(None, 2)
    if len(parts) < 2:
        handle.log(line, "WARNING")
        return
    try:
        index = int(parts[0])
    except ValueError:
        handle.log(line, "WARNING")
        return
    status = parts[1]
    detail = parts[2] if len(parts) > 2 else ""
    speaker = (by_index.get(index) or {}).get("speaker") or "(未知)"
    num = f"[{index + 1}/{total}]"
    if status == "ok":
        seg_results[index] = {"ok": True, "path": detail, "reason": ""}
        handle.log(f"{num} {speaker}：完成")
    else:
        seg_results[index] = {"ok": False, "path": "", "reason": detail}
        handle.log(f"{num} {speaker}：失败（{detail}）", "ERROR")


def _parse_watchdog_indices(line: str):
    """The in-flight segment indices named in a ``[watchdog] … indices=[…]`` line (a no-op list
    if the marker / list is absent or unparseable). Used to target a strike at workers==1."""
    i = line.find("indices=[")
    if i < 0:
        return []
    j = line.find("]", i)
    if j < 0:
        return []
    out = []
    for tok in line[i + len("indices=["):j].split(","):
        tok = tok.strip()
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def _build_segments(script, indices=None):
    """Build the ordered per-line synthesis segments (pure).

    ``index`` is the line's position in the *full* script, so per-segment filenames
    and the merged order always match the JSON. Lines with empty text are skipped;
    ``indices`` (line positions) optionally restricts which lines are included.
    """
    want = set(int(i) for i in indices) if indices else None
    segments = []
    for i, entry in enumerate(script):
        if want is not None and i not in want:
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "index": i,
            "speaker": (entry.get("speaker") or entry.get("type") or "").strip(),
            "text": text,
            "instruct": (entry.get("instruct") or "").strip(),
            "pause_after": entry.get("pause_after"),
        })
    return segments


@dataclass
class _PooledFile:
    """One chapter file's bookkeeping inside a pooled multi-file run.

    ``pending`` / ``by_index`` / ``seg_results`` are keyed by the *chapter-local* index
    (the line's position in this file's JSON); the pool-global index only exists on the
    pool rows and is mapped back here through the run's ``pool_map``. ``error`` is set
    only for prep-stage fatals (missing / corrupt / empty script, package collision) —
    engine-level failures are expressed by the top-level ``failed`` list and this
    package's manifest, never by ``error``.
    """

    name: str
    src: Path | None = None
    pkg: str = ""
    out_dir: Path | None = None
    manifest_path: Path | None = None
    all_segments: list = field(default_factory=list)
    by_index: dict = field(default_factory=dict)
    old_entries: dict = field(default_factory=dict)
    seg_results: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    done_count: int = 0
    all_count: int = 0
    error: str | None = None
    dirty: bool = False


def _build_pool_rows(files, pool_start: int = 0) -> tuple:
    """Expand the files' pending segments into the unified pool rows (pure).

    Returns ``(pool_rows, pool_owners)`` — parallel lists, one row per pending segment in
    request order (files, then chapter-local index order). Each row is the chapter's
    segment dict plus the pooled-run fields the worker reads: ``index`` = the row's
    pool-global position (its scheduling identity: protocol lines, watchdog, file-number
    width) numbered consecutively from ``pool_start``; ``out_dir`` = the chapter's package
    dir (absolute); ``file_index`` = the line's position inside its chapter (the manifest
    key and the file number). Files with a prep fatal or nothing pending contribute no
    rows. (On a watchdog restart the remaining rows are filtered in place and keep their
    original pool indices — never renumbered.)
    """
    pool_rows: list[dict] = []
    pool_owners: list = []
    for f in files:
        if f.error or not f.pending:
            continue
        for local in f.pending:
            row = dict(f.by_index[local])
            row["index"] = pool_start
            row["out_dir"] = str(f.out_dir)
            row["file_index"] = local
            pool_rows.append(row)
            pool_owners.append(f)
            pool_start += 1
    return pool_rows, pool_owners


def _handle_segment_pool(line: str, pool_map: dict, pool_total: int, handle) -> None:
    """Route a ``[segment] <pool-index> ok|error <detail>`` line to its owning chapter.

    ``pool_index`` is the row's pool-global segment-table position (the worker's scheduling
    identity); ``pool_map`` is the single source mapping it back to ``(file, local index)``
    — the pool index is NEVER used as a chapter-internal index. Malformed lines and unknown
    indices are logged as warnings and dropped, exactly like the single-file
    :func:`_handle_segment`.
    """
    parts = line[len("[segment]"):].split(None, 2)
    if len(parts) < 2:
        handle.log(line, "WARNING")
        return
    try:
        pool_index = int(parts[0])
    except ValueError:
        handle.log(line, "WARNING")
        return
    status = parts[1]
    detail = parts[2] if len(parts) > 2 else ""
    owner = pool_map.get(pool_index)
    if owner is None:
        handle.log(f"未知段索引 {pool_index}（池共 {pool_total} 段）：{line}", "WARNING")
        return
    f, local = owner
    speaker = (f.by_index.get(local) or {}).get("speaker") or "(未知)"
    num = f"[{pool_index + 1}/{pool_total}]"
    if status == "ok":
        f.seg_results[local] = {"ok": True, "path": detail, "reason": ""}
        f.dirty = True
        handle.log(f"{num} {f.name} · {speaker}：完成")
    else:
        f.seg_results[local] = {"ok": False, "path": "", "reason": detail}
        f.dirty = True
        handle.log(f"{num} {f.name} · {speaker}：失败（{detail}）", "ERROR")


def _settle_pool(handle, files) -> dict:
    """Aggregate the pooled run's final result (the same shape the legacy multi run returned).

    ``files`` is in request order and so is the result's ``files`` list; the top-level
    ``failed`` list is ordered file order → chapter-local index order (the worker's
    cross-chapter execution order never leaks into any result or manifest ordering). Per
    file: a prep fatal is all-zeros + ``error``; a no-pending file reports its cumulative
    completion; a pooled file reports ``total = len(pending)`` and the count of this run's
    ok results. The failure test (``attempted`` — files that pooled or errored — with
    ``completed == 0``) mirrors the single-file fatal rule: a selection where every
    actually-pooled segment failed (or every file was a prep fatal) fails the task; an
    all-complete / all-empty / one-chapter-sinks-others-succeed selection succeeds.
    """
    per_file: list[dict] = []
    failed_all: list[dict] = []
    for f in files:
        if f.error:
            per_file.append({
                "script": f.name, "total": 0, "completed": 0, "failed": 0,
                "output_dir": "", "manifest_path": "", "done_count": 0, "all_count": 0,
                "error": f.error,
            })
            continue
        done = count_completion(f.all_segments, load_manifest(f.out_dir))
        # completed = this run's ok results; a no-pending file reports its cumulative
        # completion (the legacy zero-short-circuit shape).
        completed = (sum(1 for i in f.pending if (f.seg_results.get(i) or {}).get("ok"))
                     if f.pending else done["completed"])
        file_failed = 0
        for local in f.pending:
            r = f.seg_results.get(local)
            if r and r.get("ok"):
                continue
            file_failed += 1
            failed_all.append({
                "index": local,
                "speaker": (f.by_index.get(local) or {}).get("speaker", ""),
                "reason": (r or {}).get("reason") or "（引擎未返回结果）",
                "script": f.name,
            })
        per_file.append({
            "script": f.name,
            # total = this run's pooled rows; a no-pending file reports its whole segment
            # count (the legacy zero-short-circuit shape: all-done / empty files).
            "total": len(f.pending) if f.pending else f.all_count,
            "completed": completed,
            "failed": file_failed,
            "output_dir": str(f.out_dir),
            "manifest_path": str(f.manifest_path),
            "done_count": done["completed"],
            "all_count": done["total"],
            "error": None,
        })
    result = {
        "total": sum(r["total"] for r in per_file),
        "completed": sum(r["completed"] for r in per_file),
        "failed": failed_all,
        "output_dir": "",  # pooled run: each chapter has its own package (see ``files``)
        "manifest_path": "",
        "done_count": sum(r["done_count"] for r in per_file),
        "all_count": sum(r["all_count"] for r in per_file),
        "files": per_file,
    }
    attempted = [f for f in files if f.error or f.pending]
    if not attempted or result["completed"] > 0:
        handle.progress(1.0, "完成")
        return result
    if result["total"] > 0:
        raise RuntimeError(f"全部 {result['total']} 段合成失败（各章节原因见任务日志与结果 files 字段）。")
    raise RuntimeError(f"所选 {len(files)} 个文件全部无法合成（原因见任务日志与结果 files 字段）。")


def package_for(src: Path) -> str:
    """The package (sub-folder in ``05_audio_chunk/``) a source JSON's batch output lands in.

    Named after the source's base stem so 音频合并 can list & pick a package; a base and its
    ``_checked`` variant share the same package.
    """
    stem = src.stem
    if stem.endswith("_checked"):
        stem = stem[: -len("_checked")]
    return stem or "batch"


def load_manifest(out_dir) -> dict:
    """The package's cumulative manifest as ``{index: entry}`` (``{}`` if absent / unreadable).

    One entry per segment the batch has ever reported — the source of truth for what is already
    synthesized, so a cancel (or a later re-run) can resume without re-doing finished work.
    """
    p = Path(out_dir) / "manifest.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt manifest just means "start fresh"
        return {}
    if not isinstance(data, list):
        return {}
    # Lazy migration: legacy manifests stored absolute paths (the workspace's location at
    # write time). Convert any that still point inside the workspace to the relative form
    # and rewrite the file, so the project keeps working after the workspace moves.
    _n, migrated = pathio.migrate_entries_in(p, get_layout().workspace, "list", ("path",))
    data = migrated if migrated is not None else data
    by_index = {}
    for e in data:
        if isinstance(e, dict) and "index" in e:
            try:
                by_index[int(e["index"])] = e
            except (TypeError, ValueError):
                pass
    return by_index


def is_done(entry) -> bool:
    """Whether a manifest entry is a *completed* segment: ``ok`` AND its file still on disk.

    A missing file (deleted, or a run killed between writing the file and its line being
    drained) means the segment is not truly done, so a resume re-synthesizes it. The path
    value is resolved against the *current* workspace root (relative form; a legacy
    absolute value still works, and one invalidated by a workspace move is recovered
    best-effort) — never against a fixed location.
    """
    if not entry or not entry.get("ok"):
        return False
    path = entry.get("path")
    if not path:
        return False
    ws = get_layout().workspace
    try:
        p = pathio.resolve_path(path, ws, strict=False) if ws is not None else Path(path)
    except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
        return False
    if p is None:
        return False
    try:
        return p.exists()
    except (OSError, TypeError):
        return False


def plan_to_synthesize(all_indices, done_set, indices=None):
    """Which line indices a run should synthesize (a subset of ``all_indices``).

    An explicit ``indices`` wins (synthesise exactly those, intersected with the valid set);
    otherwise the default is a *resume* — only the not-yet-done lines (``all - done``).
    Re-doing everything is NOT a planning mode: the caller deletes the package folder first
    (``POST /api/tts/batch-reset``), after which an ordinary resume has nothing to skip.
    """
    all_set = set(all_indices)
    if indices:
        return {int(i) for i in indices} & all_set
    return all_set - set(done_set)


def _store_path(path, root):
    """The manifest's on-disk form of an audio path: workspace-relative when the file
    lives inside the workspace (the location-independent form), the value unchanged when
    it does not (an external resource keeps its absolute path)."""
    if not path or root is None:
        return path
    rel = pathio.to_workspace_relative(path, root)
    return path if rel is None else rel


def build_manifest(all_segments, old_entries, run_results, root=None):
    """Rebuild the package manifest: one entry per non-empty segment, in index order.

    For each segment this run's result wins; else a prior *done* entry is preserved (its existing
    audio path, paired with the current script's speaker/text/pause); else a not-done entry
    (``ok: false``, empty path) reusing a prior failure's reason when there is one. The same
    function powers both the incremental (per-segment) writes and the final write, so the file
    always holds the cumulative state and a cancel never loses finished work.

    ``root`` (the workspace root) gives every stored path the location-independent,
    workspace-relative form; with ``None`` (or for out-of-workspace paths) the value is
    stored as given. Passing the live root also migrates any legacy absolute value a
    preserved old entry still carries.
    """
    manifest = []
    for s in sorted(all_segments, key=lambda x: x["index"]):
        index = s["index"]
        base = {
            "index": index,
            "speaker": s["speaker"],
            "text": s["text"],
            "pause_after": s["pause_after"],
        }
        r = run_results.get(index)
        if r is not None:
            if r.get("ok"):
                manifest.append({**base, "path": _store_path(r.get("path", ""), root),
                                 "ok": True, "reason": ""})
            else:
                manifest.append({**base, "path": "", "ok": False, "reason": r.get("reason", "")})
        else:
            old = old_entries.get(index)
            if old is not None and is_done(old):
                manifest.append({**base, "path": _store_path(old.get("path", ""), root),
                                 "ok": True, "reason": ""})
            elif old is not None:
                manifest.append({**base, "path": "", "ok": False, "reason": old.get("reason", "")})
            else:
                manifest.append({**base, "path": "", "ok": False, "reason": ""})
    return manifest


def count_completion(all_segments, manifest_by_index) -> dict:
    """``{total, completed, remaining}`` for a script — completed = done (ok + file exists).

    ``total`` is the number of non-empty (synthesizable) segments; the ``synthesize`` result and
    the read-only ``/batch-status`` endpoint both derive their numbers from this.
    """
    total = len(all_segments)
    completed = sum(1 for s in all_segments if is_done(manifest_by_index.get(s["index"])))
    return {"total": total, "completed": completed, "remaining": total - completed}


def _write_manifest_file(manifest_path, manifest) -> None:
    """Write the (list) manifest to disk (UTF-8, pretty-printed; JSON tolerates the CRLF)."""
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _synthesize_one(handle, indices=None, script=None, concurrency=None, seed=None) -> dict:
    """Task worker: synthesize ONE script's lines (default = resume: only the not-yet-done).

    The single-file body behind :func:`synthesize` (the legacy API path and the tests).
    The multi-file shape does NOT call this: ``synthesize_multi`` pools every chapter's
    pending segments into one engine subprocess (see there) instead of running one
    subprocess per file.

    ``concurrency`` is only the *per-batch ceiling* (the most segments that may share one GPU
    tensor batch); the worker sets the actual size at runtime from the segment-length bands and
    a measured VRAM governor (never a fixed concurrency). When omitted it falls back to the
    persisted default (``config.tts.batch_concurrency``); either way it is clamped to
    ``[1, 64]`` before being handed to the worker. ``seed`` (>=0) makes a run reproducible; when
    omitted it uses ``config.tts.batch_seed`` (-1 = random).

    A hung / OOM-killed child (the worker's watchdog, exit 124) is *not* a fatal failure: the run
    shrinks the batch (halving the cap, floor 1) and restarts a fresh subprocess (resume semantics
    skip the already-done segments), down to batch 1, where a repeat timeout strikes the in-flight
    segment and two strikes isolate it as a recorded failure (the run continues without it).

    The package ``manifest.json`` is the cumulative source of truth: the in-memory state updates
    after every segment, and the disk rewrite is throttled (at most once per
    ``MANIFEST_FLUSH_INTERVAL``) with a forced flush on cancel / engine failure / watchdog restart
    / completion — a cancel keeps whatever finished; only a hard kill of the backend can lose up
    to the interval's worth of segments (their files stay on disk, so a resume re-does only what
    the manifest still lacks). A run is a *resume* — it skips segments already done
    (``ok`` + file on disk); a resume with nothing left short-circuits without spawning the
    engine. Re-doing everything is not a mode here: the caller deletes the package folder
    first (``POST /api/tts/batch-reset``), after which an ordinary resume has nothing to skip.
    """
    src = resolve_parsed_json(script)
    script = _load_script(src)

    layout = get_layout()
    ws = layout.workspace

    # voice_config is optional here — a character missing from it becomes a clear
    # per-segment error (the run continues), not a crash.
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"voice_config.json 无法解析（{e}）——相关角色将失败。", "WARNING")
        # Lazy migration of legacy absolute ref_audio values (the worker resolves the
        # relative form against --workspace, so the file must be rewritten before spawn).
        _n, migrated_vc = pathio.migrate_entries_in(vc_path, ws, "dict", ("ref_audio",))
        if isinstance(migrated_vc, dict):
            voice_config = migrated_vc

    out_dir = layout.audio_chunk / package_for(src)
    manifest_path = out_dir / "manifest.json"

    # The full set of synthesizable segments (the manifest always describes exactly these) and
    # the subset this run will actually synthesize (a resume = the not-yet-done ones).
    all_segments = _build_segments(script)
    all_indices = {s["index"] for s in all_segments}
    old_entries = load_manifest(out_dir)
    done_set = {i for i in all_indices if is_done(old_entries.get(i))}
    to_do = plan_to_synthesize(all_indices, done_set, indices)
    segments = [s for s in all_segments if s["index"] in to_do]
    run_total = len(segments)

    if indices is None and done_set:
        handle.log(f"续合：已完成 {len(done_set)} 段，本次合成剩余 {run_total} 段（共 {len(all_segments)} 段）")
    else:
        handle.log(f"开始音频合成：{run_total} 段")

    speakers_in_batch = sorted({s["speaker"] for s in segments if s["speaker"]})
    if speakers_in_batch:
        handle.log(f"涉及角色：{'、'.join(speakers_in_batch)}")

    # Nothing left to do (a resume that is already complete) — persist the full manifest and stop
    # without spawning the engine (no wasted model load).
    if run_total == 0:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest_file(manifest_path, build_manifest(all_segments, old_entries, {}, root=ws))
        done = count_completion(all_segments, old_entries)["completed"]
        handle.log(f"已全部完成，无需合成（{done}/{len(all_segments)} 段）。")
        handle.progress(1.0, "完成")
        return {
            "total": len(all_segments),
            "completed": done,
            "failed": [],
            "output_dir": str(out_dir),
            "manifest_path": str(manifest_path),
            "done_count": done,
            "all_count": len(all_segments),
        }

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts
    # 批内段数（仅上限）: the request's value, else the persisted default
    # (config.tts.batch_concurrency); clamped to [1, 64] so a stray value can't spawn a
    # degenerate / unbounded cap (the worker sets the actual per-batch size at runtime).
    workers = clamp_concurrency(concurrency if concurrency else t.batch_concurrency)
    # seed: the request's value, else the persisted default (config.tts.batch_seed); -1 = random.
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    handle.log(f"引擎：.venv-tts（一次性子进程，模型只加载一次）· 批内上限 {workers} 段")
    # A persistent per-run transcript: the task log is SSE-only and vanishes with the session,
    # so every line the engine emits is also mirrored to this file (one per run, appended per
    # restart attempt) — a run that dies mid-batch leaves its exact batch / watchdog / error
    # trail on disk for diagnosis.
    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle.log(f"运行日志（排障用，含每次重启的完整引擎输出）：{run_log}")
    handle.progress(0.02, "启动引擎")

    seg_results: dict = {}  # index -> {ok, path, reason} (this run)
    by_index = {s["index"]: s for s in segments}
    in_flight: set = set()  # indices the current child was generating (from its [watchdog] line)

    # In-memory truth updates on every [segment] line, but the full-file disk rewrite is
    # throttled to at most once per MANIFEST_FLUSH_INTERVAL: under disk / AV-scanner load a
    # 1MB rewrite per line stalled this line-processing loop (task log and progress bar crept
    # one line at a time while the worker itself was tensor-batching at full speed). The first
    # line always flushes (last_flush starts at 0); forced flushes still fire on cancel, engine
    # failure, watchdog restart and completion — a cancel loses nothing, a hard kill of the
    # backend loses at most the interval's worth of segments (their files stay on disk, so a
    # resume re-does only what the manifest still lacks).
    last_flush = [0.0]  # time.monotonic() of the last manifest write to disk

    def _write_manifest(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < MANIFEST_FLUSH_INTERVAL:
            return
        _write_manifest_file(manifest_path, build_manifest(all_segments, old_entries, seg_results, root=ws))
        last_flush[0] = now

    def on_line(line: str) -> None:
        if line.startswith("[segment]"):
            _handle_segment(line, by_index, run_total, seg_results, handle)
            _write_manifest()
        elif line.startswith("[watchdog]"):
            # The child names the batch that hung before it exits 124: remember the in-flight
            # indices so a strike at workers==1 targets the right segment(s).
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        else:
            handle.log(line)

    # -- the watchdog / restart loop ----------------------------------------------
    # A hung or OOM-killed child (exit 124) is not a fatal task failure: shrink the batch and
    # restart a fresh subprocess (resume semantics skip the done), down to workers==1, where a
    # repeat timeout strikes the in-flight segment; two strikes isolate it as a recorded failure.
    MAX_ATTEMPTS = 8
    excluded: set = set()
    struck: dict = {}
    attempt = 0
    try:
        while True:
            if attempt > MAX_ATTEMPTS:
                raise RuntimeError(
                    f"音频合成引擎反复超时（{MAX_ATTEMPTS} 次缩批重试后仍未完成）——已完成进度已保住，"
                    f"请调小「批内段数」后重试。"
                )
            remaining = [s for s in segments
                         if s["index"] not in excluded
                         and not (seg_results.get(s["index"]) or {}).get("ok")]
            if not remaining:
                break
            seg_file.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
            cmd = _build_cmd(
                python, worker, seg_file, vc_path, out_dir,
                language=t.language, device=t.device,
                model=t.model, base_model=t.base_model, design_model=t.design_model,
                ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers, seed=seed,
                workspace=ws, disabled_checks=disabled_planner_checks(t),
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                run_worker(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log)
                break  # a clean exit (0)
            except WorkerWatchdogTimeout:
                attempt += 1
                if workers > 1:
                    workers = max(1, workers // 2)
                    handle.log(f"看门狗触发（批内段超时）→ 批内上限缩到 {workers} 段，重启引擎", "WARNING")
                else:
                    # workers == 1: strike the in-flight segment; two strikes isolate a poison
                    # segment
                    newly = []
                    for i in list(in_flight):
                        struck[i] = struck.get(i, 0) + 1
                        if struck[i] >= 2:
                            excluded.add(i)
                            seg_results[i] = {"ok": False, "path": "", "reason": "超时（已隔离）"}
                            newly.append(i)
                    if newly:
                        names = "、".join(f"第 {i + 1} 段" for i in sorted(newly))
                        handle.log(f"{names} 连续两次超时 → 隔离为失败，其余段继续", "WARNING")
                    else:
                        handle.log("看门狗触发（单段超时，首次记罚）→ 重启引擎重试", "WARNING")
                # The restart rebuilds its segment table from in-memory state (a throttled
                # manifest can't cause re-synthesis) — flush anyway so a backend crash in the
                # gap can't make a later resume re-do the last ~2s.
                _write_manifest(force=True)
                continue
    except Exception:
        # Cancel / engine failure / attempt cap: flush whatever finished since the last
        # throttled write, then let the exception settle the task as before.
        _write_manifest(force=True)
        raise

    # Final manifest (the throttled writes may lag up to the interval; this one is
    # authoritative — also a safety net in case the child exits before its last line is
    # drained).
    _write_manifest(force=True)

    completed = 0
    failed = []
    for s in segments:
        r = seg_results.get(s["index"])
        if r and r.get("ok"):
            completed += 1
        else:
            failed.append({"index": s["index"], "speaker": s["speaker"],
                           "reason": (r or {}).get("reason") or "（引擎未返回结果）"})

    done = count_completion(all_segments, load_manifest(out_dir))
    handle.log(
        f"音频合成结束：本次成功 {completed} / 失败 {len(failed)} / 共 {run_total} 段；"
        f"累计已合成 {done['completed']}/{done['total']} 段。清单：{manifest_path.name}"
    )

    if completed == 0:
        first = failed[0]["reason"] if failed else "无"
        raise RuntimeError(f"本次 {run_total} 段全部合成失败。首个原因：{first}")

    handle.progress(1.0, "完成")
    return {
        "total": run_total,
        "completed": completed,
        "failed": failed,
        "output_dir": str(out_dir),
        "manifest_path": str(manifest_path),
        "done_count": done["completed"],
        "all_count": done["total"],
    }


def synthesize(handle, indices=None, script=None, concurrency=None, seed=None) -> dict:
    """Single-file entry (the legacy ``POST /api/tts/batch`` path and the tests): delegates
    verbatim to :func:`_synthesize_one`. Signature kept identical so positional callers work."""
    return _synthesize_one(handle, indices, script, concurrency, seed)


def synthesize_multi(handle, scripts, concurrency=None, seed=None) -> dict:
    """Task worker: synthesize several parsed JSON files (chapters) in ONE task — pooled.

    Every selected chapter's *pending* segments (resume: the not-yet-done lines) are read
    into one unified TTS task pool and a SINGLE engine subprocess schedules the whole pool:
    the worker's speaker grouping / ordering / lazy sub-batch / VRAM governance now run
    across chapter boundaries (the model set loads once; a short chapter's tail no longer
    starves the batch). Each pool row carries its chapter attribution (``out_dir`` = the
    chapter's package dir, ``file_index`` = the line's position inside that chapter) so
    every result lands back in the right chapter package — the pool's scheduling order is
    decoupled from the chapter order, but results and manifests keep the chapter-local
    order. 章节是数据组织单位，不是 GPU 调度单位。

    Prep-stage fatals (missing / corrupt / empty script, a package collision) are
    *isolated* per file: logged, recorded on that file's ``error`` entry, and the run
    continues with the other files. An engine-level "all of a file's segments failed"
    does NOT set ``error`` — it is expressed by the top-level ``failed`` list (each entry
    tagged with its ``script``) and the chapter's own manifest ``ok: false`` entries (one
    bad chapter never sinks the pool — the same principle as 角色配音's per-character
    failure isolation). The task fails only when every actually-pooled segment failed
    (or every file was a prep fatal); an all-complete / all-empty selection settles as a
    success without spawning the engine.

    Progress is driven directly by the worker's ``[progress]`` lines (0→1 over the whole
    pool — the backend emits no per-file progress windows). Cancellation propagates:
    ``TaskCancelled`` is re-raised (NEVER swallowed by the per-file error isolation); a
    forced manifest flush covers every pooled file first, so a cancel keeps whatever
    finished.
    """
    if not scripts:
        raise RuntimeError("没有可合成的文件——请先在「待合成」列表勾选解析 JSON。")
    n = len(scripts)
    layout = get_layout()
    ws = layout.workspace

    # voice_config is optional here — a character missing from it becomes a clear
    # per-segment error (the run continues), not a crash. Read + lazily migrated ONCE for
    # the whole pool (same logic as _synthesize_one; the worker resolves the relative
    # ref_audio against --workspace, so the rewrite happens before any spawn).
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"voice_config.json 无法解析（{e}）——相关角色将失败。", "WARNING")
        _n, migrated_vc = pathio.migrate_entries_in(vc_path, ws, "dict", ("ref_audio",))
        if isinstance(migrated_vc, dict):
            voice_config = migrated_vc

    # -- per-file prep (request order): fatal files are isolated, the rest join the pool --
    files: list[_PooledFile] = []
    pkg_owner: dict = {}  # package -> first file claiming it (collision defence)
    for i, name in enumerate(scripts):
        handle.check()  # cancel / pause point before any work on file i
        f = _PooledFile(name=name)
        files.append(f)
        handle.log(f"文件 {i + 1}/{n}：{name}")
        try:
            src = resolve_parsed_json(name)
            script = _load_script(src)
            f.src = src
            pkg = package_for(src)
            # API-layer defence: the UI already filters _checked names, but a direct API
            # call could pass both x.json and x_checked.json — they map to ONE package and
            # would write the same files / manifest, so fail the second clearly instead of
            # corrupting silently.
            if pkg in pkg_owner:
                raise RuntimeError(f"包目录 {pkg} 与 {pkg_owner[pkg]} 冲突（同一包不可被两个文件合成）")
            pkg_owner[pkg] = name
            f.pkg = pkg
            f.out_dir = layout.audio_chunk / pkg
            f.manifest_path = f.out_dir / "manifest.json"
            f.all_segments = _build_segments(script)
            f.by_index = {s["index"]: s for s in f.all_segments}
            f.old_entries = load_manifest(f.out_dir)
            all_indices = {s["index"] for s in f.all_segments}
            done_set = {i for i in all_indices if is_done(f.old_entries.get(i))}
            f.pending = sorted(plan_to_synthesize(all_indices, done_set))
            f.all_count = len(f.all_segments)
            if f.pending:
                if done_set:
                    handle.log(f"续合：已完成 {len(done_set)} 段，待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
                else:
                    handle.log(f"待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
            else:
                # Nothing left (all done, or an empty script) — rewrite the engine-owned
                # manifest and contribute no rows to the pool (no wasted model work).
                f.out_dir.mkdir(parents=True, exist_ok=True)
                _write_manifest_file(f.manifest_path,
                                     build_manifest(f.all_segments, f.old_entries, {}, root=ws))
                handle.log("无待合成段，跳过" if not f.all_count else f"已全部完成，跳过（0/{f.all_count} 段待合成）")
        except TaskCancelled:
            raise  # cancel is a task-level outcome — never "file failed, keep going"
        except Exception as e:  # noqa: BLE001 — one bad file is isolated, the pool continues
            f.error = str(e)
            handle.log(f"文件 {name} 准备失败（跳过，继续其余文件）：{e}", "ERROR")

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    # -- build the unified pool ----------------------------------------------------------
    pool_rows, pool_owners = _build_pool_rows(files)
    pool_total = len(pool_rows)
    # The single source mapping pool-global index -> (chapter file, chapter-local index).
    pool_map = {row["index"]: (f, row["file_index"]) for row, f in zip(pool_rows, pool_owners)}

    # -- engine prep (once; the model set loads once for the whole pool) ------------------
    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts
    # 批内段数（仅上限）: the request's value, else the persisted default
    # (config.tts.batch_concurrency); clamped to [1, 64] — ONE cap for the whole pool.
    workers = clamp_concurrency(concurrency if concurrency else t.batch_concurrency)
    # seed: the request's value, else the persisted default (config.tts.batch_seed); -1 = random.
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    # Multi-manifest throttled flush: one shared 2s clock, every pooled (dirty, non-fatal)
    # file's manifest rewritten together. Forced flushes (cancel / engine failure /
    # watchdog restart / completion) cover ALL pooled files — a cancel keeps whatever
    # finished in every chapter, not just the last one touched.
    last_flush = [0.0]

    def flush_manifests(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < MANIFEST_FLUSH_INTERVAL:
            return
        for f in files:
            if f.error or not f.pending:
                continue
            if not force and not f.dirty:
                continue
            f.dirty = False
            _write_manifest_file(f.manifest_path,
                                 build_manifest(f.all_segments, f.old_entries, f.seg_results, root=ws))
        last_flush[0] = now

    # In-flight POOL indices the current child was generating (from its [watchdog] line) —
    # shared with on_line below; a strike at workers==1 targets these, mapped back to their
    # chapters for the manifest write.
    in_flight: set = set()

    def on_line(line: str) -> None:
        if line.startswith("[segment]"):
            _handle_segment_pool(line, pool_map, pool_total, handle)
            flush_manifests()
        elif line.startswith("[watchdog]"):
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        else:
            handle.log(line)

    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"

    # No pool (everything done / empty, or every file a prep fatal): settle without the
    # engine — all-fatals raise inside _settle_pool (nothing was synthesized).
    if pool_total == 0:
        handle.log(f"无待合成段（{n} 个文件），不启动引擎")
        return _settle_pool(handle, files)

    # The pooled rows' own out_dir decides their save location; the whole-batch --out-dir is
    # just the worker's required fallback (constraint 3) — the first pooled file's package.
    for f in files:
        if f.pending and not f.error:
            f.out_dir.mkdir(parents=True, exist_ok=True)  # the manifest flush needs the dir
    out_dir_fallback = next(f.out_dir for f in files if f.pending and not f.error)
    handle.log(f"引擎：.venv-tts（一次性子进程，全池统一调度，模型只加载一次）· 批内上限 {workers} 段")
    handle.log(f"运行日志（排障用，含每次重启的完整引擎输出）：{run_log}")

    MAX_ATTEMPTS = 8
    excluded: set = set()  # pool indices isolated after two strikes
    struck: dict = {}
    attempt = 0
    try:
        while True:
            if attempt > MAX_ATTEMPTS:
                raise RuntimeError(
                    f"音频合成引擎反复超时（{MAX_ATTEMPTS} 次缩批重试后仍未完成）——已完成进度已保住，"
                    f"请调小「批内段数」后重试。"
                )
            # The restart keeps the remaining rows' ORIGINAL pool indices (no renumbering):
            # the manifest / result mapping and the workers' protocol stay stable across
            # restarts.
            remaining = []
            for row in pool_rows:
                if row["index"] in excluded:
                    continue
                f, local = pool_map[row["index"]]
                if (f.seg_results.get(local) or {}).get("ok"):
                    continue
                remaining.append(row)
            if not remaining:
                break
            seg_file.write_bytes(json.dumps(remaining, ensure_ascii=False).encode("utf-8"))
            cmd = _build_cmd(
                python, worker, seg_file, vc_path, out_dir_fallback,
                language=t.language, device=t.device,
                model=t.model, base_model=t.base_model, design_model=t.design_model,
                ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers, seed=seed,
                workspace=ws, disabled_checks=disabled_planner_checks(t),
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                run_worker(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log)
                break  # a clean exit (0)
            except WorkerWatchdogTimeout:
                attempt += 1
                if workers > 1:
                    workers = max(1, workers // 2)
                    handle.log(f"看门狗触发（批内段超时）→ 批内上限缩到 {workers} 段，重启引擎", "WARNING")
                else:
                    # workers == 1: strike the in-flight POOL rows; two strikes isolate a
                    # poison row — mapped back to its chapter for the manifest write.
                    newly = []
                    for pi in sorted(in_flight):
                        struck[pi] = struck.get(pi, 0) + 1
                        if struck[pi] >= 2:
                            excluded.add(pi)
                            f, local = pool_map[pi]
                            f.seg_results[local] = {"ok": False, "path": "", "reason": "超时（已隔离）"}
                            f.dirty = True
                            newly.append((f, local, pi))
                    if newly:
                        names = "、".join(f"{f.name} 第 {local + 1} 段（池内第 {pi + 1}）"
                                          for f, local, pi in sorted(newly, key=lambda t_: t_[2]))
                        handle.log(f"{names} 连续两次超时 → 隔离为失败，其余段继续", "WARNING")
                    else:
                        handle.log("看门狗触发（单段超时，首次记罚）→ 重启引擎重试", "WARNING")
                # The restart rebuilds its segment table from in-memory state (a throttled
                # manifest can't cause re-synthesis) — flush every pooled file anyway so a
                # backend crash in the gap can't make a later resume re-do the last ~2s.
                flush_manifests(force=True)
                continue
    except Exception:
        # Cancel / engine failure / attempt cap: force-flush every pooled file's manifest,
        # then let the exception settle the task (TaskCancelled re-raised verbatim).
        flush_manifests(force=True)
        raise

    # Final authoritative manifests (the throttled writes may lag up to the interval; also a
    # safety net in case the child exits before its last line is drained).
    flush_manifests(force=True)
    return _settle_pool(handle, files)
