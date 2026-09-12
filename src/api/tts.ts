import { http } from './client'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareFoundationsOptions,
  MakeClonesOptions,
  BatchRunOptions,
  BatchStatus,
} from '@/types'

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** 角色配音 · 阶段 1（LLM only）：start the voice-foundation Task (all / new-only / a subset). */
export function prepareFoundations(opts: PrepareFoundationsOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/prepare-foundations', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    overrides: opts.overrides ?? null,
    script: opts.script ?? null,
  })
}

/** 角色配音 · 阶段 2（TTS only）：start the clone-seed Task (all / new-only / a subset; N parallel). */
export function makeClones(opts: MakeClonesOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/make-clones', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    concurrency: opts.concurrency ?? null,
    script: opts.script ?? null,
  })
}

/** 角色配音：detected characters + voice-config state + preview paths (for a given script). */
export function listVoices(script?: string): Promise<VoicesListResult> {
  const q = script ? `?script=${encodeURIComponent(script)}` : ''
  return http.get<VoicesListResult>(`/api/tts/voices${q}`)
}

/** 音频合成：start a batch TTS Task (all lines, or the given line indices; for a script).
 *  ``force_all`` re-synthesizes every segment; otherwise the run resumes (skips the done). */
export function runBatch(opts: BatchRunOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
    concurrency: opts.concurrency ?? null,
    force_all: opts.force_all ?? false,
  })
}

/** 音频合成进度：the chosen script's 【已合成 / 总段落】(completed / total / remaining).
 *  Reads the package manifest (written incrementally as synthesis proceeds), so polling it
 *  while a run streams gives a live, real count. */
export function batchStatus(script?: string): Promise<BatchStatus> {
  const q = script ? `?script=${encodeURIComponent(script)}` : ''
  return http.get<BatchStatus>(`/api/tts/batch-status${q}`)
}

/** 音频合并：start a merge Task for one package (MP3 now; M4B is a later phase).
 *  ``pkg`` names the sub-folder in 05_audio_chunk/ to merge; omitted → most recent. */
export function runMerge(m4b = false, pkg?: string): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/merge', { m4b, package: pkg ?? null })
}
