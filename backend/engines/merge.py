"""Merge engine — combine per-segment audio into the final audiobook.

Reads the manifest written by the batch stage, hands the ordered per-segment files
(speakers + per-segment pause overrides) to the worker's ``merge`` mode — which runs
the ported ``compute_timeline`` / ``combine_audio_with_pauses`` in the isolated env
(it has pydub / numpy / soundfile; the lean 3.14 backend does not) — and reports the
final ``cloned_audiobook.mp3``. Order is preserved, missing files are skipped with a
clear warning, and a merge failure (no audio / engine error) marks the task FAILED.

``run`` is a Task worker (first arg is a :class:`TaskHandle`); it streams progress /
log over SSE and honours cooperative cancel (killing the child).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..core.config import get_config
from ..core.paths import get_layout
from .tts import resolve_engine, run_worker

IMPLEMENTED = True


def run(handle, m4b: bool = False) -> dict:
    """Task worker: merge the batch output into the final audiobook file."""
    layout = get_layout()
    manifest_path = layout.audio_chunk / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("未找到合成结果清单（05_audio_chunk/manifest.json）——请先运行「音频合成」。")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"manifest.json 无法解析：{e}")
    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("manifest.json 为空——请先运行「音频合成」。")

    if m4b:
        handle.log("（M4B 输出将在后续阶段支持；本次生成 MP3。）", "WARNING")

    # Keep only segments that succeeded and whose file actually exists, in order.
    segs = []
    missing = 0
    for m in manifest:
        if not m.get("ok"):
            continue
        p = m.get("path") or ""
        if not p or not os.path.exists(p):
            missing += 1
            continue
        segs.append({
            "index": m.get("index"),
            "path": p,
            "speaker": m.get("speaker", ""),
            "pause_after": m.get("pause_after"),
            "text": m.get("text", ""),
        })
    if not segs:
        raise RuntimeError("没有可合并的音频——音频合成未产生任何成功段落。")
    if missing:
        handle.log(f"警告：{missing} 段成功记录的文件缺失，将跳过。", "WARNING")

    # Re-order by index so the merged file always matches the original JSON order.
    segs.sort(key=lambda s: s["index"])

    cfg = get_config()
    t = cfg.tts
    pause_ms = t.pause_between_speakers_ms or 500
    same_ms = t.pause_same_speaker_ms or 250

    handle.log(f"开始 Merge：{len(segs)} 段 · 停顿 换人 {pause_ms}ms / 同人 {same_ms}ms")

    seg_file = layout.temp / f"merge_segments_{uuid.uuid4().hex[:12]}.json"
    seg_file.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out_path = layout.audio_merge / "cloned_audiobook.mp3"

    python, worker = resolve_engine()
    cmd = [
        str(python), str(worker),
        "--mode", "merge",
        "--segments-file", str(seg_file),
        "--out", str(out_path),
        "--pause-ms", str(pause_ms),
        "--same-same-ms", str(same_ms),
    ]
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

    run_worker(cmd, handle, on_line, temp_files=(seg_file,), fail_prefix="Merge 引擎")

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
