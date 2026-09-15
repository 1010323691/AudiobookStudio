<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listDir } from '@/api/files'
import { batchStatusFiles, runBatch, ttsStatus } from '@/api/tts'
import type { BatchFileStatus, BatchResult, FileItem, TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Input from '@/components/ui/Input.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Layers,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  RotateCcw,
  ArrowRight,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)

// ---------------------------------------------------------------------------
// 待合成 rows: the 03_parsed_json/ directory listing + each file's live stats
// (completed/total, speaker count, ready voices, the 已合成 flag) — one row per
// parsed JSON, so the numbers sit on the row instead of below a single pick.
// ---------------------------------------------------------------------------
const fileNames = ref<string[]>([])
const statuses = ref<BatchFileStatus[]>([])
const filesLoaded = ref(false)
const filesLoading = ref(false)

// The same filter DirPicker applied: plain .json files, excluding the two-checks
// shared product (<stem>_checked.json).
function matches(i: FileItem): boolean {
  if (i.is_dir) return false
  const low = i.name.toLowerCase()
  return low.endsWith('.json') && !low.endsWith('_checked.json')
}

// Grid tracks shared by the list header and every row:
// [checkbox | filename (flexible) | 已合成/总段落 | 角色 | 已就绪声音].
// The numeric columns get fixed, generous tracks (header text fits on one line, no
// wrapping); the filename absorbs the leftover width; the 已合成 badge sits INSIDE the
// filename cell so badge rows and plain rows keep their numeric columns in the exact
// same place (the old flex layout let the badge push them right on wide screens).
const ROW_GRID =
  'grid grid-cols-[1.25rem_minmax(0,1fr)_6.5rem_3.5rem_5rem] items-center gap-x-6 px-2'

/** A 待合成 row: the directory's file name + its live stats (zeros when stats are missing). */
interface FileRow {
  name: string
  total: number
  completed: number
  remaining: number
  complete: boolean
  speakers: number
  ready: number
  missing: string[]
}

const rows = computed<FileRow[]>(() => {
  const byName = new Map(statuses.value.map((s) => [s.name, s]))
  return fileNames.value.map(
    (n) =>
      byName.get(n) ?? {
        name: n, total: 0, completed: 0, remaining: 0,
        complete: false, speakers: 0, ready: 0, missing: [],
      },
  )
})

/** Re-fetch ONLY the per-row stats (the poller's single request; the row list is untouched). */
async function refreshStatusesOnly() {
  if (!fileNames.value.length) return
  try {
    // `?? []` guards against a pre-upgrade backend (its /batch-status answer has no
    // `files` key) — the rows then show zeros instead of the computed crashing on render.
    statuses.value = (await batchStatusFiles(fileNames.value)).files ?? []
  } catch {
    /* backend down — keep the last known stats */
  }
}

async function refreshRows() {
  if (!workspaceSet.value) return
  filesLoading.value = true
  try {
    const r = await listDir('03_parsed_json')
    fileNames.value = r.items.filter(matches).map((i) => i.name)
    await refreshStatusesOnly()
  } catch {
    /* backend down — keep the last known rows */
  } finally {
    filesLoading.value = false
    filesLoaded.value = true
  }
}

// ---------------------------------------------------------------------------
// Selection: a local multi-select map (keyed by file name). The shared single
// value project.activeScript (角色配音 reads it) mirrors the FIRST selected file —
// written only on user interaction, so a multi-select is never collapsed by its
// own sync; an external change (角色配音 picking a file) collapses the selection
// to that file (the previous cross-page replace semantics).
// ---------------------------------------------------------------------------
const selected = reactive<Record<string, boolean>>({})
let lastSynced = project.activeScript
if (project.activeScript) selected[project.activeScript] = true

const selectedNames = computed(() => fileNames.value.filter((n) => selected[n]))

function syncScript() {
  const first = selectedNames.value[0] ?? ''
  project.activeScript = first
  lastSynced = first
}

function onRowChange(name: string, ev: Event) {
  if ((ev.target as HTMLInputElement).checked) selected[name] = true
  else delete selected[name]
  syncScript()
}

