"""TTS synthesis engine — local Qwen3-TTS (subprocess orchestrator).

The heavy ML stack (torch + qwen-tts) lives in an *isolated* virtualenv
(``.venv-tts``, Python 3.10) and runs as a one-shot subprocess; the app's own
3.14 backend never imports torch. That keeps the ML dependencies out of the app
package (the 3.14 test suite is unaffected) and mirrors how the reference project
runs. The engine core is ``tts-engine/tts_worker.py`` (built by
``install_tts_env.ps1``).

``synthesize`` is a Task worker (first arg is a :class:`TaskHandle`), mirroring
``backend/api/audio.py``: it streams the child's stdout into the task's progress
and log line-by-line and honours cooperative cancel by killing the child.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from ..core.config import get_config
from ..core.paths import PROJECT_ROOT, get_layout

IMPLEMENTED = True
NOT_READY_MSG = "TTS 引擎未就绪：请先运行 install_tts_env.ps1 安装独立的 .venv-tts 环境。"

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_SPEAKER = "serena"
DEFAULT_LANGUAGE = "chinese"


def _venv_python() -> Path:
    """Interpreter of the isolated TTS env (Windows ``Scripts`` / posix ``bin``)."""
    if os.name == "nt":
        return PROJECT_ROOT / ".venv-tts" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv-tts" / "bin" / "python"


def resolve_engine() -> tuple[Path, Path]:
    """Return ``(python_exe, worker_script)``. Env overrides (``AUDIOTTS_PYTHON`` /
    ``AUDIOTTS_WORKER``) let tests point at a stub. Raises an actionable error if the
    engine is missing."""
    python = Path(os.environ.get("AUDIOTTS_PYTHON") or _venv_python())
    worker = Path(os.environ.get("AUDIOTTS_WORKER") or (PROJECT_ROOT / "tts-engine" / "tts_worker.py"))
    if not python.exists():
        raise RuntimeError("未找到 TTS 引擎解释器（.venv-tts）。请先运行 install_tts_env.ps1 安装独立的 TTS 环境。")
    if not worker.exists():
        raise RuntimeError("未找到 TTS 引擎脚本 tts-engine/tts_worker.py。")
    return python, worker


def synthesize(handle, text: str, out_path, speaker: str = "", language: str = "",
              instruct: str = "") -> dict:
    """Synthesize ``text`` to an mp3 under the TTS output dir via the isolated env.

    Task worker contract: first arg is the :class:`TaskHandle`. Returns
    ``{"file", "path"}`` on success; raises on failure (the Task records it as
    failed and the error stays isolated from the rest of the console).
    """
    if not (text or "").strip():
        raise RuntimeError("输入文本为空。")

    python, worker = resolve_engine()
    cfg = get_config()
    t = cfg.tts

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Pass the text via a UTF-8 file: robust for long / non-ASCII text and free of
    # Windows command-line encoding + length limits.
    text_file = get_layout().temp / f"tts_{uuid.uuid4().hex[:12]}.txt"
    text_file.write_text(text, encoding="utf-8")

    cmd = [
        str(python), str(worker),
        "--text-file", str(text_file),
        "--out", str(out_path),
        "--speaker", speaker or t.speaker or DEFAULT_SPEAKER,
        "--language", language or t.language or DEFAULT_LANGUAGE,
        "--device", t.device or "auto",
    ]
    if instruct:
        cmd += ["--instruct", instruct]
    if t.model:
        cmd += ["--model", t.model]
    if cfg.ffmpeg.ffmpeg_path:
        cmd += ["--ffmpeg", cfg.ffmpeg.ffmpeg_path]

    handle.log(f"引擎：.venv-tts · 模型：{t.model or DEFAULT_MODEL}")
    handle.progress(0.02, "启动引擎")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(PROJECT_ROOT))
    except FileNotFoundError:
        raise RuntimeError(f"无法启动 TTS 引擎：{python}")

    out_q: "queue.Queue" = queue.Queue()
    err_q: "queue.Queue" = queue.Queue()
    stderr_tail: deque = deque(maxlen=40)

    def _pump(stream, q: "queue.Queue") -> None:
        try:
            for raw in iter(stream.readline, b""):
                q.put(raw)
        except Exception:
            pass
        finally:
            q.put(None)  # EOF sentinel — guarantees the reader loop terminates

    threading.Thread(target=_pump, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err_q), daemon=True).start()

    result_path = ""
    out_done = err_done = False
    try:
        while True:
            handle.check()  # cooperative cancel (child killed in finally) / pause
            try:
                while True:
                    raw = out_q.get_nowait()
                    if raw is None:
                        out_done = True
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if line.startswith("[result]"):
                        result_path = line[len("[result]"):].strip()
                    elif line.startswith("[progress]"):
                        parts = line.split(None, 2)
                        try:
                            frac = float(parts[1])
                        except (ValueError, IndexError):
                            frac = 0.0
                        handle.progress(frac, parts[2] if len(parts) > 2 else "")
                    else:
                        handle.log(line)
            except queue.Empty:
                pass
            try:
                while True:
                    raw = err_q.get_nowait()
                    if raw is None:
                        err_done = True
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        stderr_tail.append(line)
                        handle.log(line, "WARNING")
            except queue.Empty:
                pass
            if proc.poll() is not None and out_done and err_done:
                break
            time.sleep(0.15)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for p in (proc.stdout, proc.stderr):
            try:
                p.close()
            except Exception:
                pass
        try:
            text_file.unlink(missing_ok=True)
        except Exception:
            pass

    if proc.returncode != 0:
        tail = " | ".join(stderr_tail)[-500:]
        raise RuntimeError(
            f"TTS 引擎失败（退出码 {proc.returncode}）"
            + (f"：{tail}" if tail else "（无错误输出）")
        )

    # The authoritative produced file is the worker's [result] line (mp3, or the wav
    # fallback if MP3 encoding was unavailable); fall back to the intended path.
    produced = Path(result_path or out_path)
    if not produced.exists():
        raise RuntimeError(f"引擎报告成功，但未找到输出文件：{produced}")

    handle.progress(1.0, "完成")
    return {"file": produced.name, "path": str(produced)}
