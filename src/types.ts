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
  total_chars: number
  chapters: BookChapter[]
  chapter_count: number
  filenames: string[]
  /** The chapter format the splitter actually recognizes (「第N章」) — user-facing. */
  expected_format: string
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
  gender: 'male' | 'female' | '' // '' = unknown; pre-filled by Phase 1, the badge pick wins
  description: string
  preview: string // path relative to 04_voice_profiles/ (playable via downloadUrl('04_voice_profiles', preview)); '' if none
  /** The character's clone candidates (new format; a legacy single-take entry synthesises
   *  one, entries without a clone yield []). ``preview`` is relative to 04_voice_profiles/. */
  candidates: { id: string; preview: string; seed: number }[]
  /** The user's candidate pick; null = no explicit pick (the first candidate is active). */
  selected_audio_id: string | null
}
export interface VoicesListResult {
  has_script: boolean
  script_path: string
  voice_config_path: string
  speakers: VoiceItem[]
}
/** 角色配音 · 合并角色：source 的全部台词在 Parse 源数据中改为 target，source 的
 *  声音配置被删除（候选音频文件留盘）。``files`` = 实际被改写的解析 JSON 文件名。 */
export interface MergeSpeakersResult {
  ok: boolean
  source: string
  target: string
  replaced: number
  files: string[]
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
  /** 批内行数上限（单个 worker 进程内的 GPU 张量批；1 = 逐条串行；1..64）；
   *  undefined → 后端缺省 (config.tts.batch_concurrency=4)。 */
  concurrency?: number
  /** Which parsed JSON to read for the character set; undefined → most recent. */
  script?: string
  /** Per-character clone-candidate count: undefined/null → auto (absolute log-scale
   *  ladder on each character's OWN line count — the 旁白's 10×+ line count can't
   *  demote the leads); 2/4/6/8 → fixed count. */
  candidate_count?: number | null
}
/** Options for ``POST /api/tts/batch`` (音频合成). */
export interface BatchRunOptions {
  indices?: number[]
  /** Which parsed JSON (in 03_parsed_json/) to synthesize; undefined → most recent. */
  script?: string
  /** Multi-file run (the 待合成 card's multi-select): parsed JSON file names in 03_parsed_json/.
   *  Takes precedence over `script`; the files are synthesized one by one in a single task —
   *  each file is its own package, a fully-done file is skipped without loading the model,
   *  and a per-file failure is isolated (the rest of the batch continues). */
  scripts?: string[]
  /** 批内段数（上限，1..64，不是固定并发数）：把多段垫成一个 GPU 张量批一次并行推理；
   *  undefined → 持久默认 (config.tts.batch_concurrency)。实际每批条数按段长自动分档
   *  （短段跑满、长段自动降低、超长单独），并按实测显存余量实时升降。 */
  concurrency?: number
  /** Reproducible seed for the run: >=0 seeds each sub-batch (seed + sub-batch seq);
   *  undefined → the persisted default (config.tts.batch_seed); -1 → random. */
  seed?: number
}
/** 压测（临时测试入口）· 单轮结果行（每行字数从起点每轮递增，跑到失败为止）。 */
export interface StressTestResultRow {
  /** 轮次（1 起）。 */
  round: number
  /** 本轮每行字数。 */
  chars_per_line: number
  /** 批内行数（--concurrency 上限）。 */
  rows: number
  /** 处理量（行数 × 字数）。 */
  total_chars: number
  /** 成功行数。 */
  ok: number
  /** 失败行数。 */
  failed: number
  /** 合成耗时（秒，「模型就绪」→ 进程退出，不含模型加载）；null = 超时被杀 / 无法测得。 */
  synth_seconds: number | null
  /** 限时（秒）= 处理量 / 10（吞吐标准：1 秒必须出 10 个字）。 */
  deadline_seconds: number
  /** 真实吞吐量（字/秒）；未通过 / 无法测得为 null。 */
  throughput_chars_per_sec: number | null
  /** 是否通过（限时内完成且无崩溃）。 */
  passed: boolean
  /** 失败原因（通过为空串）。 */
  reason: string
}
/** 压测（临时测试入口）启动参数（``POST /api/tts/stress-test``）。 */
export interface StressTestOptions {
  /** 批内行数（--concurrency 上限，后端钳 1..64）；undefined → 64。 */
  rows?: number
  /** 起始每行字数（1..2500）；undefined → 10。 */
  start_chars?: number
  /** 每轮递增的每行字数（≥1）；undefined → 10。 */
  step_chars?: number
  /** 轮数上限；undefined → 不限（跑到失败为止）。 */
  max_rounds?: number
  /** 压测用的克隆音色角色名；undefined/'' → 后端自动取第一个可用克隆音色。 */
  speaker?: string
  /** 可复现 seed；undefined → 持久默认（config.tts.batch_seed）。 */
  seed?: number
}
/** 压测任务结果（``POST /api/tts/stress-test``）。 */
export interface StressTestResult {
  ts: string
  /** 实际使用的克隆音色角色名。 */
  speaker: string
  rows: number
  start_chars: number
  step_chars: number
  /** 吞吐标准（字/秒，= 10）。 */
  min_throughput_chars_per_sec: number
  seed: number
  /** 逐轮明细，按运行顺序。 */
  results: StressTestResultRow[]
  /** 失败轮的每行字数（无失败为 null）。 */
  failed_at_chars: number | null
  stopped_reason: string
  /** 结果文件（工作空间 stress_test/ 目录）。 */
  result_path: string
  /** 运行日志（排障用）。 */
  run_log: string
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
  results: { speaker: string; ok: boolean; type: string; preview: string; reason?: string; candidates?: number }[]
}
export interface BatchResult {
  total: number
  completed: number
  /** Failed segments (a multi-file run tags each entry with its file name in `script`). */
  failed: { index: number; speaker: string; reason: string; script?: string }[]
  output_dir: string
  manifest_path: string
  /** Cumulative (after this run) number of segments already synthesized — 「累计已合成 X」. */
  done_count?: number
  /** Total synthesizable segments in the script — 「全部 Y」 (denominator of the cumulative count). */
  all_count?: number
  /** Per-file outcomes (multi-file runs only; one entry per requested file, in request order). */
  files?: BatchFileResult[]
}
/** Per-file outcome of a multi-file synthesis run (``BatchResult.files``). */
export interface BatchFileResult {
  /** The parsed JSON file name (03_parsed_json/). */
  script: string
  total: number
  completed: number
  /** Number of failed segments in this file (top-level `failed` carries the details, tagged). */
  failed: number
  output_dir: string
  manifest_path: string
  /** Cumulative done segments after the run (resume-aware). */
  done_count: number
  /** Total synthesizable segments in the file. */
  all_count: number
  /** Non-null when the file itself failed fatally (e.g. unreadable JSON) — isolated, the rest
   *  of the batch continues (the reason is in the task log). */
  error: string | null
}
/** Per-file synthesis stats (the 待合成 rows; ``GET /api/tts/batch-status?scripts=…``). */
export interface BatchFileStatus {
  name: string
  total: number
  completed: number
  remaining: number
  /** Every segment synthesized (ok + file on disk) → the row's 【已合成】 badge. */
  complete: boolean
  /** Distinct speakers in the file (first-appearance order, incl. NARRATOR). */
  speakers: number
  /** Speakers with a usable voice (an alias, or a ready clone/design/custom) — the same rule
   *  the 角色配音 page uses for its ready state. */
  ready: number
  /** Speakers without a usable voice (warned before the run). */
  missing: string[]
}
/** Response of ``GET /api/tts/batch-status?scripts=…`` (one entry per requested file, in order). */
export interface BatchStatusFiles {
  files: BatchFileStatus[]
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
  audio: {
    target_duration: string
    naming_format: string
    start_number: string
    smart_align: boolean
    align_tolerance: number
  }
  tts: {
    /** 遗留字段（无读取方，仅为旧配置 round-trip 保留）。 */
    parallel_workers: number
    /** 音频合成与角色配音·克隆共用的「批内行数」上限（只是上限，不是固定并发数）：把多行
     *  垫成一个 GPU 张量批一次并行推理（1 = 逐行串行；范围 1..64）。实际每批条数按行长自动
     *  分档（短行跑满、长行自动降低、超长单独），并按实测显存余量实时升降。 */
    batch_concurrency: number
    /** 子批规划检查开关（默认全开 = 行为不变；关闭 = 规划跳过该约束并留日志）。
     *  段长分档：短段跑满上限、长段自动降档、>2048 字单独成批。 */
    planner_length_bands: boolean
    /** 单批字符上限：一个子批的总字数 ≤ 上限（防超大 prefill / TDR 挂起）。 */
    planner_batch_chars: boolean
    /** 超长行独批：>2500 字的行不与短行混批。 */
    planner_seq_chars: boolean
    /** 批内长度比：批内最长/最短 ≤3（防短行按混入长行的解码上限跑全程）。 */
    planner_length_ratio: boolean
    /** 显存静态估算门（对短行偏保守）；只关静态门——实测显存的动态调节恒生效。 */
    planner_vram: boolean
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
    /** Max files parsed in parallel (LLM jobs); the rest queue behind a shared gate. */
    max_concurrency: number
    /** 解析后归属抽样率（0 = 关闭）：1/3 纯随机（整书错误率仪表）+ 2/3 风险加权。
     *  每本读数记入任务日志与 config/spot_check_history.json；降不降由用户手动决定。 */
    spot_check_rate: number
    /** 断句失败校验开关（解析内阶段；关闭 = 跳过该阶段并记录日志）。 */
    revalidate_splits: boolean
    /** 纯归属标签条删除开关（解析内阶段；关闭 = 跳过该阶段并记录日志）。 */
    delete_saying_tags: boolean
    /** 解析内重判批大小（断句失败校验 / 归属抽样共用）；不在设置页露出。 */
    check_batch_size: number
    /** 解析内重判上下文窗口（断句失败校验 / 归属抽样共用）；不在设置页露出。 */
    check_context_window: number
  }
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
  /** Whether the pointed folder still exists on disk (false = moved/deleted → re-select). */
  exists?: boolean
  /** True when no user workspace is set yet (pipeline is locked). */
  is_default: boolean
  /** Artifact directory name → absolute path (01_input, 02_split_text, …); empty when unset. */
  dirs: Record<string, string>
}
