import { http } from './client'
import type { AppConfig } from '@/types'

/** Read the full unified config. */
export function getConfig(): Promise<AppConfig> {
  return http.get<AppConfig>('/api/config')
}

/** Merge a (possibly partial) patch into the config and persist it. */
export function patchConfig(patch: Partial<AppConfig>): Promise<AppConfig> {
  return http.put<AppConfig>('/api/config', patch)
}
