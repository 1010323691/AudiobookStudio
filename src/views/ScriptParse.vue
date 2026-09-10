<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { generateScriptFiles, checkScriptFiles } from '@/api/script'
import { listDir } from '@/api/files'
import { downloadFile } from '@/utils/fileops'
import { formatBytes } from '@/utils/format'
import type { FileItem, TaskSnapshot } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import LiveStreamPanel from '@/components/ui/LiveStreamPanel.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  FileText,
  ScanText,
  ShieldCheck,
  Loader2,
  XCircle,
  CheckCircle2,
  Download,
  RefreshCw,
  ListChecks,
  Eraser,
  TriangleAlert,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()

// LLM / 生成参数 / Prompt / Speaker 检查 的配置编辑已迁移到「设置」页；本页只从
// settings.config 读取已保存的值（用于并发数显示与模型名校验），不再本地编辑 / 保存。

// ---- File selection (02_split_text) + per-file parse jobs ------------------------
// The user checks one or more split .txt files; each becomes an independent backend
// Task (its own LLM request, status, success and JSON output). The frontend never
// reads file contents — it sends only file names; the backend does the discovery,
// the concurrency-bounded LLM calls, and the per-file JSON writes.
interface ParseFile extends FileItem {
  /** True when 03_parsed_json/<file-stem>.json already exists (already parsed). */
  done: boolean
  /** True when 03_parsed_json/<file-stem>_checked.json exists (Speaker 检查 已完成). */
  checked: boolean
}
const files = ref<ParseFile[]>([])
const filesLoading = ref(false)
const filesError = ref('')
const selected = reactive<Record<string, boolean>>({})

const selectedNames = computed(() => files.value.filter((f) => selected[f.name]).map((f) => f.name))
const doneCount = computed(() => files.value.filter((f) => f.done).length)
const checkedCount = computed(() => files.value.filter((f) => f.checked).length)
/** Selected files that are already parsed — the only ones a Speaker 检查 can run on. */
const checkableSelected = computed(() => files.value.filter((f) => selected[f.name] && f.done).length)
const allSelected = computed(() => files.value.length > 0 && files.value.every((f) => selected[f.name]))

const error = ref('')
const fileJobs = ref<{ name: string; taskId: string; kind: 'parse' | 'check' }[]>([])

function jobTask(taskId: string): TaskSnapshot | undefined {
  return taskStore.tasks.find((t) => t.id === taskId)
}

type JobState = {
  label: string
  variant: 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
}
function jobState(task: TaskSnapshot | undefined, kind: 'parse' | 'check' = 'parse'): JobState {
  if (!task) return { label: '待处理', variant: 'secondary' }
  switch (task.status) {
    case 'succeeded':
      return { label: '已完成', variant: 'success' }
    case 'failed':
      return { label: '失败', variant: 'destructive' }
    case 'cancelled':
      return { label: '已取消', variant: 'outline' }
    case 'paused':
      return { label: '已暂停', variant: 'secondary' }
    case 'running':
      // Still waiting on the concurrency gate reads as "queued", not actively working.
      if (/排队/.test(task.current || '')) return { label: '待处理', variant: 'secondary' }
      return kind === 'check'
        ? { label: '检查中', variant: 'default' }
        : { label: '解析中', variant: 'default' }
    default:
      return { label: '待处理', variant: 'secondary' }
  }
}

interface JobRow {
  name: string
  taskId: string
  kind: 'parse' | 'check'
  task: TaskSnapshot | undefined
  state: JobState
  progress: number
  active: boolean
  error: string
  outputName: string
}
const jobRows = computed<JobRow[]>(() =>
  fileJobs.value.map((j) => {
    const task = jobTask(j.taskId)
    const status = task?.status
    return {
      name: j.name,
      taskId: j.taskId,
      kind: j.kind,
      task,
      state: jobState(task, j.kind),
      progress: task?.progress ?? 0,
      active: status === 'pending' || status === 'running' || status === 'paused',
      error: task?.error || '',
      outputName:
        (task?.result?.output_name as string) ||
        j.name.replace(/\.[^.]+$/, '') + '.json',
    }
  }),
)

// "In flight" while any job's task hasn't reached a terminal state (drives the button).
const busy = computed(() => jobRows.value.some((r) => r.active))

// Effective concurrency after the backend's clamp (core/concurrency.py: max(1, int(n or 1))).
// 0 / empty / negative / non-numeric all collapse to 1 — the silent "fully serial" footgun,
// so mirror it here to warn the user before they launch a batch that can't actually run in
// parallel. A low value doesn't merely slow the batch: files beyond the cap queue and run
// one/few at a time, which reads as "the LLM isn't concurrent" when it is.
const effectiveConcurrency = computed(() => Math.max(1, Number(settings.config?.generation.max_concurrency) || 1))