watch(
  () => project.activeScript,
  (v) => {
    if (v === lastSynced) return // our own sync — ignore
    for (const k of Object.keys(selected)) delete selected[k]
    if (v) selected[v] = true
    lastSynced = v
  },
)

// Select-all = every NOT-yet-complete file (a 已合成 row is never auto-checked);
// unchecking clears only the pending rows (a manually-checked complete row stays —
// it is still useful for 重新全部合成).
const pendingRows = computed(() => rows.value.filter((r) => !r.complete))
const allPendingSelected = computed(
  () => pendingRows.value.length > 0 && pendingRows.value.every((r) => selected[r.name]),
)
const somePendingSelected = computed(
  () => !allPendingSelected.value && pendingRows.value.some((r) => selected[r.name]),
)

function onSelectAllChange(ev: Event) {
  const on = (ev.target as HTMLInputElement).checked
  if (on) {
    for (const r of pendingRows.value) selected[r.name] = true
  } else {
    for (const r of pendingRows.value) delete selected[r.name]
  }
  syncScript()
}

// ---------------------------------------------------------------------------
// Warnings
// ---------------------------------------------------------------------------
const noSynthesizable = computed(
  () => filesLoaded.value && rows.value.length > 0 && rows.value.every((r) => r.total === 0),
)

/** Union of the not-ready speakers across the selected rows (all rows when nothing is selected). */
const warnNames = computed(() => {
  const scope = selectedNames.value.length ? rows.value.filter((r) => selected[r.name]) : rows.value
  const names = new Set<string>()
  for (const r of scope) for (const m of r.missing) names.add(m)
  return [...names]
})

// ---------------------------------------------------------------------------
// Run state
// ---------------------------------------------------------------------------
const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<BatchResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value) ?? null)

// 一键合成「批内段数」上限 (1..64)：把多段垫成一个 GPU 张量批，一批最多能垫多少段（只是上限，
// 不是固定并发数；模型只加载一次。实际每批条数 = min(段长分档〔短段跑满、长段自动降低、超长
// 单独〕, 实测显存动态调节, 显存估算, 单批字符上限)）。
// Seeded from the persisted config; written back to it on each run (see doRun).
const MIN_CONCURRENCY = 1
const MAX_CONCURRENCY = 64
const concurrency = ref(4)

function clampConcurrency(n: number): number {
  const v = Math.trunc(n)
  if (!Number.isFinite(v)) return MIN_CONCURRENCY
  return Math.max(MIN_CONCURRENCY, Math.min(MAX_CONCURRENCY, v))
}

// Coerce the 批内段数 input (the Input component emits a string) to an integer in [1, 64].
function onConcurrency(v: string | number) {
  concurrency.value = clampConcurrency(Number(v))
}

// Reproducible seed (a debug aid; NOT persisted to config): empty → use the configured
// default (config.tts.batch_seed, default -1 = random); a number → this run is reproducible.
const seedInput = ref('')
function runSeed(): number | undefined {
  const t = seedInput.value.trim()
  if (t === '') return undefined
  const v = Math.trunc(Number(t))
  return Number.isFinite(v) ? v : undefined
}

// ---------------------------------------------------------------------------
// Live progress: while a run is in flight, re-fetch the per-row stats every 3 s
// (the manifests are written incrementally, so the numbers are real, never
// animated) and the 已合成 badge appears the moment a file finishes.
// ---------------------------------------------------------------------------
let statusTimer: ReturnType<typeof setInterval> | null = null

function startStatusPolling() {
  if (statusTimer) return
  void refreshStatusesOnly()
  statusTimer = setInterval(() => void refreshStatusesOnly(), 3000)
}

