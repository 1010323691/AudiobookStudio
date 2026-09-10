import { defineStore } from 'pinia'
import { ref } from 'vue'
import { controlTask, listTasks, streamTask } from '@/api/tasks'
import type { TaskControl, TaskSnapshot, TaskStatus } from '@/types'

const ACTIVE: TaskStatus[] = ['pending', 'running', 'paused']

// Mirror of the backend's per-task LLM stream cap (core/tasks.py LLM_STREAM_CAP), so the
// live buffer never holds more than the authoritative snapshot replays on the next
// snapshot / terminal event (which replaces the whole task).
const LLM_STREAM_CLIENT_CAP = 128 * 1024

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<TaskSnapshot[]>([])
  const loading = ref(false)

  // One live SSE stream per in-flight task; keyed by task id.
  const streams = new Map<string, () => void>()

  function isActive(s: TaskStatus) {
    return ACTIVE.includes(s)
  }

  function upsert(t: TaskSnapshot) {
    const i = tasks.value.findIndex((x) => x.id === t.id)
    if (i === -1) tasks.value.unshift(t)
    else tasks.value[i] = t
  }

  function applyEvent(id: string, e: { type: string; [k: string]: any }) {
    const t = tasks.value.find((x) => x.id === id)
    if (!t) {
      // A late snapshot/final carries the full task — add it if unknown.
      if ((e.type === 'snapshot' || e.type === 'final') && e.task) upsert(e.task)
      return
    }
    switch (e.type) {
      case 'snapshot':
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        break
      case 'progress':
        t.progress = e.progress
        if (e.current) t.current = e.current
        break
      case 'log':
        // Append chronologically (oldest first) so the UI shows the newest line at the
        // bottom; the snapshot replay uses the same order. Trim the OLDEST lines first.
        t.logs.push({ level: e.level, msg: e.msg, t: e.t })
        if (t.logs.length > 1000) t.logs.splice(0, t.logs.length - 1000)
        break
      case 'llm_chunk': {
        // Append a slice of the raw LLM stream (「流式反馈」 panel). Display-only: the
        // snapshot / terminal events replace the whole task with the authoritative,
        // backend-capped buffer, so this live append self-corrects on the next snapshot.
        const buf = (t.llm_stream ?? '') + String(e.data ?? '')
        t.llm_stream = buf.length > LLM_STREAM_CLIENT_CAP ? buf.slice(-LLM_STREAM_CLIENT_CAP) : buf
        break
      }
      case 'llm_rate':
        // Live LLM generation rate (chars/s, the 吞吐量 / per-window gauge); the backend
        // computes it from the actual streamed text. A later snapshot / terminal event
        // replaces the whole task (which also carries llm_cps), so this self-corrects.
        t.llm_cps = typeof e.cps === 'number' ? e.cps : 0
        break
      case 'llm_chars':
        // Cumulative original-text chars (处理速度 numerator) + cumulative processing
        // seconds up to this chunk's completion (denominator). Both step together per
        // chunk, so the gauge updates per segment and stays stable in between (the time
        // base is frozen at each chunk's end, not a live clock).
        t.llm_chars = typeof e.chars === 'number' ? e.chars : 0
        t.llm_secs = typeof e.secs === 'number' ? e.secs : 0
        break
      case 'status':
        // A terminal status arrives with the full snapshot (result / error); apply it
        // atomically so the completion handler never reads a stale, empty result.
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        else t.status = e.status
        break
      case 'final':
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        break
    }
  }

  function startStream(id: string) {
    if (streams.has(id)) return
    const stop = streamTask(
      id,
      (e) => applyEvent(id, e),
      () => {
        streams.delete(id)
        refresh()
      },
    )
    streams.set(id, stop)
  }

  async function refresh() {
    loading.value = true
    try {
      tasks.value = await listTasks()
      // Keep a live stream on every task that hasn't reached a terminal state.
      tasks.value.forEach((t) => {
        if (isActive(t.status)) startStream(t.id)
      })
    } finally {
      loading.value = false
    }
  }

  /** Issue a control; the SSE stream confirms the resulting state. */
  async function control(id: string, action: TaskControl): Promise<void> {
    await controlTask(id, action)
    if (action === 'retry') startStream(id)
  }

  function stopAll() {
    streams.forEach((s) => s())
    streams.clear()
  }

  refresh()

  return { tasks, loading, refresh, startStream, control, stopAll }
})
