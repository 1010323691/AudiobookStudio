import { defineStore } from 'pinia'
import { ref } from 'vue'
import { controlTask, listTasks, streamTask } from '@/api/tasks'
import type { TaskControl, TaskSnapshot, TaskStatus } from '@/types'

const ACTIVE: TaskStatus[] = ['pending', 'running', 'paused']

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
