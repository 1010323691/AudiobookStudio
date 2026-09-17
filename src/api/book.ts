import { http } from './client'
import type { BookAnalyzeResult, BookSplitResult } from '@/types'

/** Preview the split: chapters + per-chapter filenames (no files written). */
export function analyzeBook(path: string): Promise<BookAnalyzeResult> {
  return http.post<BookAnalyzeResult>('/api/book/analyze', { path })
}

/** Write one file per chapter to the workspace's 02_split_text/ (or a single
 *  `<base> 全书.txt` with wholeBook; optionally a STORE zip as well). */
export function splitBook(
  path: string,
  opts: { base?: string; asZip?: boolean; wholeBook?: boolean } = {},
): Promise<BookSplitResult> {
  return http.post<BookSplitResult>('/api/book/split', {
    path,
    base: opts.base ?? null,
    as_zip: !!opts.asZip,
    whole_book: !!opts.wholeBook,
  })
}
