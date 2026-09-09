import { API_BASE, http } from './client'
import type { TaskControl, TaskSnapshot } from '@/types'

/** List all tasks (newest first). */
export function listTasks(): Promise<TaskSnapshot[]> {
  return http.get<TaskSnapshot[]>('/api/tasks')
}

/** cancel / pause / resume / retry. */
export function controlTask(id: string, action: TaskControl): Promise<TaskSnapshot> {
  return http.post<TaskSnapshot>(`/api/tasks/${id}/${action}`)
}

/**
 * Subscribe to a task's live SSE stream (progress + logs + status). The backend
 * replays the current snapshot on connect, then forwards events until the task
 * reaches a terminal state. Returns an abort function.
 */
export function streamTask(
  id: string,
  onEvent: (e: { type: string; [k: string]: any }) => void,
  onDone?: () => void,
): () => void {
  const es = new EventSource(`${API_BASE}/api/tasks/${id}/stream`)

  // The backend emits every frame as a plain `data:` line (no `event:` field),
  // so the browser delivers them all as `message` events — dispatch on the JSON
  // `type` field rather than named event types.
  let finished = false
  const handler = (ev: MessageEvent) => {
    let e: { type: string; [k: string]: any }
    try {
      e = JSON.parse(ev.data)
    } catch {
      return
    }
    onEvent(e)
    if (e.type === 'final' && !finished) {
      finished = true
      teardown()
      onDone?.()
    }
  }

  function teardown() {
    es.close()
  }

  es.onmessage = handler
  // EventSource auto-reconnects on transient errors; only a fully-closed stream
  // ends the view, so a dropped connection doesn't silently stop the live feed.
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && !finished) {
      finished = true
      teardown()
      onDone?.()
    }
  }

  return () => {
    if (!finished) {
      finished = true
      teardown()
    }
  }
}