const concurrencyWarning = computed(() => {
  const n = selectedNames.value.length
  if (!n) return ''
  const eff = effectiveConcurrency.value
  if (eff >= n) return ''
  return eff === 1
    ? `并发数为 1，将串行逐个解析这 ${n} 个文件（一个完整跑完才开始下一个），无法并发。请在上方「并发数」设为 ≥ ${n} 以同时解析。`
    : `并发数（${eff}）小于所选文件数（${n}）：前 ${eff} 个文件并发解析，其余 ${n - eff} 个排队，待有槽位时再逐个进行。`
})

// ---- 性能指标（顶部 3 卡）：并发数 / 吞吐量 / 处理速度 ---------------------------
// 吞吐量 (字/s): 各运行中窗口「近 10 秒平均」生成速率之和。每个任务的 10 秒窗口速率
// (task.llm_cps_10s) 由后端按真实流式字符算出（近 10 秒生成字符 ÷ 对应秒数）并经 SSE 实时推送；
// 前端只把它们相加（同一时间窗口的速率可加：各运行窗口之和 = 总体近 10 秒平均）。用 computed
// 跟随 SSE 事件重算，无需定时器；无运行中窗口时自然为 0，段间 / 排队窗口随时间在后端衰减。
const totalTps = computed(() => {
  let sum = 0
  for (const r of jobRows.value) if (r.task?.status === 'running') sum += r.task.llm_cps_10s ?? 0
  return sum
})

// 一批解析结束后自动刷新文件列表：把刚生成 JSON 的文件标记为「已完成」并收起其勾选。
watch(busy, (b, was) => {
  if (was && !b) loadFiles()
})

// 处理速度 (字/s): Σ(已完成各段原始字符数) ÷ Σ(到各段为止的累计处理耗时)。
//   分子 / 分母都由后端在每段完成后一并上报（llm_chars / llm_secs）——耗时冻结于"该段完成"
//   的时刻而非实时时钟。因此本指标只在"某段刚完成"时更新一次，段与段之间保持恒定，
//   不会像用实时时钟做分母那样在等待下一段时持续走低。真实字符，非 token。
// A computed, so it recomputes exactly when a chunk-completion event lands (never on a
// timer) — per-segment updates with no decay in between.
const speedCps = computed(() => {
  let chars = 0
  let secs = 0
  for (const r of jobRows.value) {
    chars += r.task?.llm_chars ?? 0  // 各任务已完成各段的累计原始字符数
    secs += r.task?.llm_secs ?? 0    // 各任务到已完成各段为止的累计处理耗时
  }
  return secs > 0 ? chars / secs : 0
})

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  await loadFiles()
  taskStore.refresh()
})

async function loadFiles() {
  if (!workspaceSet.value) {
    files.value = []
    return
  }
  filesLoading.value = true
  filesError.value = ''
  try {
    const r = await listDir('02_split_text')
    // 已生成判断：03_parsed_json/ 下是否已有 <文件基名>.json（后端对缺失目录返回空列表）。
    const out = await listDir('03_parsed_json').catch(() => null)
    const outNames = new Set((out?.items ?? []).map((i) => i.name))
    const txts: ParseFile[] = r.items
      .filter((i) => !i.is_dir && i.name.toLowerCase().endsWith('.txt'))
      .map((f) => {
        const stem = f.name.replace(/\.[^.]+$/, '')
        return {
          ...f,
          done: outNames.has(stem + '.json'),
          checked: outNames.has(stem + '_checked.json'),
        }
      })
    files.value = txts
    // 默认全不选：只保留仍存在于磁盘的既有勾选（已完成文件同样可勾选，用于重新解析 / 跑其他流程），
    // 剔除已消失的文件。
    const names = new Set(txts.map((t) => t.name))
    for (const k of Object.keys(selected)) if (!names.has(k)) delete selected[k]
  } catch (e: any) {
    filesError.value = e?.message || '读取文件列表失败'
  } finally {
    filesLoading.value = false
  }
}

function selectAll() {
  files.value.forEach((f) => {
    selected[f.name] = true
  })
}
function clearAll() {
  files.value.forEach((f) => {
    delete selected[f.name]
  })
}
function onFileChange(name: string, ev: Event) {
  if ((ev.target as HTMLInputElement).checked) selected[name] = true
  else delete selected[name]
}

async function startParse() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  if (!(settings.config?.llm.model_name || '').trim()) {
    error.value = '请先填写 LLM 模型名称（模型不能为空）。'
    return
  }
  error.value = ''
  try {
    const r = await generateScriptFiles(names)
    fileJobs.value = r.files.map((f) => ({ name: f.file, taskId: f.task_id, kind: 'parse' as const }))
    await taskStore.refresh()
  } catch (e: any) {
    error.value = e?.message || '启动解析失败'
  }
}

