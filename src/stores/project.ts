import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { AudioCutResult, BookSplitResult, TextFormatResult } from '@/types'

/**
 * Pipeline handoff (requirement #3): each stage records the path(s) it produced
 * so the next stage can offer "前往下一步" with them as its input.
 *
 *   文本排版 output  →  分册切割 input
 *   分册切割 outputs →  TTS input (future)
 *   TTS outputs      →  音频分集 input
 */
export const useProjectStore = defineStore('project', () => {
  const textOutput = ref<string | null>(null) // a formatted .txt path
  const textResult = ref<TextFormatResult | null>(null)

  const bookOutputs = ref<string[]>([]) // volume .txt paths
  const bookResult = ref<BookSplitResult | null>(null)

  const ttsOutputs = ref<string[]>([]) // (future) tts audio paths
  const audioOutputs = ref<string[]>([]) // cut .mp3 paths
  const audioResult = ref<AudioCutResult | null>(null)

  // What the next stage would consume if the user hits "前往下一步".
  const bookInput = computed(() => textOutput.value)
  const ttsInput = computed(() => bookOutputs.value)
  // The audio stage consumes *audio* — only the (future) TTS output qualifies.
  // Book output is text, so it is deliberately not a fallback here.
  const audioInput = computed(() => ttsOutputs.value)

  function recordText(r: TextFormatResult) {
    textResult.value = r
    textOutput.value = r.output_path
  }
  function recordBook(r: BookSplitResult) {
    bookResult.value = r
    bookOutputs.value = r.files.map((f) => f.path)
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
    ttsOutputs,
    audioOutputs,
    audioResult,
    bookInput,
    ttsInput,
    audioInput,
    recordText,
    recordBook,
    recordAudio,
  }
})
