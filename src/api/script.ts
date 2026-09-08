import { http } from './client'
import type { ScriptGenerateResult } from '@/types'

/** Start a script-generation (text-parse) Task; returns ``{ task_id }`` to stream via the task store. */
export function generateScript(text: string): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/script/generate', { text })
}

/** Re-read the last generated script (convenience after a page reload). */
export function getScriptResult(): Promise<ScriptGenerateResult> {
  return http.get<ScriptGenerateResult>('/api/script/result')
}
