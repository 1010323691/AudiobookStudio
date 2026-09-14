"""Merge engine — combine per-segment audio into the final audiobook.

Reads the manifest written by the batch stage, hands the ordered per-segment files
(speakers + per-segment pause overrides) to the worker's ``merge`` mode — which
merges in two stages in the isolated env (it has pydub / numpy / soundfile; the
lean 3.14 backend does not): per-batch part WAVs (``MERGE_BATCH_SIZE`` segments
each, staged in a ``00_temp`` dir) are combined first, then the parts into the
whole book. That keeps a merge of thousands of segments reporting live per-batch /
per-part / per-encode progress instead of going silent. Order is preserved, the
inter-batch pauses keep the single-pass semantics (segment ``pause_after`` >
same speaker > speaker change), missing files are skipped with a clear warning,
and a merge failure (no audio / engine error) marks the task FAILED. The staging
dir is this module's to clean (try/finally) on every exit path.

``run`` is a Task worker (first arg is a :class:`TaskHandle`); it streams progress /
log over SSE and honours cooperative cancel (killing the child).
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from ..core import pathio
from ..core.config import get_config
from ..core.paths import get_layout
from .tts import resolve_engine, run_worker

IMPLEMENTED = True

# Segments per part WAV in the two-stage merge (passed as the worker's --merge-batch-size).
MERGE_BATCH_SIZE = 100

# Windows-illegal filename characters (a package name becomes an output file name).
_BAD_FILENAME_CHARS = set('\\/:*?"<>|')


def collect_segments(manifest, ws):
    """The ordered merge inputs from a parsed manifest (the pure core of ``run``).

    Keeps only ``ok`` entries whose file actually exists at the resolved location, in
    manifest order, and re-orders by index so the merged file always matches the
    original JSON order. Each stored path is resolved against the *current* workspace
    root ``ws`` (relative form; a legacy absolute value still works, and one invalidated
    by a workspace move is recovered best-effort) — a value that resolves to nothing
    (or a missing file) counts as ``missing`` and is skipped. The worker receives
    absolute paths (the transient segments file carries no location dependence on disk).

    Returns ``(segs, missing)``.
    """
    segs = []
    missing = 0
    for m in manifest:
        if not m.get("ok"):
            continue
        raw = m.get("path") or ""
        try:
            p = pathio.resolve_path(raw, ws, strict=False) if (ws is not None and raw) else (Path(raw) if raw else None)
        except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
            p = None
        if p is None or not p.exists():
            missing += 1
            continue
        segs.append({
            "index": m.get("index"),
            "path": str(p),
            "speaker": m.get("speaker", ""),
            "pause_after": m.get("pause_after"),
            "text": m.get("text", ""),
        })
    # Re-order by index so the merged file always matches the original JSON order.
    segs.sort(key=lambda s: s["index"])
    return segs, missing


def _batch_count(n: int, size: int = MERGE_BATCH_SIZE) -> int:
    """How many part batches the worker will build for ``n`` segments (>= 1) — the
    same plan the worker's plan_merge_batches yields, for the up-front log line."""
    return max(1, -(-n // max(1, size)))


def _find_manifest(layout, package: str | None) -> Path:
    """The batch manifest to merge: the given package's ``manifest.json``, else the
    most recent package's, else the legacy top-level ``05_audio_chunk/manifest.json``."""
    d = layout.audio_chunk
    if package:
        return d / package / "manifest.json"
    if d.exists():
        cands = [p for p in d.glob("*/manifest.json") if p.is_file()]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    return d / "manifest.json"


def _output_name(manifest_path: Path, layout) -> str:
    """``<包>.mp3`` for a package manifest; ``cloned_audiobook.mp3`` for the legacy
    top-level manifest. Keeps one merged file per source book in ``06_audio_merge/``."""
    if manifest_path.parent == layout.audio_chunk:  # top-level (pre-package) manifest
        return "cloned_audiobook.mp3"
    stem = "".join("_" if c in _BAD_FILENAME_CHARS else c for c in manifest_path.parent.name).strip()
    return f"{stem or 'audiobook'}.mp3"


def run(handle, m4b: bool = False, package: str | None = None) -> dict:
    """Task worker: merge one package's batch output into the final audiobook file.

    ``package`` names a sub-folder under ``05_audio_chunk/`` (one per source JSON,
    written by the batch stage); when given, that package's manifest is used. When
    omitted, the most recent package is merged (falling back to the legacy
    top-level manifest for older projects).
    """
    layout = get_layout()
    ws = layout.workspace
    manifest_path = _find_manifest(layout, package)
    if not manifest_path.exists():
        raise RuntimeError("未找到合成结果清单（05_audio_chunk/<包>/manifest.json）——请先运行「音频合成」。")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"manifest.json 无法解析：{e}")
    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("manifest.json 为空——请先运行「音频合成」。")

    # Lazy migration of legacy absolute path values (and the in-memory copy follows
    # the rewrite), so the merge works on a project whose workspace has moved.
    _n, migrated = pathio.migrate_entries_in(manifest_path, ws, "list", ("path",))
    manifest = migrated if migrated is not None else manifest

    if m4b:
        handle.log("（M4B 输出将在后续阶段支持；本次生成 MP3。）", "WARNING")

    # Keep only segments that succeeded and whose file actually exists, in order
    # (each stored path resolved against the current workspace root — see collect_segments).
    segs, missing = collect_segments(manifest, ws)
    if not segs:
        raise RuntimeError("没有可合并的音频——音频合成未产生任何成功段落。")
    if missing:
        handle.log(f"警告：{missing} 段成功记录的文件缺失，将跳过。", "WARNING")

    cfg = get_config()
    t = cfg.tts
    pause_ms = t.pause_between_speakers_ms or 500
    same_ms = t.pause_same_speaker_ms or 250

    handle.log(f"开始 Merge：{len(segs)} 段 · 停顿 换人 {pause_ms}ms / 同人 {same_ms}ms")

    m = _batch_count(len(segs))
    if m > 1:
        handle.log(f"两阶段合并：{len(segs)} 段 → {m} 批（每批 {MERGE_BATCH_SIZE} 段）→ 整书")

    seg_file = layout.temp / f"merge_segments_{uuid.uuid4().hex[:12]}.json"
    seg_file.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    tmp_dir = layout.temp / f"merge_tmp_{uuid.uuid4().hex[:12]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = layout.audio_merge / _output_name(manifest_path, layout)

    python, worker = resolve_engine()
    cmd = [
        str(python), str(worker),
        "--mode", "merge",
        "--segments-file", str(seg_file),
        "--out", str(out_path),
        "--pause-ms", str(pause_ms),
        "--same-same-ms", str(same_ms),
        "--tmp-dir", str(tmp_dir),
        "--merge-batch-size", str(MERGE_BATCH_SIZE),
    ]
    if ws:
        # The workspace root, so a (transient) relative segment path resolves correctly
        # inside the worker too (its own cwd is the project root, not the workspace).
        cmd += ["--workspace", str(ws)]
    if cfg.ffmpeg.ffmpeg_path:
        cmd += ["--ffmpeg", cfg.ffmpeg.ffmpeg_path]

    handle.progress(0.05, "启动引擎")

    result_path = ""

    def on_line(line: str) -> None:
        nonlocal result_path
        if line.startswith("[result]"):
            result_path = line[len("[result]"):].strip()
        else:
            handle.log(line)

    try:
        run_worker(cmd, handle, on_line, temp_files=(seg_file,), fail_prefix="Merge 引擎")
    finally:
        # On an MP3-encode failure the worker keeps the whole-book WAV inside the
        # staging dir and reports it via [result] — relocate it before the dir goes.
        if result_path:
            kept = Path(result_path)
            if kept.is_file() and str(kept).startswith(str(tmp_dir) + os.sep):
                target = layout.audio_merge / kept.name
                kept.replace(target)
                result_path = str(target)
        # The backend owns the staging dir's cleanup on every exit path
        # (success / failure / cancel-kill of the child).
        shutil.rmtree(tmp_dir, ignore_errors=True)

    produced = Path(result_path or out_path)
    if not produced.exists():
        raise RuntimeError(f"引擎报告成功，但未找到输出文件：{produced}")
    size = produced.stat().st_size

    handle.log(f"Merge 完成 → {produced}（{size / 1024 / 1024:.1f} MB）")
    handle.progress(1.0, "完成")
    return {
        "file": produced.name,
        "path": str(produced),
        "segments": len(segs),
        "size": size,
    }
