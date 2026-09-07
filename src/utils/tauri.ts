// Tauri bridge: native file dialog + open-in-OS. Falls back to browser
// upload / download links when not running inside the Tauri WebView.

import { uploadFile } from '@/api/files'

const API_BASE: string = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8642'

let _isTauri: boolean | null = null
/** True when running inside the Tauri desktop shell (WebView2 / WKWebView). */
export function isTauri(): boolean {
  if (_isTauri === null) {
    _isTauri = !!(window as any).__TAURI_INTERNALS__
  }
  return _isTauri
}

export interface PickedFile {
  path: string
  name: string
  size: number
}

export interface FileFilter {
  name: string
  /** Extensions without dots, e.g. ['txt'] / ['mp3','wav','m4a']. */
  extensions: string[]
}

/**
 * Pick an existing file. In Tauri: native dialog → absolute path (the backend
 * reads it directly). In a browser: hidden file input → upload into the backend's
 * ``input/`` dir → returned path. Either way the caller gets a path to send on.
 */
export async function pickFile(filters: FileFilter[] = []): Promise<PickedFile | null> {
  if (isTauri()) {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const path = (await open({
      multiple: false,
      filters: filters.map((f) => ({ name: f.name, extensions: f.extensions })),
    })) as string | null
    if (!path) return null
    return { path: String(path), name: path.split(/[\\/]/).pop() || String(path), size: 0 }
  }

  // Browser fallback: hidden <input type=file> + multipart upload.
  return new Promise<PickedFile | null>((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    if (filters.length) {
      input.accept = filters.flatMap((f) => f.extensions.map((e) => `.${e}`)).join(',')
    }
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) {
        resolve(null)
        return
      }
      try {
        const r = await uploadFile(file)
        resolve({ path: r.path, name: r.name, size: r.size })
      } catch (e) {
        console.error('上传失败', e)
        resolve(null)
      }
    }
    input.click()
  })
}

/** Open a file or folder with the OS (Tauri shell plugin). No-op in a browser. */
export async function openPath(target: string): Promise<void> {
  if (!isTauri()) return
  const { open } = await import('@tauri-apps/plugin-shell')
  await open(target)
}

/** URL to download a module output file (browser fallback; Tauri opens the folder). */
export function downloadUrl(module: string, name: string): string {
  return `${API_BASE}/api/files/download/${module}/${encodeURIComponent(name)}`
}

/** Directory that contains the given file (handles Windows and POSIX separators). */
function parentDir(p: string): string {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  return i > 0 ? p.slice(0, i) : p
}

/**
 * Best-effort "open in the OS" (desktop shell only). It never rejects, so it is safe to call
 * from a button handler or right after an action that already succeeded — a failure to reveal
 * a folder must not turn that successful action (e.g. an export) into a reported failure.
 */
export function reveal(target: string): void {
  if (!isTauri() || !target) return
  void openPath(target).catch(() => {})
}

/**
 * Hand a backend output file to the user, however each shell can actually deliver it:
 *  - Browser: navigate to the download URL; the backend replies
 *    `Content-Disposition: attachment`, so the browser saves it to the Downloads folder.
 *  - Tauri WebView: that navigation is a blocked cross-origin top-frame change, and WebViews
 *    have no "save as" handler, so instead reveal the file's folder in the OS — the file is
 *    already on this machine, which is the desktop equivalent of a download.
 */
export function downloadFile(module: string, path: string): void {
  if (isTauri()) {
    reveal(parentDir(path))
  } else {
    window.location.href = downloadUrl(module, path.split(/[\\/]/).pop() || path)
  }
}
