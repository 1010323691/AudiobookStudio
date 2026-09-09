"""TTS engine base — local Qwen3-TTS (isolated-env helpers + shared orchestrator).

The heavy ML stack (torch + qwen-tts) lives in an *isolated* virtualenv
(``.venv-tts``, Python 3.10) and runs as a one-shot subprocess; the app's own
3.14 backend never imports torch. That keeps the ML dependencies out of the app
package (the 3.14 test suite is unaffected) and mirrors how the reference project
runs. The engine core is ``tts-engine/tts_worker.py`` (built by
``install_tts_env.ps1``).

This module is the shared base of the TTS-family stages (``tts_batch`` /
``merge``): interpreter + child-env resolution, and :func:`run_worker` — the
one-shot subprocess orchestration every stage's Task worker is built on (pump
threads, progress/log streaming, cooperative cancel/pause, child + temp cleanup).
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from ..core.paths import PROJECT_ROOT

IMPLEMENTED = True
NOT_READY_MSG = "TTS 引擎未就绪：请先运行 install_tts_env.ps1 安装独立的 .venv-tts 环境。"

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
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


def _child_env() -> dict:
    """Environment for the isolated TTS child (see :func:`resolve_engine`).

    Inherits the parent environment but forces UTF-8 stdio. A separate CPython
    child would otherwise default its stdout to the Windows ANSI codepage (GBK on
    zh-CN), which mangles any non-ASCII text it prints — CJK character names in
    ``[progress]`` lines and CJK output paths in ``[result]``/``[segment]`` lines —
    before the parent decodes it as UTF-8. Forcing UTF-8 (and, via UTF-8 mode, the
    filesystem encoding) also keeps the path the child *reports* identical to the
    file it actually *wrote*, so the parent's existence check finds it.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_worker(cmd: list, handle, on_line, *, temp_files=(), fail_prefix: str = "TTS 引擎") -> deque:
    """Run a one-shot ``.venv-tts`` worker and stream its output into a Task.

    Shared orchestration for the TTS-family stages (batch synthesis / merge):
    spawn the child (UTF-8 forced, project root as cwd), pump stdout/stderr from
    reader threads into the main loop, honour cooperative cancel/pause via
    ``handle.check()`` (the child is killed in ``finally``), mirror stderr into the
    task log at WARNING level, then clean up the child and any ``temp_files``.

    * ``[progress] <frac> <label>`` stdout lines are reported via
      ``handle.progress`` here; every other non-empty stdout line is passed to
      ``on_line`` (decoded, stripped) for stage-specific parsing.
    * A non-zero exit raises ``RuntimeError(f"{fail_prefix}失败（退出码 N）…")``.

    Returns the rolling stderr tail for post-run validation.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(PROJECT_ROOT), env=_child_env())
    except FileNotFoundError:
        raise RuntimeError(f"无法启动 TTS 引擎：{cmd[0]}")

    out_q: "queue.Queue" = queue.Queue()
    err_q: "queue.Queue" = queue.Queue()
    stderr_tail: deque = deque(maxlen=40)

    def _pump(stream, q: "queue.Queue") -> None:
        try:
            for raw in iter(stream.readline, b""):
                q.put(raw)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)  # EOF sentinel — guarantees the reader loop terminates

    threading.Thread(target=_pump, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err_q), daemon=True).start()

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
                    if line.startswith("[progress]"):
                        parts = line.split(None, 2)
                        try:
                            frac = float(parts[1])
                        except (ValueError, IndexError):
                            frac = 0.0
                        handle.progress(frac, parts[2] if len(parts) > 2 else "")
                    else:
                        on_line(line)
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
            except Exception:  # noqa: BLE001
                pass
        for f in temp_files:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    if proc.returncode != 0:
        tail = " | ".join(stderr_tail)[-500:]
        raise RuntimeError(
            f"{fail_prefix}失败（退出码 {proc.returncode}）"
            + (f"：{tail}" if tail else "（无错误输出）")
        )
    return stderr_tail
