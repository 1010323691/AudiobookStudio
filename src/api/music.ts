// Music library API (全局音乐库；后端 /api/music)。
// 瘦客户端：只做 HTTP 封装，无处理逻辑。

import { API_BASE, ApiError, http } from './client'
import type {
  MusicLibrary,
  MusicTrack,
  MusicDeleteResult,
  MusicTagCategory,
  TrackTags,
  SuggestTagsResult,
  SuggestBatchResult,
  ApplySuggestionsResult,
} from '@/types'

/** The full index (tag registry + all tracks). */
export function getLibrary(): Promise<MusicLibrary> {
  return http.get<MusicLibrary>('/api/music/library')
}

/** Preview (audio) URL for a library track — the library is NOT served via the
 *  workspace files API. */
export function musicPreviewUrl(name: string): string {
  return `${API_BASE}/api/music/preview/${encodeURIComponent(name)}`
}

/** Upload one music file (mp3/wav). Same name -> ApiError 409 (never overwrites). */
export async function uploadMusic(file: File): Promise<{ name: string; track: MusicTrack }> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(API_BASE + '/api/music/upload', { method: 'POST', body: form })
  const text = await res.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  if (!res.ok) {
    const detail = (body as any)?.detail ?? (body as any)?.error ?? res.statusText
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as { name: string; track: MusicTrack }
}

/** Update a track's tags / enabled / description (partial). */
export function updateTrack(
  name: string,
  patch: { tags?: TrackTags; enabled?: boolean; description?: string },
): Promise<{ name: string; track: MusicTrack }> {
  return http.put<{ name: string; track: MusicTrack }>(
    `/api/music/tracks/${encodeURIComponent(name)}`,
    patch,
  )
}

/** Delete one track. Locked chapter references skip it (see `skipped`). */
export function deleteTrack(name: string): Promise<MusicDeleteResult> {
  return http.del<MusicDeleteResult>(`/api/music/tracks/${encodeURIComponent(name)}`)
}

/** Batch enable/disable. */
export function batchEnable(names: string[], enabled: boolean): Promise<{ updated: number; missing: string[] }> {
  return http.post('/api/music/tracks/batch-enable', { names, enabled })
}

/** Batch add/remove tag names on the SELECTED tracks. */
export function batchTags(
  tracks: string[],
  tagNames: string[],
  category: MusicTagCategory,
  op: 'add' | 'remove',
): Promise<{ updated: number; missing: string[] }> {
  return http.post('/api/music/tracks/batch-tags', { tracks, names: tagNames, category, op })
}

/** Batch delete (same locked-reference skip semantics as single delete). */
export function batchDelete(names: string[]): Promise<MusicDeleteResult> {
  return http.post<MusicDeleteResult>('/api/music/tracks/batch-delete', { names })
}

/** Add a tag to the registry (global-uniqueness 409 on clash). */
export function createTag(category: MusicTagCategory, name: string): Promise<{ tags: Record<MusicTagCategory, string[]> }> {
  return http.post('/api/music/tags', { category, name })
}

/** Rename a registry tag (propagates to all tracks + the analysis cache). */
export function renameTag(
  category: MusicTagCategory,
  name: string,
  newName: string,
): Promise<{ tags: Record<MusicTagCategory, string[]>; affected_tracks: number }> {
  return http.post('/api/music/tags/rename', { category, name, new_name: newName })
}

/** Remove a tag from registry + all tracks (never deletes music files). */
export function deleteTag(category: MusicTagCategory, name: string): Promise<{ tags: Record<MusicTagCategory, string[]>; deleted_track_refs: number }> {
  return http.del(`/api/music/tags/${encodeURIComponent(category)}/${encodeURIComponent(name)}`)
}

/** AI-recommended tags (filename + description + vocabulary; the LLM never
 *  reads the audio). Results are in-vocabulary candidates for user confirmation. */
export function suggestTags(name: string, description?: string): Promise<SuggestTagsResult> {
  return http.post<SuggestTagsResult>('/api/music/suggest-tags', { name, description })
}

/** One-click batch AI recognition: one Task per selected track (shared LLM
 *  gate, per-track progress/cancel). Candidates land in the suggestions
 *  cache — nothing is applied to track tags until the user confirms. */
export function suggestTagsBatch(names: string[]): Promise<SuggestBatchResult> {
  return http.post<SuggestBatchResult>('/api/music/suggest-tags-batch', { names })
}

/** Adopt cached AI candidates (the「AI 推荐采用」confirmation): only tracks
 *  with a candidate AND no manual tags are touched — a user decision is never
 *  overwritten; applied tracks consume their candidate. */
export function applySuggestions(names: string[]): Promise<ApplySuggestionsResult> {
  return http.post<ApplySuggestionsResult>('/api/music/tracks/apply-suggestions', { names })
}