function stopStatusPolling(final = true) {
  if (statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
  if (final) void refreshStatusesOnly()
}

// ---------------------------------------------------------------------------
// Lifecycle (the page is keep-alive cached: onUnmounted does NOT fire on
// navigation, so the poller follows onActivated/onDeactivated)
// ---------------------------------------------------------------------------
onMounted(async () => {
  // On a cold start settings may still be loading when onActivated runs (it fires right
  // after mounted, before this await resolves) — remember whether WE had to load it.
  const needLoad = !settings.loaded
  if (needLoad) await settings.load()
  // Initialize 批内段数 from the persisted config (falls back to the default of 4 if unavailable).
  concurrency.value = clampConcurrency(Number(settings.config?.tts?.batch_concurrency ?? 4))
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  taskStore.refresh()
  if (needLoad) void refreshRows() // the initial onActivated run was skipped (no workspace yet)
})

// Rows refresh whenever the page (re)appears, and the live poll resumes only if the
// tracked run is still going. onActivated also fires on first mount (initial load);
// on a cold start (settings still loading) it defers to onMounted's post-load refresh.
onActivated(() => {
  if (settings.loaded) void refreshRows()
  const st = task.value?.status
  if (task.value && (st === 'pending' || st === 'running' || st === 'paused')) startStatusPolling()
})
onDeactivated(() => stopStatusPolling(false)) // hidden: no timer, no requests
onUnmounted(() => stopStatusPolling(false)) // last resort (keep-alive usually prevents this)

async function doRun() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  busy.value = true
  error.value = ''
  result.value = null
  const concurrencyNow = concurrency.value
  try {
    // Default (resume): synthesize only the not-yet-done segments, skipping existing audio
    // (a fully-done file never loads the model at all).
    const { task_id } = await runBatch({ scripts: names, concurrency: concurrencyNow, seed: runSeed() })
    taskId.value = task_id
    await taskStore.refresh()
    // Remember the chosen 批内段数 in the persisted config (fire-and-forget: the run above
    // already carries it; a save failure here must not fail the run that just started).
    void settings.save({ tts: { batch_concurrency: concurrencyNow } })
    startStatusPolling()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

// 「重新全部合成」: the user explicitly clears the completion state and re-does EVERY segment
// of the selected files (loads the model again, re-synthesizes all). Gated behind a confirm
// since it is expensive and discards the resume shortcut.
async function doRunAll() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  if (!window.confirm('重新全部合成会清除选中文件内已完成状态并重做全部段落（需重新加载模型、耗时较长）。确定继续吗？')) return
  busy.value = true
  error.value = ''
  result.value = null
  const concurrencyNow = concurrency.value
  try {
    const { task_id } = await runBatch({ scripts: names, concurrency: concurrencyNow, seed: runSeed(), force_all: true })
    taskId.value = task_id
    await taskStore.refresh()
    void settings.save({ tts: { batch_concurrency: concurrencyNow } })
    startStatusPolling()
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

function cancel() {
  if (task.value) taskStore.control(task.value.id, 'cancel')
}

watch(
  () => task.value?.status,
  (st) => {
    const t = task.value
    if (!st || !t) return
    if (st === 'succeeded') {
      result.value = t.result as BatchResult
      taskId.value = null
      busy.value = false
      stopStatusPolling()
      project.recordBatch(result.value)
      toast({
        title: '音频合成完成',
        variant: result.value?.failed.length ? 'default' : 'success',
        description: `成功 ${result.value?.completed ?? 0} / ${result.value?.total ?? 0} 段`,
      })
    } else if (st === 'failed') {
      error.value = t.error || '音频合成失败'
      taskId.value = null
      busy.value = false
      stopStatusPolling()
      toast({ title: '音频合成失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      taskId.value = null
      busy.value = false
      stopStatusPolling()
    }
  },
)
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        音频合成
        <Badge :variant="status?.implemented ? 'success' : 'secondary'">
          {{ status?.implemented ? '可用' : '引擎未就绪' }}
        </Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">
        勾选一个或多个解析 JSON 后合成：多文件在一个任务里按顺序逐个合成（每个未完成文件启动一次
        引擎、模型只加载一次；已完成文件自动跳过），单段 / 单文件失败会记录而不中断；行内的
        已合成 / 总段落 · 角色 · 已就绪声音 实时刷新。每个 JSON 合成成一个「包」：音频段与
        <code class="text-xs">manifest.json</code> 一起保存到 <code class="text-xs">05_audio_chunk/&lt;JSON 基名&gt;/</code>。
      </p>
    </div>

    <WorkspaceGateAlert />

    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <!-- 待合成（多选） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />待合成</CardTitle>
          <CardDescription>
            解析 JSON（03_parsed_json/）——可多选；行内显示每个文件的合成进度、角色与声音就绪情况。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <!-- list header: select-all (excludes 已合成 rows) + selection count + refresh -->
          <div class="flex items-center gap-3 px-2">
            <input
              type="checkbox"
              class="h-4 w-4 shrink-0 accent-primary"
              :checked="allPendingSelected"
              :indeterminate="somePendingSelected"
              :disabled="!workspaceSet || !pendingRows.length"
              @change="onSelectAllChange"
            />
            <span class="text-sm text-muted-foreground">全选未合成文件</span>
            <span class="ml-auto text-xs text-muted-foreground">
              已选 {{ selectedNames.length }} / {{ fileNames.length }} 个文件
            </span>
            <Button variant="outline" size="sm" :disabled="filesLoading || !workspaceSet" @click="refreshRows">
              <RefreshCw class="h-3.5 w-3.5" :class="filesLoading && 'animate-spin'" />刷新
            </Button>
          </div>

          <div class="max-h-80 space-y-0.5 overflow-y-auto pr-1">
            <!-- Column header shares the rows' grid tracks (ROW_GRID) and sticks INSIDE the
                 scroll container: header and rows shrink by the same amount when the scrollbar
                 appears, so the columns can never drift apart. -->
            <div :class="[ROW_GRID, 'sticky top-0 z-10 bg-card py-1 text-xs text-muted-foreground']">
              <span />
              <span>文件（03_parsed_json/）</span>
              <span class="text-right">已合成 / 总段落</span>
              <span class="text-right">角色</span>
              <span class="text-right">已就绪声音</span>
            </div>
            <label
              v-for="r in rows"
              :key="r.name"
              :class="[
                ROW_GRID,
                'cursor-pointer rounded py-1.5 hover:bg-accent/50',
                { 'bg-accent/60': selected[r.name] },
              ]"
            >
              <input
                type="checkbox"
                class="h-4 w-4 accent-primary"
                :checked="!!selected[r.name]"
                :disabled="!workspaceSet"
                @change="onRowChange(r.name, $event)"
              />
              <!-- 已合成 badge lives inside the filename cell — it never displaces the numeric columns -->
              <span class="flex min-w-0 items-center gap-2 text-sm">
                <Badge v-if="r.complete" variant="success" class="shrink-0">已合成</Badge>
                <span class="min-w-0 truncate" :title="r.name">{{ r.name }}</span>
              </span>
              <span class="text-right text-xs tabular-nums text-muted-foreground">
                {{ r.completed }} / {{ r.total }}
              </span>
              <span class="text-right text-xs tabular-nums text-muted-foreground">
                {{ r.speakers }}
              </span>
              <span
                class="text-right text-xs tabular-nums"
                :class="r.ready < r.speakers ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'"
              >
                {{ r.ready }}
              </span>
            </label>
            <p v-if="filesLoaded && !rows.length" class="px-2 py-3 text-sm text-muted-foreground">
              03_parsed_json/ 里没有解析 JSON——请先到「文本解析」生成。
            </p>
          </div>

          <Alert v-if="noSynthesizable" variant="default">
            没有可合成的段——请先到「文本解析」生成脚本。
          </Alert>
          <Alert v-if="warnNames.length" variant="warning">
            有 {{ warnNames.length }} 个角色尚未就绪声音（{{ warnNames.slice(0, 5).join('、') }}{{
              warnNames.length > 5 ? ' 等' : ''
            }}）——建议先到「角色配音」页一键生成，否则这些角色的段会失败。
          </Alert>
          <p v-if="filesLoaded && rows.length && !selectedNames.length" class="text-xs text-muted-foreground">
            勾选要合成的文件（「全选未合成文件」= 全部未完成文件，不含已合成的）。
          </p>
        </CardContent>
      </Card>

      <!-- 操作 + 实时日志 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />一键音频合成</CardTitle>
          <CardDescription>
            按勾选的文件逐个合成（每个文件单独启动一次引擎、模型只加载一次；已完成文件自动跳过）。
            把多段垫成 GPU 张量批一次并行推理（「批内段数」只是上限，1 = 逐段串行；实际每批条数
            按段长自动分档——短段跑满、长段自动降低、超长单独——并按实测显存余量实时升降），
            实时显示「正在生成（角色 X）」与「完成 i / N 段」及成功 / 失败。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || !workspaceSet || !selectedNames.length" @click="doRun">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Layers v-else class="h-4 w-4" />
              {{ busy ? '合成中…' : `开始音频合成${selectedNames.length ? `（${selectedNames.length} 个文件）` : ''}` }}
            </Button>
            <Button
              variant="outline"
              :disabled="busy || !workspaceSet || !selectedNames.length"
              title="清除选中文件内已完成状态，重新合成全部段落"
              @click="doRunAll"
            >
              <RotateCcw class="h-4 w-4" />重新全部合成
            </Button>
            <label class="flex items-center gap-2 text-sm text-muted-foreground">
              批内段数
              <Input
                :modelValue="concurrency"
                type="number"
                min="1"
                max="64"
                step="1"
                class="h-8 w-20"
                :disabled="busy"
                @update:modelValue="onConcurrency"
              />
            </label>
            <label
              class="flex items-center gap-2 text-sm text-muted-foreground"
              title="留空 = 用「设置」里的默认（默认 -1 = 随机）；填数字 = 本次运行可复现。仅本次运行生效，不写入设置。"
            >
              seed
              <Input
                :modelValue="seedInput"
                type="number"
                step="1"
                placeholder="默认随机"
                class="h-8 w-24"
                :disabled="busy"
                @update:modelValue="seedInput = String($event)"
              />
            </label>
          </div>

          <LiveLogPanel :task="task" :max-height-class="'h-96'">
            <template #actions>
              <Button v-if="task" variant="outline" size="sm" @click="cancel">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <!-- 结果 -->
          <div v-if="result" class="space-y-3">
            <Alert variant="default" class="items-center">
              <template #icon>
                <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-500" />
              </template>
              <span class="flex-1">
                本次成功 {{ result.completed }} / 共 {{ result.total }} 段
                <span v-if="result.failed.length" class="text-amber-600 dark:text-amber-400">
                  ，失败 {{ result.failed.length }}
                </span>
                <span v-if="result.done_count != null" class="text-xs text-muted-foreground">
                  · 累计已合成 {{ result.done_count }} / 全部 {{ result.all_count }} 段
                </span>
              </span>
              <Button v-if="(result.done_count ?? result.completed) > 0" size="sm" @click="router.push('/merge')">
                前往音频合并<ArrowRight class="h-4 w-4" />
              </Button>
            </Alert>

            <div v-if="result.files?.length" class="space-y-1.5">
              <div class="text-xs font-medium text-muted-foreground">各文件结果（{{ result.files.length }}）</div>
              <div class="max-h-56 space-y-1 overflow-y-auto rounded-md border p-2 text-xs">
                <div v-for="f in result.files" :key="f.script" class="flex items-start gap-2">
                  <span class="min-w-0 flex-1 truncate font-medium" :title="f.script">{{ f.script }}</span>
                  <span v-if="f.error" class="text-destructive">— {{ f.error }}</span>
                  <span v-else class="flex shrink-0 items-center gap-1 text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 class="h-3.5 w-3.5" />
                    {{ f.done_count }} / {{ f.all_count }} 段
                    <span v-if="f.failed > 0" class="text-amber-600 dark:text-amber-400">
                      · 失败 {{ f.failed }}
                    </span>
                  </span>
                </div>
              </div>
            </div>

            <div v-if="result.failed.length" class="space-y-1.5">
              <div class="text-xs font-medium text-muted-foreground">失败的段（{{ result.failed.length }}）</div>
              <div class="max-h-56 space-y-1 overflow-y-auto rounded-md border p-2 text-xs">
                <div v-for="(f, i) in result.failed" :key="i" class="flex items-start gap-2">
                  <span v-if="f.script" class="shrink-0 text-muted-foreground">{{ f.script }} · </span>
                  <span class="shrink-0 text-muted-foreground">第 {{ f.index + 1 }} 段</span>
                  <span class="shrink-0 font-medium">{{ f.speaker || '(未知)' }}</span>
                  <span class="text-destructive">— {{ f.reason }}</span>
                </div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
