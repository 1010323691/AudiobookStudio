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


def _build_cmd(python, worker, seg_file, vc_path, out_dir, *, language, device,
               model, base_model, design_model, ffmpeg_path, concurrency, seed,
               workspace=None) -> list:
    """The one-shot ``.venv-tts`` batch command (pure; factored out for testing).

    ``--concurrency`` is always present (a clamped int) — the worker's *per-batch ceiling*
    (the length bands + measured VRAM governor set the actual size); ``--seed`` (-1 = random)
    makes a run reproducible. Empty model ids are omitted so the worker falls back to its own
    (identical) defaults. ``--workspace`` hands the worker the workspace root so the
    workspace-relative ``ref_audio`` values in ``voice_config.json`` resolve correctly there
    (the worker's own cwd is the project root, not the workspace).
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


def plan_to_synthesize(all_indices, done_set, indices=None, force_all=False):
    """Which line indices a run should synthesize (a subset of ``all_indices``).

    An explicit ``indices`` wins (synthesise exactly those, intersected with the valid set);
    otherwise ``force_all`` re-does everything; otherwise the default is a *resume* — only the
    not-yet-done lines (``all - done``).
    """
    all_set = set(all_indices)
    if indices:
        return {int(i) for i in indices} & all_set
    if force_all:
        return all_set
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


def _synthesize_one(handle, indices=None, script=None, concurrency=None, force_all=False, seed=None) -> dict:
    """Task worker: synthesize ONE script's lines (default = resume: only the not-yet-done).

    The shared per-file body of both run shapes: ``synthesize`` (single file, the legacy
    API path / tests) and ``synthesize_multi`` (several files in one task, in order) call
    this — with a progress-scaling :class:`_ScaledHandle` in the multi case.

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

    The package ``manifest.json`` is the cumulative source of truth and is written *incrementally*
    (after every segment), so a cancel keeps whatever finished. The default run is a *resume* —
    it skips segments already done (``ok`` + file on disk); ``force_all`` re-synthesizes every line.
    A resume with nothing left short-circuits without spawning the engine.
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
    to_do = plan_to_synthesize(all_indices, done_set, indices, force_all)
    segments = [s for s in all_segments if s["index"] in to_do]
    run_total = len(segments)

    if force_all:
        handle.log(f"重新全部合成：{run_total} 段（全部重做；共 {len(all_segments)} 段）")
    elif indices is None and done_set:
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

    def _write_manifest() -> None:
        # The cumulative manifest, flushed after every segment so a cancel keeps what finished.
        _write_manifest_file(manifest_path, build_manifest(all_segments, old_entries, seg_results, root=ws))

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
            workspace=ws,
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
                continue
            # workers == 1: strike the in-flight segment; two strikes isolate a poison segment
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
                _write_manifest()
            else:
                handle.log("看门狗触发（单段超时，首次记罚）→ 重启引擎重试", "WARNING")
            continue

    # Final manifest (the incremental writes already cover it; a safety net in case the child
    # exits before its last line is drained).
    _write_manifest()

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


def synthesize(handle, indices=None, script=None, concurrency=None, force_all=False, seed=None) -> dict:
    """Single-file entry (the legacy ``POST /api/tts/batch`` path and the tests): delegates
    verbatim to :func:`_synthesize_one`. Signature kept identical so positional callers work."""
    return _synthesize_one(handle, indices, script, concurrency, force_all, seed)


class _ScaledHandle:
    """One file's view of a multi-file task handle: progress scaled, everything else verbatim.

    The task has a single progress bar; file *i* of *n* runs in the window
    ``[i/n, (i+1)/n]`` (``base + frac·weight``), so the bar climbs monotonically across
    files and can never dip below the start of the current file (even across the worker's
    restart-on-watchdog progress reset). Non-empty progress labels are prefixed with the
    file name so the step row always names the file being synthesized (a bare "完成" from
    file *i* would otherwise linger while file *i*+1 starts). ``log`` / ``check`` (and the
    LLM telemetry methods, unused on this path) forward to the parent untouched, so
    cancel/pause semantics are identical to a single-file run.
    """

    def __init__(self, parent, base: float, weight: float, name: str):
        self._parent = parent
        self._base = base
        self._weight = weight
        self._name = name

    def progress(self, frac: float, current: str = "") -> None:
        label = f"{self._name} · {current}" if current else ""
        self._parent.progress(self._base + float(frac) * self._weight, label)

    def log(self, msg: str, level: str = "INFO") -> None:
        self._parent.log(msg, level)

    def check(self) -> None:
        self._parent.check()

    def llm_chunk(self, text: str) -> None:
        self._parent.llm_chunk(text)

    def llm_rate(self, chars: int, cps: float) -> None:
        self._parent.llm_rate(chars, cps)

    def llm_chars(self, chars: int, secs: float) -> None:
        self._parent.llm_chars(chars, secs)


