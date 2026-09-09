// Shared backend types — mirror the FastAPI response/request shapes exactly.
// Keeping these in one place so the views, api modules and stores agree.

// ------------------------------ files ------------------------------
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

// ------------------------------ tts: 角色配音 / 音频合成 / 音频合并 ------------------------------
export interface VoiceItem {
  name: string
  line_count: number
  status: 'ready' | 'pending'
  type: string // clone | design | custom | ''
  alias_of: string // non-empty -> this label points at another character's voice
  description: string
  preview: string // path relative to 04_voice_profiles/ (playable via downloadUrl('04_voice_profiles', preview)); '' if none
}
export interface VoicesListResult {
  has_script: boolean
  script_path: string
  voice_config_path: string
  speakers: VoiceItem[]
}
export interface PrepareVoicesOptions {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
}
export interface PrepareVoicesResult {
  count: number
  aliases: number
  speakers: string[]
  voice_config_path: string
  output_dir: string
  results: { speaker: string; ok: boolean; type: string; preview: string; description: string }[]
}
export interface BatchResult {
  total: number
  completed: number
  failed: { index: number; speaker: string; reason: string }[]
  output_dir: string
  manifest_path: string
}
export interface MergeResult {
  file: string
  path: string
  segments: number
  size: number
}

// ------------------------------ script (LLM -> JSON) ------------------------------
export interface ScriptEntry {
  speaker: string
  text: string
  instruct: string
}
export interface ScriptGenerateResult {
  entries: ScriptEntry[]
  output_path: string
  count: number
  speakers: string[]
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
    model: string
    base_model: string
    design_model: string
    speaker: string
    language: string
    device: string
    pause_between_speakers_ms: number
    pause_same_speaker_ms: number
    parallel_workers: number
    api_base: string
    api_key: string
    voice: string
    concurrency: number
  }
  llm: {
    base_url: string
    api_key: string
    model_name: string
  }
  prompts: {
    system_prompt: string
    user_prompt: string
  }
  persona_prompts: {
    system_prompt: string
    user_prompt: string
    advanced_prompt: string
  }
  generation: {
    chunk_size: number
    max_tokens: number
    temperature: number
    top_p: number
    top_k: number
    min_p: number
    presence_penalty: number
    banned_tokens: number[]
  }
  ffmpeg: { ffmpeg_path: string; ffprobe_path: string }
  log: { level: string }
  ui: { theme: string }
}

/** Workspace state from ``GET /api/workspace``. */
export interface WorkspaceInfo {
  set: boolean
  /** The workspace folder; empty string when no workspace is set. */
  path: string
  /** True when no user workspace is set yet (pipeline is locked). */
  is_default: boolean
  /** Artifact directory name → absolute path (01_input, 02_split_text, …); empty when unset. */
  dirs: Record<string, string>
}
