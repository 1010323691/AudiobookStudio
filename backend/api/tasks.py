"""Unified task-system endpoints (requirement #2): list / detail / control / SSE.

The SSE stream (``GET /api/tasks/{id}/stream``) is how the task centre gets live
progress + logs. It replays the current snapshot on connect, then forwards each
event until the task reaches a terminal state.
"""
from __future__ import annotations

import json
import queue as _queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..core.tasks import TERMINAL, get_task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("")
def list_tasks() -> list[dict]:
    return [t.snapshot() for t in get_task_manager().list()]


@router.get("/{task_id}")
def get_task(task_id: str) -> dict:
    task = get_task_manager().get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.snapshot()


@router.post("/{task_id}/{action}")
def control_task(task_id: str, action: str) -> dict:
    """action ∈ {cancel, pause, resume, retry}."""
    try:
        task = get_task_manager().control(task_id, action)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return task.snapshot()


@router.get("/{task_id}/stream")
def stream_task(task_id: str):
    task = get_task_manager().get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    def gen():
        q = task.subscribe()
        try:
            # Replay state so a late subscriber starts with the full picture.
            yield _sse({"type": "snapshot", "task": task.snapshot()})
            if task.status in TERMINAL:
                return
            while True:
                try:
                    event = q.get(timeout=15)
                except _queue.Empty:
                    yield _sse({"type": "ping"})  # keep-alive
                    continue
                yield _sse(event)
                if event.get("type") == "final":
                    break
        finally:
            task.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
