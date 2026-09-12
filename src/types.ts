// Shared backend types — mirror the FastAPI response/request shapes exactly.
// Keeping these in one place so the views, api modules and stores agree.

// ------------------------------ files ------------------------------
export interface UploadResult {
  path: string
  name: string
  size: number
}
export interface FileItem {
  name: string
  is_dir: boolean
  size: number | null
}
/** Response of ``GET /api/files/list/{module}``. */
export interface DirListResult {
  path: string
  items: FileItem[]
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
  status: 'ready' | 'pending' // overall ready = alias OR a usable voice (clone/design/custom); a bare foundation is NOT ready
  foundation_status: 'none' | 'done' | 'failed' // Phase 1 (语音推理基础) state
  clone_status: 'none' | 'done' | 'failed' // Phase 2 (克隆音频) state
  type: string // clone | design | custom | foundation | ''
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
export interface PrepareFoundationsOptions {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
  /** Which parsed JSON (in 03_parsed_json/) to read; undefined → most recent. */
  script?: string
}
/** Phase 2 (TTS only): options for ``POST /api/tts/make-clones`` (批量制作克隆音频). */
export interface MakeClonesOptions {
  speakers?: string[]
  new_only?: boolean
  /** Number of parallel TTS subprocesses; undefined → backend default (1). */
  concurrency?: number
  /** Which parsed JSON to read for the character set; undefined → most recent. */
  script?: string
}
/** Options for ``POST /api/tts/batch`` (音频合成). */
export interface BatchRunOptions {
  indices?: number[]
  /** Which parsed JSON (in 03_parsed_json/) to synthesize; undefined → most recent. */
  script?: string
  /** Concurrent segments (1..32); undefined → the persisted default (config.tts.batch_concurrency). */
  concurrency?: number
  /** True → re-synthesize EVERY segment (clears the resume skip); undefined/false → resume
   *  (only the not-yet-done segments, skipping existing audio). */
  force_all?: boolean
}
export interface PrepareFoundationsResult {
  count: number
  aliases: number
  speakers: string[]
  voice_config_path: string
  results: { speaker: string; ok: boolean; type: string; description: string }[]
}
/** Phase 2 (TTS) batch result (``POST /api/tts/make-clones``). */
export interface MakeClonesResult {
  count: number
  ok: number
  failed: number
  speakers: string[]
  voice_config_path: string
  output_dir: string
  results: { speaker: string; ok: boolean; type: string; preview: string; reason?: string }[]
}
export interface BatchResult {
  total: number
  completed: number
  failed: { index: number; speaker: string; reason: string }[]
  output_dir: string
  manifest_path: string
  /** Cumulative (after this run) number of segments already synthesized — 「累计已合成 X」. */
  done_count?: number
  /** Total synthesizable segments in the script — 「全部 Y」 (denominator of the cumulative count). */
  all_count?: number
}
/** Synthesis progress for the 待合成 card's 【已合成 / 总段落】 (``GET /api/tts/batch-status``). */
export interface BatchStatus {
  total: number
  completed: number
  remaining: number
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
  output_name?: string
  count: number
  speakers: string[]
}
/** Response of ``POST /api/script/generate-files``: one independent task per file. */
export interface GenerateFilesResult {
  task_ids: string[]
  files: { file: string; task_id: string }[]
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
  /** Raw LLM stream (「流式反馈」 panel); populated by `llm_chunk` events / snapshots. */
  llm_stream?: string
  /** Live LLM generation rate (chars/s) for the 文本解析 per-window gauge; 0 when idle / queued. */
  llm_cps?: number
  /** 10-second-window average of the LLM generation rate (chars/s) for the 文本解析 吞吐量 card;
   *  backend-computed as (chars over the last 10 s) / 10 s from the real streamed text; 0 when idle. */
  llm_cps_10s?: number
  /** Cumulative original-text chars processed (per completed chunk) — 处理速度 numerator. */
  llm_chars?: number
  /** Cumulative processing seconds up to the last completed chunk — 处理速度 denominator. */
  llm_secs?: number
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
    /** 角色配音·阶段 2（克隆）的并行 TTS 子进程数。 */
    parallel_workers: number
    /** 音频合成（一键合成）的并发段数（单子进程内线程池；1 = 串行；范围 1..32）。 */
    batch_concurrency: number
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
  /** Speaker 检查 — 每次送检段落数 + 上下文窗口大小 + 独立的检查提示词（与 `prompts` 完全分离）。 */
  speaker_check: {
    /** 每次送检段落数：每批送入 LLM 重判的目标条数（每批再在前后各加 `context_window` 条上下文）。 */
    batch_size: number
    /** 上下文窗口大小：每批送检块前后各取 N 条上下文（仅供理解、不改判）。 */
    context_window: number
    system_prompt: string
    user_prompt: string
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
    /** Max files parsed in parallel (LLM jobs); the rest queue behind a shared gate. */
    max_concurrency: number
  }
  ffmpeg: { ffmpeg_path: string; ffprobe_path: string }
  log: { level: string }
  ui: { theme: string }
}

/** A recursively-partial ``AppConfig`` — mirrors the backend's deep-merge ``update_config``
 *  (``PUT /api/config``), which patches only the fields actually sent, at any nesting depth.
 *  Nested object sections may be partial; arrays are provided whole. */
export type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends readonly any[]
    ? T[K]
    : T[K] extends object
      ? DeepPartial<T[K]>
      : T[K]
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
