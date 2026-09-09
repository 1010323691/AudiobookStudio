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

from ..core.config import get_config
from ..core.paths import get_layout
from .tts import DEFAULT_LANGUAGE, DEFAULT_MODEL, resolve_engine, run_worker

IMPLEMENTED = True


def _load_script():
    p = get_layout().parsed_json / "annotated_script.json"
    if not p.exists():
        raise RuntimeError("未找到 03_parsed_json/annotated_script.json——请先在「文本解析」生成脚本。")
    try:
        script = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"annotated_script.json 无法解析：{e}")
    if not isinstance(script, list) or not script:
        raise RuntimeError("annotated_script.json 为空——请先生成脚本。")
    return script


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


def synthesize(handle, indices=None) -> dict:
    """Task worker: synthesize all (or the selected) script lines, in JSON order."""
    script = _load_script()

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

    # Build the ordered segment list. ``index`` is the line's position in the full
    # script, so per-segment filenames and the merged order always match the JSON.
    segments = _build_segments(script, indices)
    if not segments:
        raise RuntimeError("没有可合成的段（脚本为空或筛选后无内容）。")

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    total = len(segments)
    handle.log(f"开始音频合成：{total} 段")
    speakers_in_batch = sorted({s["speaker"] for s in segments if s["speaker"]})
    if speakers_in_batch:
        handle.log(f"涉及角色：{'、'.join(speakers_in_batch)}")

    layout = get_layout()
    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    seg_file.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    out_dir = layout.audio_chunk
    out_dir.mkdir(parents=True, exist_ok=True)

    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts

    cmd = [
        str(python), str(worker),
        "--mode", "batch",
        "--segments-file", str(seg_file),
        "--voice-config", str(vc_path),
        "--out-dir", str(out_dir),
        "--language", t.language or DEFAULT_LANGUAGE,
        "--device", t.device or "auto",
    ]
    # Omit empty model ids so the worker falls back to its own (identical) defaults.
    if t.model:
        cmd += ["--model", t.model]
    if t.base_model:
        cmd += ["--base-model", t.base_model]
    if t.design_model:
        cmd += ["--design-model", t.design_model]
    if cfg.ffmpeg.ffmpeg_path:
        cmd += ["--ffmpeg", cfg.ffmpeg.ffmpeg_path]

    handle.log("引擎：.venv-tts（一次性子进程，模型只加载一次）")
    handle.progress(0.02, "启动引擎")

    seg_results: dict = {}  # index -> {ok, path, reason}
    by_index = {s["index"]: s for s in segments}

    def on_line(line: str) -> None:
        if line.startswith("[segment]"):
            _handle_segment(line, by_index, total, seg_results, handle)
        else:
            handle.log(line)

    run_worker(cmd, handle, on_line, temp_files=(seg_file,), fail_prefix="音频合成引擎")

    # Build the ordered manifest for the Merge stage (one entry per segment).
    manifest = []
    failed = []
    completed = 0
    for s in segments:
        index = s["index"]
        r = seg_results.get(index)
        if r and r.get("ok"):
            completed += 1
            manifest.append({"index": index, "speaker": s["speaker"], "text": s["text"],
                             "path": r["path"], "ok": True, "reason": "",
                             "pause_after": s["pause_after"]})
        else:
            reason = (r or {}).get("reason") or "（引擎未返回结果）"
            failed.append({"index": index, "speaker": s["speaker"], "reason": reason})
            manifest.append({"index": index, "speaker": s["speaker"], "text": s["text"],
                             "path": "", "ok": False, "reason": reason,
                             "pause_after": s["pause_after"]})

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    handle.log(f"音频合成结束：成功 {completed}，失败 {len(failed)}，共 {total} 段。清单：{manifest_path.name}")

    if completed == 0:
        first = failed[0]["reason"] if failed else "无"
        raise RuntimeError(f"全部 {total} 段合成失败。首个原因：{first}")

    handle.progress(1.0, "完成")
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "output_dir": str(out_dir),
        "manifest_path": str(manifest_path),
    }
