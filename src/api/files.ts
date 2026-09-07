import { http } from './client'
import type { FileList, UploadResult } from '@/types'

const API_BASE: string = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8642'

/** List the output directory of a module (text | books | tts | audio). */
export function listModule(module: 'text' | 'books' | 'tts' | 'audio'): Promise<FileList> {
  return http.get<FileList>(`/api/files/list/${module}`)
}

/**
 * Browser fallback for file selection: upload a file into the backend's
 * ``input/`` directory and get its absolute path back. The Tauri desktop shell
 * uses the native dialog instead (see ``utils/tauri.ts``), so this is the path
 * used when running in a plain browser.
 */
export async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('filename', file.name)
  const res = await fetch(API_BASE + '/api/files/upload', { method: 'POST', body: form })
  const text = await res.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  if (!res.ok) {
    const detail = (body as any)?.detail ?? (body as any)?.error ?? res.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as UploadResult
}
