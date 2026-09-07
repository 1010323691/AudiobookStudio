import { http } from './client'
import type { BookAnalyzeResult, BookSplitResult } from '@/types'

/** Preview the split: chapters + volume plan + filenames (no files written). */
export function analyzeBook(path: string, targetChars?: number): Promise<BookAnalyzeResult> {
  return http.post<BookAnalyzeResult>('/api/book/analyze', {
    path,
    target_chars: targetChars ?? null,
  })
}

/** Write the volumes to output/books/ (optionally a STORE zip too). */
export function splitBook(
  path: string,
  opts: { targetChars?: number; base?: string; asZip?: boolean } = {},
): Promise<BookSplitResult> {
  return http.post<BookSplitResult>('/api/book/split', {
    path,
    target_chars: opts.targetChars ?? null,
    base: opts.base ?? null,
    as_zip: !!opts.asZip,
  })
}
