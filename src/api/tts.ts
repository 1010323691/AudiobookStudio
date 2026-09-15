import { http } from './client'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareFoundationsOptions,
  MakeClonesOptions,
  BatchRunOptions,
  BatchStatusFiles,
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
    candidate_count: opts.candidate_count ?? null,
  })
}

/** 角色配音：detected characters + voice-config state + preview paths (for a given script). */
export function listVoices(script?: string): Promise<VoicesListResult> {
  const q = script ? `?script=${encodeURIComponent(script)}` : ''
  return http.get<VoicesListResult>(`/api/tts/voices${q}`)
}

/** 角色配音：记录用户对某角色克隆候选的选择（单选一个为最终音色；audioId 为 null =
 *  清除选择，回默认第一条）。同步写（非任务）：选中的候选成为生效的克隆参考。 */
export function selectVoice(speaker: string, audioId: string | null): Promise<{
  ok: boolean
  speaker: string
  selected_audio_id: string | null
  ref_audio: string
}> {
  return http.put<{ ok: boolean; speaker: string; selected_audio_id: string | null; ref_audio: string }>(
    '/api/tts/voices/select', { speaker, audio_id: audioId },
  )
}

/** 音频合成：start a batch TTS Task (all lines, or the given line indices; for a script —
 *  or a whole selection of scripts, the 待合成 card's multi-select: one task synthesizes
 *  the files one by one, each in its own package). ``force_all`` re-synthesizes every
 *  segment; otherwise the run resumes (skips the done). ``concurrency`` is the *manual
 *  per-batch cap* (批内段数上限); ``seed`` (>=0) makes a run reproducible (omitted → the
 *  persisted config default; -1 → random). */
export function runBatch(opts: BatchRunOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
    scripts: opts.scripts ?? null,
    concurrency: opts.concurrency ?? null,
    seed: opts.seed ?? null,
    force_all: opts.force_all ?? false,
  })
}

/** 音频合成进度（每文件）：each file's 【已合成 / 总段落】· 角色 · 已就绪声音, plus the
 *  【已合成】 flag when a file's segments are all done. Reads the package manifests (written
 *  incrementally as synthesis proceeds), so polling while a run streams gives live, real
 *  per-row counts. FastAPI's ``list[str]`` query param = one repeated ``scripts=`` per file. */
export function batchStatusFiles(scripts: string[]): Promise<BatchStatusFiles> {
  const q = new URLSearchParams()
  for (const s of scripts) q.append('scripts', s)
  return http.get<BatchStatusFiles>(`/api/tts/batch-status?${q.toString()}`)
}

/** 音频合并：start a merge Task for one package (MP3 now; M4B is a later phase).
 *  ``pkg`` names the sub-folder in 05_audio_chunk/ to merge; omitted → most recent. */
export function runMerge(m4b = false, pkg?: string): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/merge', { m4b, package: pkg ?? null })
}
