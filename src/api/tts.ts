import { http } from './client'
import type { TTSStatus, TTSSynthesizeOptions } from '@/types'

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** Start a TTS synthesis Task; returns ``{ task_id }`` to stream via the task store. */
export function synthesize(text: string, opts: TTSSynthesizeOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/synthesize', {
    text,
    speaker: opts.speaker ?? '',
    language: opts.language ?? '',
    instruct: opts.instruct ?? '',
  })
}
