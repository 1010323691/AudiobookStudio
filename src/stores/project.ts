import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  AudioCutResult,
  BatchResult,
  BookSplitResult,
  MergeResult,
  PrepareVoicesResult,
  TextFormatResult,
} from '@/types'

/**
 * Pipeline handoff (requirement #3): each stage records the result it produced
 * so the dashboard can show per-stage completion and offer "前往下一步".
 *
 *   文本排版 output   →  分册切割 input
 *   分册切割 outputs  →  文本解析 input
 *   音频合并 output   →  音频分集 input
 *
 * The middle stages (文本解析 / 角色配音 / 音频合成 / 音频合并) all read fixed
 * workspace files (03_parsed_json / 04_voice_profiles / 05_audio_chunk), so they need no
 * path handoff — only a "done" marker for the dashboard.
 */
export const useProjectStore = defineStore('project', () => {
  const textOutput = ref<string | null>(null) // a formatted .txt path
  const textResult = ref<TextFormatResult | null>(null)

  const bookOutputs = ref<string[]>([]) // volume .txt paths
  const bookResult = ref<BookSplitResult | null>(null)

  // Which parsed JSON (a file name in 03_parsed_json/) the downstream 角色配音 / 音频合成
  // stages should read. Shared by both pages so they operate on the same file.
  // Empty string → the backend falls back to the most recently written JSON.
  const activeScript = ref('')
  const voiceResult = ref<PrepareVoicesResult | null>(null)
  const batchResult = ref<BatchResult | null>(null)
  const mergeResult = ref<MergeResult | null>(null)

  const audioOutputs = ref<string[]>([]) // cut .mp3 paths
  const audioResult = ref<AudioCutResult | null>(null)

  // What the next stage would consume if the user hits "前往下一步".
  const bookInput = computed(() => textOutput.value)
  const scriptInput = computed(() => bookOutputs.value)
  // The audio stage consumes *audio* — the merged audiobook (or a user-picked file).
  const audioInput = computed(() => mergeResult.value?.path ?? null)

  function recordText(r: TextFormatResult) {
    textResult.value = r
    textOutput.value = r.output_path
  }
  function recordBook(r: BookSplitResult) {
    bookResult.value = r
    bookOutputs.value = r.files.map((f) => f.path)
  }
  function setActiveScript(name: string) {
    activeScript.value = name
  }
  function recordVoices(r: PrepareVoicesResult) {
    voiceResult.value = r
  }
  function recordBatch(r: BatchResult) {
    batchResult.value = r
  }
  function recordMerge(r: MergeResult) {
    mergeResult.value = r
  }
  function recordAudio(r: AudioCutResult) {
    audioResult.value = r
    audioOutputs.value = r.files.map((f) => f.path)
  }

  return {
    textOutput,
    textResult,
    bookOutputs,
    bookResult,
    activeScript,
    voiceResult,
    batchResult,
    mergeResult,
    audioOutputs,
    audioResult,
    bookInput,
    scriptInput,
    audioInput,
    recordText,
    recordBook,
    setActiveScript,
    recordVoices,
    recordBatch,
    recordMerge,
    recordAudio,
  }
})
