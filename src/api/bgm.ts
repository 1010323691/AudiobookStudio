// BGM API (背景音乐：章节气氛分析 / 匹配 / 手动干预 / 混音；后端 /api/bgm)。
// 瘦客户端：只做 HTTP 封装，无处理逻辑。

import { API_BASE, http } from './client'
import type {
  BgmChaptersResult,
  BgmMatchResult,
  BgmBatchResult,
  BgmAssignment,
  TrackTags,
} from '@/types'

/** All chapter rows (disk-state basis = 02_split_text stems). Read-only. */
export function getChapters(): Promise<BgmChaptersResult> {
  return http.get<BgmChaptersResult>('/api/bgm/chapters')
}

/** Download / preview URL of a finished mix (08_bgm is a workspace dir — the
 *  shared files API serves it). */
export function bgmFileUrl(stem: string): string {
  return `${API_BASE}/api/files/download/08_bgm/${encodeURIComponent(stem + '.mp3')}`
}

/** Start one LLM mood-analysis Task per selected chapter. */
export function analyzeChapters(chapters: string[]): Promise<BgmBatchResult> {
  return http.post<BgmBatchResult>('/api/bgm/analyze', { chapters })
}

/** (Re-)match the selected chapters (empty/omitted = all). Synchronous. */
export function matchChapters(chapters: string[] | null, mode: string): Promise<BgmMatchResult> {
  return http.post<BgmMatchResult>('/api/bgm/match', { chapters, mode })
}

/** Manual edit of one chapter: tags and/or music (null = clear) and/or lock.
 *  Omitted keys are untouched. */
export function updateChapter(
  stem: string,
  patch: { tags?: TrackTags; music?: string | null; locked?: boolean; __setMusic?: boolean },
): Promise<BgmAssignment> {
  const { __setMusic, ...rest } = patch
  const body: Record<string, unknown> = {}
  if (rest.tags !== undefined) body.tags = rest.tags
  if (__setMusic) body.music = rest.music ?? null
  if (rest.locked !== undefined) body.locked = rest.locked
  return http.put<BgmAssignment>(`/api/bgm/chapters/${encodeURIComponent(stem)}`, body)
}

/** Start one mix Task per selected chapter (the no-BGM copy2 path is allowed). */
export function mixChapters(chapters: string[]): Promise<BgmBatchResult> {
  return http.post<BgmBatchResult>('/api/bgm/mix', { chapters })
}
