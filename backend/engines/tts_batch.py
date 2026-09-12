"""Batch TTS engine — synthesize every script line (subprocess orchestrator).

One long-running Task drives the worker's ``batch`` mode: a single ``.venv-tts``
subprocess loads the needed model(s) once and synthesizes all segments in JSON
order, so a full book runs in one process (models loaded once) while the 3.14
backend still never imports torch. The child's stdout is pumped line-by-line into
the task's progress/log — the real-time "第 i/N 段 · 角色：X · 正在生成" stream — and
each per-segment ``[segment]`` line is recorded, so a single failure never freezes
the run. A manifest of what was produced is written for the Merge stage.

``synthesize`` is a Task worker (first arg is a :class:`TaskHandle`), mirroring
``engines/tts.py``: it streams progress/log, honours cooperative cancel (killing
the child), and marks the task FAILED only on a *fatal* error (no segments, engine
down, or every segment failed) — a per-segment failure is a recorded, non-fatal line.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..core.config import get_config
from ..core.paths import get_layout, resolve_parsed_json
from .tts import DEFAULT_LANGUAGE, DEFAULT_MODEL, resolve_engine, run_worker

IMPLEMENTED = True

# 一键合成并发段数的上下界（前端输入与后端钳制共用）。
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 32


def clamp_concurrency(n) -> int:
    """Clamp a requested concurrency to ``[MIN_CONCURRENCY, MAX_CONCURRENCY]``.

    ``None`` / non-integer / out-of-range values collapse to a safe in-range int, so a
    stray config value or request can never spawn a degenerate pool (0) or an unbounded
    one (e.g. 999).
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = MIN_CONCURRENCY
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, v))


def _build_cmd(python, worker, seg_file, vc_path, out_dir, *, language, device,
               model, base_model, design_model, ffmpeg_path, concurrency) -> list:
    """The one-shot ``.venv-tts`` batch command (pure; factored out for testing).

    ``--concurrency`` is always present (a clamped int) so the worker's thread pool is
    sized explicitly; empty model ids are omitted so the worker falls back to its own
    (identical) defaults.
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
    ]
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
    drained) means the segment is not truly done, so a resume re-synthesizes it.
    """
    if not entry or not entry.get("ok"):
        return False
    path = entry.get("path")
    if not path:
        return False
    try:
        return Path(path).exists()
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


def build_manifest(all_segments, old_entries, run_results):
    """Rebuild the package manifest: one entry per non-empty segment, in index order.

    For each segment this run's result wins; else a prior *done* entry is preserved (its existing
    audio path, paired with the current script's speaker/text/pause); else a not-done entry
    (``ok: false``, empty path) reusing a prior failure's reason when there is one. The same
    function powers both the incremental (per-segment) writes and the final write, so the file
    always holds the cumulative state and a cancel never loses finished work.
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
                manifest.append({**base, "path": r.get("path", ""), "ok": True, "reason": ""})
            else:
                manifest.append({**base, "path": "", "ok": False, "reason": r.get("reason", "")})
        else:
            old = old_entries.get(index)
            if old is not None and is_done(old):
                manifest.append({**base, "path": old.get("path", ""), "ok": True, "reason": ""})
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


def synthesize(handle, indices=None, script=None, concurrency=None, force_all=False) -> dict:
    """Task worker: synthesize the script's lines (default = resume: only the not-yet-done).

    ``concurrency`` is the max number of segments synthesized in parallel (a thread pool inside
    the single TTS subprocess, which still loads each needed model once); when omitted it falls
    back to the persisted default (``config.tts.batch_concurrency``). Either way it is clamped to
    ``[1, 32]`` before being handed to the worker.

    The package ``manifest.json`` is the cumulative source of truth and is written *incrementally*
    (after every segment), so a cancel keeps whatever finished. The default run is a *resume* —
    it skips segments already done (``ok`` + file on disk); ``force_all`` re-synthesizes every line.
    A resume with nothing left short-circuits without spawning the engine.
    """
    src = resolve_parsed_json(script)
    script = _load_script(src)

    # voice_config is optional here — a character missing from it becomes a clear
    # per-segment error (the run continues), not a crash.
    vc_path = get_layout().voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"voice_config.json 无法解析（{e}）——相关角色将失败。", "WARNING")

    layout = get_layout()
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
        _write_manifest_file(manifest_path, build_manifest(all_segments, old_entries, {}))
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
    seg_file.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)

    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts
    # 并发段数: the request's value, else the persisted default (config.tts.batch_concurrency);
    # clamped to [1, 32] so a stray value can't spawn a degenerate / unbounded pool.
    workers = clamp_concurrency(concurrency if concurrency else t.batch_concurrency)

    cmd = _build_cmd(
        python, worker, seg_file, vc_path, out_dir,
        language=t.language, device=t.device,
        model=t.model, base_model=t.base_model, design_model=t.design_model,
        ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers,
    )

    handle.log(f"引擎：.venv-tts（一次性子进程，模型只加载一次）· 并发 {workers} 段")
    handle.progress(0.02, "启动引擎")

    seg_results: dict = {}  # index -> {ok, path, reason} (this run)
    by_index = {s["index"]: s for s in segments}

    def _write_manifest() -> None:
        # The cumulative manifest, flushed after every segment so a cancel keeps what finished.
        _write_manifest_file(manifest_path, build_manifest(all_segments, old_entries, seg_results))

    def on_line(line: str) -> None:
        if line.startswith("[segment]"):
            _handle_segment(line, by_index, run_total, seg_results, handle)
            _write_manifest()
        else:
            handle.log(line)

    run_worker(cmd, handle, on_line, temp_files=(seg_file,), fail_prefix="音频合成引擎")

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
