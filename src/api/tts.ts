import { http } from './client'
import type { TTSStatus } from '@/types'

/** Report whether TTS is implemented, so the UI shows an accurate badge. */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** Placeholder — the backend returns 501 until a provider is added. */
export function synthesize(path: string): Promise<unknown> {
  return http.post<unknown>('/api/tts/synthesize', { path })
}