async function startCheck() {
  if (busy.value) return
  if (!(settings.config?.llm.model_name || '').trim()) {
    error.value = '请先填写 LLM 模型名称（模型不能为空）。'
    return
  }
  // 只有已解析完成（[已完成]）的文件才能检查；派生其 03_parsed_json 基文件名 <stem>.json。
  const names = files.value
    .filter((f) => selected[f.name] && f.done)
    .map((f) => f.name.replace(/\.[^.]+$/, '') + '.json')
  if (!names.length) {
    error.value = '请先勾选至少一个已完成解析（[已完成]）的文件，再开始 Speaker 检查。'
    return
  }
  error.value = ''
  try {
    const r = await checkScriptFiles(names)
    fileJobs.value = r.files.map((f) => ({ name: f.file, taskId: f.task_id, kind: 'check' as const }))
    await taskStore.refresh()
  } catch (e: any) {
    error.value = e?.message || '启动检查失败'
  }
}

function cancelJob(task: TaskSnapshot | undefined) {
  if (task) taskStore.control(task.id, 'cancel')
}

function downloadJob(row: JobRow) {
  downloadFile('03_parsed_json', row.outputName)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        <ScanText class="h-6 w-6" />文本解析
      </h1>
      <p class="mt-1 text-muted-foreground">
        从 <code class="text-xs">02_split_text/</code> 勾选要解析的分册文本（可多选），每个文件作为独立任务
        并发调用 LLM（并发数可配），分别生成 <code class="text-xs">03_parsed_json/&lt;文件基名&gt;.json</code>；
        每个文件独立成败、互不影响。
      </p>
    </div>

    <WorkspaceGateAlert />

    <!-- 选择待解析文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><FileText class="h-5 w-5" />选择待解析文件</CardTitle>
        <CardDescription>
          列出工作空间 <code class="text-xs">02_split_text/</code> 下的分册文本，勾选要处理的分册（可多选）；
          已生成 <code class="text-xs">03_parsed_json/</code> 的标记为「已完成」（可再次勾选以重新解析 / 跑其他流程）。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="filesError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ filesError }}
        </Alert>

        <div
          v-else-if="files.length"
          class="max-h-80 space-y-1 overflow-y-auto rounded-md border p-2"
        >
          <label
            v-for="f in files"
            :key="f.name"
            class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
          >
            <input
              type="checkbox"
              class="h-4 w-4 shrink-0 accent-primary"
              :checked="!!selected[f.name]"
              :disabled="busy"
              @change="onFileChange(f.name, $event)"
            />
            <span class="min-w-0 flex-1 truncate text-sm">{{ f.name }}</span>
            <Badge v-if="f.done" variant="success" class="shrink-0">已完成</Badge>
            <Badge v-if="f.checked" variant="secondary" class="shrink-0">已检查</Badge>
            <span class="shrink-0 text-xs text-muted-foreground">{{ formatBytes(f.size) }}</span>
          </label>
        </div>
        <p v-else class="text-sm text-muted-foreground">
          02_split_text/ 下暂无 .txt 文件——请先到「分册切割」生成分册。
        </p>

        <div class="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" :disabled="busy || !files.length" @click="selectAll">
            <ListChecks class="h-3.5 w-3.5" />{{ allSelected ? '已全选' : '全选' }}
          </Button>
          <Button variant="outline" size="sm" :disabled="busy || !files.length" @click="clearAll">
            <Eraser class="h-3.5 w-3.5" />清空
          </Button>
          <Button variant="outline" size="sm" :disabled="busy || filesLoading" @click="loadFiles">
            <RefreshCw class="h-3.5 w-3.5" :class="filesLoading ? 'animate-spin' : ''" />刷新
          </Button>
          <span class="ml-auto text-xs text-muted-foreground">
            已选 {{ selectedNames.length }} / {{ files.length }} 个
            <span v-if="doneCount"> · 已完成 {{ doneCount }} 个</span>
            <span v-if="checkedCount"> · 已检查 {{ checkedCount }} 个</span>
            · 并发 {{ settings.config?.generation.max_concurrency ?? '—' }}
          </span>
        </div>

        <Alert v-if="concurrencyWarning" variant="warning">
          <template #icon><TriangleAlert class="h-4 w-4 shrink-0" /></template>
          {{ concurrencyWarning }}
        </Alert>

        <div class="flex flex-wrap gap-2">
          <Button class="min-w-[10rem] flex-1" :disabled="!selectedNames.length || !workspaceSet || busy" @click="startParse">
            <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
            <ScanText v-else class="h-4 w-4" />
            {{ busy ? '处理中…' : '开始解析' }}
          </Button>
          <Button
            class="min-w-[10rem] flex-1"
            variant="secondary"
            :disabled="!checkableSelected || !workspaceSet || busy"
            @click="startCheck"
          >
            <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
            <ShieldCheck v-else class="h-4 w-4" />
            {{ busy ? '处理中…' : '开始检查' }}
          </Button>
        </div>
      </CardContent>
      <CardFooter>
        <span class="text-xs text-muted-foreground">
          解析输出：<code class="text-xs">03_parsed_json/&lt;文件基名&gt;.json</code>；
          检查输出：<code class="text-xs">&lt;文件基名&gt;_checked.json</code>（下游流程优先读取 _checked 版本）。
        </span>
      </CardFooter>
    </Card>

    <!-- 解析进度（每文件一行） -->
    <Card v-if="fileJobs.length">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><ScanText class="h-5 w-5" />解析 / 检查进度</CardTitle>
        <CardDescription>
          每个文件一个独立任务：待处理 / 解析中 / 检查中 / 已完成 / 失败 / 已取消；一个文件失败不影响其他。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <!-- 性能指标（顶部 3 卡）：并发数（当前配置）/ 吞吐量（各运行中窗口「近 10 秒平均」字/s 之和，随 SSE 实时刷新）/ 处理速度（累计已处理字÷累计处理耗时，每完成一段刷新、段间恒定） -->
        <div class="grid gap-3 sm:grid-cols-3">
          <div class="rounded-md border bg-muted/30 px-3 py-2">
            <div class="text-xs text-muted-foreground">并发数</div>
            <div class="mt-0.5 text-lg font-semibold tabular-nums">{{ effectiveConcurrency }}</div>
          </div>
          <div class="rounded-md border bg-muted/30 px-3 py-2">
            <div class="text-xs text-muted-foreground" title="近 10 秒平均：各运行中窗口「近 10 秒生成字符 ÷ 对应秒数」之和（后端按真实流式字符计算，平滑不抖动）">吞吐量（字/s）</div>
            <div class="mt-0.5 text-lg font-semibold tabular-nums">{{ Math.round(totalTps) }}</div>
          </div>
          <div class="rounded-md border bg-muted/30 px-3 py-2">
            <div class="text-xs text-muted-foreground" title="累计平均：Σ已完成源字符 ÷ Σ累计处理耗时（自批次开始；每完成一段刷新、段间恒定）">处理速度（字/s）</div>
            <div class="mt-0.5 text-lg font-semibold tabular-nums">{{ Math.round(speedCps) }}</div>
          </div>
        </div>

        <div v-for="row in jobRows" :key="row.taskId" class="space-y-2 rounded-md border p-3">
          <div class="flex items-center gap-3">
            <span class="min-w-0 flex-1 truncate text-sm font-medium" :title="row.name">{{ row.name }}</span>
            <Badge :variant="row.state.variant">{{ row.state.label }}</Badge>
            <span
              class="shrink-0 text-xs tabular-nums"
              :class="['解析中', '检查中'].includes(row.state.label) ? 'text-primary' : 'text-muted-foreground'"
              title="本窗口近 10 秒平均生成速度（字/s，按 LLM 流式输出实测）"
            >{{ ['解析中', '检查中'].includes(row.state.label) ? `${Math.round(row.task?.llm_cps_10s ?? 0)} 字/s` : '—' }}</span>
            <span class="shrink-0 w-10 text-right text-xs text-muted-foreground">
              {{ Math.round(row.progress * 100) }}%
            </span>
            <Button v-if="row.active" variant="outline" size="sm" @click="cancelJob(row.task)">
              <XCircle class="h-3.5 w-3.5" />取消
            </Button>
            <Button
              v-else-if="row.state.label === '已完成'"
              variant="outline"
              size="sm"
              @click="downloadJob(row)"
            >
              <Download class="h-3.5 w-3.5" />下载 JSON
            </Button>
          </div>

          <Progress :value="row.progress" />

          <div class="flex items-stretch gap-3">
            <!-- 左：解析进度日志（现有窗口，行为不变；任务结束后收起，把整行让给右侧） -->
            <div :class="row.active ? 'min-w-0 flex-1' : 'hidden'">
              <LiveLogPanel
                v-if="row.active"
                :task="row.task ?? null"
                :show-progress="false"
                :max-height-class="'h-40'"
              />
            </div>
            <!-- 右：流式反馈（LLM 原始输出，实时追加；完成后保留以便回看） -->
            <div class="min-w-0 flex-1">
              <LiveStreamPanel
                :task="row.task ?? null"
                :max-height-class="'h-40'"
              />
            </div>
          </div>

          <p v-if="row.state.label === '失败'" class="text-xs text-destructive">
            {{ row.error || '解析失败' }}
          </p>
        </div>
      </CardContent>
    </Card>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
