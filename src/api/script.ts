import { http } from './client'
import type { GenerateFilesResult } from '@/types'

/**
 * Start one independent parse Task per selected file. ``names`` are bare file names in
 * ``02_split_text/``; the backend reads them, runs the LLM → JSON pipeline per file
 * (concurrency-bounded by ``generation.max_concurrency``), and returns the created task
 * ids to stream via the task store. Each file gets its own status / success / output.
 * The pipeline includes the in-parse check stages (断句失败校验 / 纯归属标签条删除 /
 * 归属抽样), each gated by its own project-level toggle.
 */
export function generateScriptFiles(names: string[]): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/generate-files', { files: names })
}
