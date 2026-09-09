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
