import { http } from './client'
import type { WorkspaceInfo } from '@/types'

/** Current workspace + its artifact directories (empty path/dirs when unset). */
export function getWorkspace(): Promise<WorkspaceInfo> {
  return http.get<WorkspaceInfo>('/api/workspace')
}

/**
 * Set the pipeline's workspace folder (creating its subdirectories); an empty
 * path clears it, which re-locks the pipeline. Existing files are never moved
 * or deleted.
 */
export function setWorkspace(path: string): Promise<WorkspaceInfo> {
  return http.put<WorkspaceInfo>('/api/workspace', { path })
}
