import { http } from './client'
import type { GenerateFilesResult } from '@/types'

/**
 * Start one independent parse Task per selected file. ``names`` are bare file names in
 * ``02_split_text/``; the backend reads them, runs the LLM → JSON pipeline per file
 * (concurrency-bounded by ``generation.max_concurrency``), and returns the created task
 * ids to stream via the task store. Each file gets its own status / success / output.
 */
export function generateScriptFiles(names: string[]): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/generate-files', { files: names })
}

/**
 * Start one Speaker-check Task per selected parsed file. ``names`` are the base
 * ``<stem>.json`` file names in ``03_parsed_json/``; the backend re-judges each entry's
 * ``speaker`` with a ±N context window and writes ``<stem>_checked.json`` (the original is
 * left untouched). Same response shape as {@link generateScriptFiles}.
 */
export function checkScriptFiles(names: string[]): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/check-files', { files: names })
}
