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
 * Start one 角色匹配检查 (Speaker-check) Task per selected parsed file. ``names`` are the
 * base ``<stem>.json`` file names in ``03_parsed_json/``; the backend re-judges each
 * entry's ``speaker`` with a ±N context window. Input is the ``_checked`` artifact when it
 * is the mix check's fresh output, else the base; the result updates ``<stem>_checked.json``
 * in place (only ``speaker`` changes; the base file is left untouched). Same response shape
 * as {@link generateScriptFiles}.
 */
export function checkScriptFiles(names: string[]): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/check-files', { files: names })
}

/**
 * Start one 段落混合检查 (mix-check) Task per selected parsed file. ``names`` are the base
 * ``<stem>.json`` file names in ``03_parsed_json/``; the backend splits multi-subject
 * entries (narration + dialogue, or lines of 2+ characters) into separate entries in
 * original order and deletes punctuation-only entries (deterministically, no LLM), then
 * writes ``<stem>_checked.json`` (the base file is left untouched). Shares the
 * 角色匹配检查 batch geometry. Same response shape as {@link generateScriptFiles}.
 */
export function mixScriptFiles(names: string[]): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/mix-files', { files: names })
}