def synthesize_multi(handle, scripts, concurrency=None, force_all=False, seed=None) -> dict:
    """Task worker: synthesize several parsed JSON files in ONE task, sequentially.

    Each file is one package (``05_audio_chunk/<stem>/``) synthesized by its own one-shot
    engine subprocess — the model loads once per file, and a file that is already fully
    done short-circuits without spawning the engine at all (no wasted model load). The run
    is sequential: two engine processes would never run at once (they would fight over
    GPU memory).

    Per-file fatal errors (missing / corrupt / empty script, an engine run that produces
    nothing) are *isolated*: logged, recorded on that file's result entry, and the run
    continues with the next file — one bad file never sinks the batch (the same principle
    as 角色配音's per-character failure isolation). The task fails only when nothing was
    synthesized while something was actually attempted; an all-complete / all-empty
    selection settles as a success (each such file took the zero-short-circuit).

    Cancellation propagates: ``TaskCancelled`` is re-raised (NEVER swallowed by the
    per-file error isolation), so a cancel mid-run settles as ``cancelled`` — whatever
    finished in the earlier files is kept (each file's manifest is written incrementally).
    """
    if not scripts:
        raise RuntimeError("没有可合成的文件——请先在「待合成」列表勾选解析 JSON。")
    n = len(scripts)
    per_file: list[dict] = []
    failed_all: list[dict] = []
    for i, name in enumerate(scripts):
        handle.check()  # cancel / pause point before any work on file i
        sub = _ScaledHandle(handle, i / n, 1.0 / n, name)
        sub.log(f"文件 {i + 1}/{n}：{name}")
        try:
            res = _synthesize_one(sub, None, name, concurrency, force_all, seed)
            for f in res["failed"]:
                failed_all.append({**f, "script": name})
            per_file.append({
                "script": name,
                "total": res["total"],
                "completed": res["completed"],
                "failed": len(res["failed"]),
                "output_dir": res["output_dir"],
                "manifest_path": res["manifest_path"],
                "done_count": res["done_count"],
                "all_count": res["all_count"],
                "error": None,
            })
        except TaskCancelled:
            raise  # cancel is a task-level outcome — never "file failed, keep going"
        except Exception as e:  # noqa: BLE001 — one bad file is isolated, the batch continues
            handle.log(f"文件 {name} 合成失败（跳过，继续其余文件）：{e}", "ERROR")
            per_file.append({
                "script": name, "total": 0, "completed": 0, "failed": 0,
                "output_dir": "", "manifest_path": "", "done_count": 0, "all_count": 0,
                "error": str(e),
            })

    result = {
        "total": sum(r["total"] for r in per_file),
        "completed": sum(r["completed"] for r in per_file),
        "failed": failed_all,
        "output_dir": "",  # multi-file: each file has its own package (see ``files``)
        "manifest_path": "",
        "done_count": sum(r["done_count"] for r in per_file),
        "all_count": sum(r["all_count"] for r in per_file),
        "files": per_file,
    }
    # Nothing synthesized while something WAS attempted (or a file errored) is a failure;
    # an all-complete / all-empty selection succeeds — the legacy zero-short-circuit, per file.
    attempted = [r for r in per_file if r["total"] > 0 or r["error"]]
    if not attempted or result["completed"] > 0:
        handle.progress(1.0, "完成")
        return result
    raise RuntimeError(
        f"全部 {result['total']} 段合成失败（各文件原因见任务日志与结果 files 字段）。"
    )
