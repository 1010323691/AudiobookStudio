import { http } from './client'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareVoicesOptions,
  BatchRunOptions,
} from '@/types'

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** 角色配音：start a voice-preparation Task (one-click all / new-only / a subset). */
export function prepareVoices(opts: PrepareVoicesOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/prepare-voices', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    overrides: opts.overrides ?? null,
    script: opts.script ?? null,
  })
}

/** 角色配音：detected characters + voice-config state + preview paths (for a given script). */
export function listVoices(script?: string): Promise<VoicesListResult> {
  const q = script ? `?script=${encodeURIComponent(script)}` : ''
  return http.get<VoicesListResult>(`/api/tts/voices${q}`)
}

/** 音频合成：start a batch TTS Task (all lines, or the given line indices; for a script). */
export function runBatch(opts: BatchRunOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
  })
}

/** 音频合并：start a merge Task (MP3 now; M4B is a later phase). */
export function runMerge(m4b = false): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/merge', { m4b })
}
