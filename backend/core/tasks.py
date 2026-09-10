"""Unified asynchronous task system.

Long-running work (audio silence-detection / cutting, and future TTS) runs as a
:class:`Task` in a worker thread. Each task exposes status, progress, a live log
buffer and start / pause / resume / cancel / retry controls, and streams events
to the UI over SSE. A failing task is marked failed and isolated — it never takes
the console down (requirement #7).
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED}

# Max chars of raw LLM stream retained per task (the 「流式反馈」 panel's source of truth).
# Tasks are kept in memory for the life of the process, so this bounds how much a long
# parse can accumulate; the oldest chunks are dropped first. Lower it if you're memory
# sensitive, raise it for more reviewable history.
LLM_STREAM_CAP = 128 * 1024


class TaskCancelled(Exception):
    """Raised by engine code to abort a task cleanly."""


class TaskHandle:
    """Handed to engine code so it can report progress, log, and honour
    pause / cancel requests cooperatively."""

    def __init__(self, task: "Task"):
        self._t = task

    @property
    def cancelled(self) -> bool:
        return self._t.cancel_event.is_set()

    def progress(self, frac: float, current: str = "") -> None:
        self._t.set_progress(frac, current)

    def log(self, msg: str, level: str = "INFO") -> None:
        self._t.log(msg, level)

    def llm_chunk(self, text: str) -> None:
        """Forward a (coalesced) slice of the raw LLM stream to the 「流式反馈」 panel.

        Display-only: the engine calls this as the model streams so the UI can show the
        model's working state live. It is never read back by the parse/JSON pipeline.
        """
        self._t.log_llm(text)

    def llm_rate(self, cps: float) -> None:
        """Report the current LLM generation rate (chars/s) to the UI.

        The 文本解析 engine computes this from the actual streamed text (content +
        reasoning) as it arrives and reports it live; like ``llm_chunk`` it is
        display-only and feeds the per-window 吞吐量 / ``字/s`` gauge (never the
        parse/JSON pipeline).
        """
        self._t.set_llm_rate(cps)

    def llm_chars(self, chars: int, secs: float) -> None:
        """Report cumulative original-text chars processed + the processing time up to the
        most recently completed chunk (per chunk).

        The 文本解析 engine calls this after each text chunk is fully processed, so the
        page's 处理速度 gauge (Σ chars ÷ Σ processing-time) steps up on every chunk and
        stays *stable* in between — the time base is frozen at each chunk's completion
        (reported by the backend), not a live clock, so the value never decays while the
        next chunk is still generating. Display-only, like ``llm_rate`` — real chars,
        never tokens.
        """
        self._t.set_llm_chars(chars, secs)

    def check(self) -> None:
        """Cooperative cancellation + pause point. Call between units of work."""
        if self._t.cancel_event.is_set():
            raise TaskCancelled()
        while self._t.pause_event.is_set() and not self._t.cancel_event.is_set():
            time.sleep(0.1)
        if self._t.cancel_event.is_set():
            raise TaskCancelled()


@dataclass
class Task:
    id: str
    module: str
    label: str
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    current: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=1000))
    # Raw LLM stream (the 「流式反馈」 panel). A char-capped buffer of the coalesced
    # deltas, oldest dropped first when over ``LLM_STREAM_CAP`` — see ``log_llm``.
    # Kept on the Task so the on-connect snapshot / terminal events can replay it and
    # the panel survives a page reload (restored via ``GET /api/tasks``).
    llm_chunks: deque = field(default_factory=deque)
    llm_len: int = 0
    # Live LLM generation rate (chars/s) for the 文本解析 吞吐量 / per-window gauge.
    # Updated from the streamed text and replayed in the snapshot / terminal events, so
    # a reconnect or a finished window still carries its most recent rate.
    llm_cps: float = 0.0
    # Cumulative original-text chars processed so far — stepped up per completed chunk by
    # the 文本解析 engine (the 处理速度 gauge's numerator). Real chars, never tokens.
    # Kept on the Task so the snapshot / terminal events replay it (survives a reload).
    llm_chars: int = 0
    # Cumulative processing time (s) up to the last completed chunk — the 处理速度
    # gauge's denominator. Frozen at each chunk's completion (not a live clock), so the
    # gauge holds steady between chunks instead of decaying.
    llm_secs: float = 0.0
    result: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _func: Optional[Callable] = field(default=None, repr=False)
    _args: tuple = field(default=(), repr=False)
    _kwargs: dict = field(default_factory=dict, repr=False)
    _listeners: set = field(default_factory=set, repr=False)

    # -- state updates (notify SSE listeners) -------------------------------
    def set_progress(self, frac: float, current: str = "") -> None:
        self.progress = max(0.0, min(1.0, float(frac)))
        if current:
            self.current = current
        self._emit({"type": "progress", "progress": self.progress, "current": self.current})

    def log(self, msg: str, level: str = "INFO") -> None:
        # Append in chronological order (oldest → newest). The UI renders the newest
        # line at the bottom, and the snapshot replays this same order, so the live
        # stream and a reconnect's replayed log always agree on ordering.
        entry = {"level": level, "msg": msg, "t": time.time()}
        self.logs.append(entry)
        self._emit({"type": "log", **entry})

    def log_llm(self, text: str) -> None:
        """Append a (coalesced) slice of the raw LLM stream and forward it over SSE.

        The 文本解析 engine calls this as the model streams, so the 「流式反馈」 panel
        fills in real time. The text is also kept on the Task (char-capped, oldest
        dropped first) so the on-connect snapshot / terminal events can replay the whole
        stream — the panel survives a page reload and self-heals any dropped live event.
        """
        self.llm_chunks.append(text)
        self.llm_len += len(text)
        while self.llm_len > LLM_STREAM_CAP and self.llm_chunks:
            self.llm_len -= len(self.llm_chunks.popleft())
        self._emit({"type": "llm_chunk", "data": text})

    def set_llm_rate(self, cps: float) -> None:
        """Set the live LLM generation rate (chars/s) and forward it over SSE (gauge)."""
        self.llm_cps = max(0.0, float(cps))
        self._emit({"type": "llm_rate", "cps": self.llm_cps})

    def set_llm_chars(self, chars: int, secs: float) -> None:
        """Set cumulative original-text chars + processing time and forward over SSE.

        Both advance together (one event per completed chunk) so the client's
        处理速度 = Σ chars ÷ Σ secs is always internally consistent and stable between
        chunks (the time base is whatever the backend measured at that chunk's end).
        """
        self.llm_chars = max(0, int(chars))
        self.llm_secs = max(0.0, float(secs))
        self._emit({"type": "llm_chars", "chars": self.llm_chars, "secs": self.llm_secs})

    def _set_status(self, status: TaskStatus) -> None:
        self.status = status
        if status in TERMINAL:
            # A terminal status is the instant consumers act on "task done", so the
            # event also carries the full snapshot (result / error). Otherwise a client
            # reacting to the status change reads a stale, empty result — the result
            # would otherwise arrive only in the later ``final`` event, by which point
            # the view has already captured (and detached on) the empty one.
            self._emit({"type": "status", "status": status.value, "task": self.snapshot()})
        else:
            self._emit({"type": "status", "status": status.value})

    def _emit(self, event: dict) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # -- snapshot for the API ------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "module": self.module,
            "label": self.label,
            "status": self.status.value,
            "progress": self.progress,
            "current": self.current,
            "logs": list(self.logs),
            "llm_stream": "".join(self.llm_chunks),
            "llm_cps": self.llm_cps,
            "llm_chars": self.llm_chars,
            "llm_secs": self.llm_secs,
            "result": self.result,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }

    # -- SSE subscription ----------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        self._listeners.discard(q)


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, module: str, label: str, func: Callable, *args, **kwargs) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], module=module, label=label)
        task._func, task._args, task._kwargs = func, args, kwargs
        task.started = time.time()
        task._set_status(TaskStatus.RUNNING)
        with self._lock:
            self._tasks[task.id] = task
        t = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread = t
        t.start()
        return task

    def _run(self, task: Task) -> None:
        handle = TaskHandle(task)
        task.log("任务开始", "INFO")
        try:
            result = task._func(handle, *task._args, **task._kwargs) or {}
            task.result = result
            task.set_progress(1.0, "完成")
            task.log("任务完成", "INFO")
            task._set_status(TaskStatus.SUCCEEDED)
        except TaskCancelled:
            task.log("任务已取消", "WARNING")
            task._set_status(TaskStatus.CANCELLED)
        except Exception as exc:  # noqa: BLE001 — isolate any failure
            task.error = str(exc)
            task.log(f"任务失败：{exc}", "ERROR")
            task._set_status(TaskStatus.FAILED)
        finally:
            task.finished = time.time()
            task._emit({"type": "final", "task": task.snapshot()})

    # -- lookups -------------------------------------------------------------
    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)

    # -- controls ------------------------------------------------------------
    def control(self, task_id: str, action: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if action == "cancel":
            task.cancel_event.set()
            task.log("收到取消请求", "WARNING")
        elif action == "pause":
            if task.status == TaskStatus.RUNNING:
                task.pause_event.set()
                task._set_status(TaskStatus.PAUSED)
                task.log("任务已暂停", "INFO")
        elif action == "resume":
            if task.status == TaskStatus.PAUSED:
                task.pause_event.clear()
                task._set_status(TaskStatus.RUNNING)
                task.log("任务已恢复", "INFO")
        elif action == "retry":
            if task.status in TERMINAL:
                task.cancel_event.clear()
                task.pause_event.clear()
                task.error = ""
                task.progress = 0.0
                task.logs.clear()
                task.llm_chunks.clear()
                task.llm_len = 0
                task.llm_cps = 0.0
                task.llm_chars = 0
                task.llm_secs = 0.0
                task.result = {}
                task.started = time.time()
                task.finished = 0.0
                task._set_status(TaskStatus.RUNNING)
                t = threading.Thread(target=self._run, args=(task,), daemon=True)
                task._thread = t
                t.start()
        else:
            raise ValueError(f"unknown action {action!r}")
        return task


_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager
