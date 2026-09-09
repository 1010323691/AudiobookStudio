import { API_BASE } from './client'
import type { UploadResult } from '@/types'

/**
 * File selection: upload a chosen file into the backend's ``01_input/`` directory
 * and get its absolute path back. This is the file-input path — the UI's hidden
 * ``<input type=file>`` hands the picked File here (see ``utils/fileops.ts``).
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
