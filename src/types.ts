// Shared backend types — mirror the FastAPI response/request shapes exactly.
// Keeping these in one place so the views, api modules and stores agree.

// ------------------------------ files ------------------------------
export interface FileItem {
  name: string
  is_dir: boolean
  size: number | null
}
export interface FileList {
  path: string
  items: FileItem[]
}
export interface UploadResult {
  path: string
  name: string
  size: number
}

// ------------------------------ text ------------------------------
export interface TextStats {
  chars: number
  paras: number
  chapters: number
}
export interface TextToggles {
  keep_single_space: boolean
  sentence_break: boolean
  dialogue_separate: boolean
  detect_chapters: boolean
  punct_ellipsis: boolean
  punct_repeated: boolean
  punct_lone_ascii: boolean
  punct_quotes: boolean
  punct_dash: boolean
  live: boolean
}
export interface TextFormatResult {
  source: string
  encoding: string
  output_path: string
  stats: TextStats
  preview: string
  full_length: number
}

// ------------------------------ book ------------------------------
export interface BookChapter {
  seq: number
  num: number | null
  numStr: string
  title: string
  chars: number
}
export interface BookVolume {
  index: number
  firstChapter: number
  lastChapter: number
  chars: number
}
export interface BookSequenceReport {
  count: number
  parseable: number
  unparseable: number
  first: number | null
  last: number | null
  gaps: { after: number; missing: number[] }[]
  duplicates: { seq: number; num: number }[]
  disorder: { seq: number; num: number; prevNum: number }[]
  hasIssues: boolean
}
export interface BookAnalyzeResult {
  source: string
  encoding: string
  base: string
  target_chars: number
  total_chars: number
  chapters: BookChapter[]
  chapter_count: number
  volume_count: number
  volumes: BookVolume[]
  filenames: string[]
  sequence: BookSequenceReport
  error: string | null
}
export interface BookSplitFile {
  name: string
  path: string
  chars: number
}
export interface BookSplitResult {
  output_dir: string
  file_count: number
  files: BookSplitFile[]
  zip_path?: string
}

// ------------------------------ audio ------------------------------
export interface AudioProbeResult {
  ok: boolean
  name: string
  path: string
  duration: number
  size: number
  ext: string
  mime: string
}
export interface AudioSegment {
  index: number
  start: number
  duration: number
}
export interface AudioPlanResult {
  duration: number
  count: number
  each: number
  target: number
  segments: AudioSegment[]
}
export interface AudioPause {
  start: number
  end: number
}
export interface AudioSilencesResult {
  duration: number
  pause_count: number
  pauses: AudioPause[]
  count: number
  segments: AudioSegment[]
  snapped: number
  fallbacks: number
  aligned: boolean
  shifts: number[]
}
export interface AudioCutFile {
  name: string
  path: string
  size: number
  duration: number
}
export interface AudioCutResult {
  output_dir: string
  file_count: number
  files: AudioCutFile[]
  duration: number
  smart_align: boolean
}

// ------------------------------ tts ------------------------------
export interface TTSStatus {
  implemented: boolean
  message: string
}

// ------------------------------ tasks ------------------------------
export type TaskStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'cancelled'
  | 'succeeded'
  | 'failed'

export interface TaskLog {
  level: string
  msg: string
  t: number
}
export interface TaskSnapshot {
  id: string
  module: string
  label: string
  status: TaskStatus
  progress: number
  current: string
  logs: TaskLog[]
  result: Record<string, any>
  error: string
  created: number
  started: number
  finished: number
}
export type TaskControl = 'cancel' | 'pause' | 'resume' | 'retry'

// ------------------------------ config ------------------------------
export interface AppConfig {
  paths: { working_dir: string }
  text: TextToggles
  book: { target_chars: number }
  audio: {
    target_duration: string
    naming_format: string
    start_number: string
    smart_align: boolean
    align_tolerance: number
  }
  tts: {
    enabled: boolean
    api_base: string
    api_key: string
    model: string
    voice: string
    concurrency: number
  }
  ffmpeg: { ffmpeg_path: string; ffprobe_path: string }
  log: { level: string }
  ui: { theme: string }
}
