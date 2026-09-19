<script setup lang="ts">
// 背景音乐（阶段 7）：章节气氛分析（LLM 缓存）→ 匹配（标签评分 / 随机）→ 最终混音。
// 行 / 派生 / SSE / F5 全复刻 Merge.vue 范本：任务按 label 尾部「：{stem}」派生式重挂
// （无本地 job 列表、不依赖后端注册表），终态经 getter 式 watch 驱动行刷新。
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { analyzeChapters, bgmFileUrl, getChapters, matchChapters, mixChapters, updateChapter } from '@/api/bgm'
import { getLibrary, musicPreviewUrl } from '@/api/music'
import { downloadFile } from '@/utils/fileops'
import type {
  BgmChapterRow,
  MusicLibrary,
  MusicTagCategory,
  TaskSnapshot,
  TrackTags,
} from '@/types'

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
import Switch from '@/components/ui/Switch.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Download,
  Eraser,
  ListChecks,
  Loader2,
  Lock,
  LockOpen,
  Music,
  Music4,
  Pencil,
  RefreshCw,
  Shuffle,
  Wand2,
  XCircle,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

// ---------------------------------------------------------------------------
// 行数据（磁盘口径：02 章节 stem + 两个 08_bgm JSON 缓存 + 06/08 存在性）
// ---------------------------------------------------------------------------
const rowsData = ref<BgmChapterRow[]>([])
const mode = ref('llm')
const lib = ref<MusicLibrary | null>(null)
const loading = ref(false)
const loadError = ref('')
const error = ref('')
const selected = reactive<Record<string, boolean>>({})
const submitting = ref(false)

// ---------------------------------------------------------------------------
// 行任务派生：bgm-analysis / bgm-mix 任务按 label 尾部「：{stem}」归位。
// 在途（非终态）取 seq 升序首个；失败取 seq 降序最新（重试走同一任务 id）。
// ---------------------------------------------------------------------------
const TERMINAL = new Set(['cancelled', 'succeeded', 'failed'])
const BGM_MODULES = ['bgm-analysis', 'bgm-mix']

function stemOfLabel(label: string): string {
  // 与后端 _inflight_bgm_stems 的 re.search(r"：(.+)$") 同一口径：取第一个「：」。
  const i = label.indexOf('：')
  return i >= 0 ? label.slice(i + 1) : ''
}

const tasksByStem = computed(() => {
  const active = new Map<string, TaskSnapshot>()
  const failed = new Map<string, TaskSnapshot>()
  for (const t of taskStore.tasks) {
    if (!BGM_MODULES.includes(t.module)) continue
    const stem = stemOfLabel(t.label)
    if (!stem) continue
    if (t.status === 'failed') {
      const cur = failed.get(stem)
      if (!cur || t.seq > cur.seq) failed.set(stem, t)
    } else if (!TERMINAL.has(t.status)) {
      const cur = active.get(stem)
      if (!cur || t.seq < cur.seq) active.set(stem, t)
    }
  }
  return { active, failed }
})

type RowVariant = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'

interface BgmRow {
  stem: string
  data: BgmChapterRow
  task: TaskSnapshot | undefined // 混音在途优先于分析在途
  mixTask: TaskSnapshot | undefined
  analysisTask: TaskSnapshot | undefined
  failedTask: TaskSnapshot | undefined
  label: string
  variant: RowVariant
  matched: boolean
  mixable: boolean
  mixLabel: string
}

// 行状态优先级：混音中 > 分析中 > 未合并（narration 缺失）> 已混音（无 BGM 章
// copy2 后同样命中）> ⚠ 已删除 > 已匹配 > 无 BGM（匹配过判无）> 已分析 > 未分析。
const rows = computed<BgmRow[]>(() => {
  const { active, failed } = tasksByStem.value
  return rowsData.value.map((d) => {
    const a = active.get(d.stem)
    const mixTask = a?.module === 'bgm-mix' ? a : undefined
    const analysisTask = a?.module === 'bgm-analysis' ? a : undefined
    const task = mixTask ?? analysisTask
    const asg = d.assignment
    const music = asg?.music ?? null
    const missing = d.music_missing
    let label: string
    let variant: RowVariant
    if (mixTask) {
      label = '混音中'
      variant = 'secondary'
    } else if (analysisTask) {
      label = '分析中'
      variant = 'secondary'
    } else if (!d.narration_exists) {
      label = '未合并'
      variant = 'destructive'
    } else if (d.mix_exists) {
      label = music ? '已混音' : '已混音 / 无 BGM'
      variant = 'success'
    } else if (missing) {
      label = '⚠ 已删除'
      variant = 'destructive'
    } else if (asg && music) {
      label = '已匹配'
      variant = 'default'
    } else if (asg) {
      label = '无 BGM'
      variant = 'secondary'
    } else if (d.analysis) {
      label = '已分析'
      variant = 'outline'
    } else {
      label = '未分析'
      variant = 'secondary'
    }
    const mixable =
      !!d.narration_exists && !!asg && !(music && missing) && !task
    const mixLabel = music ? '混音' : '无 BGM · 复制原声'
    return {
      stem: d.stem,
      data: d,
      task,
      mixTask,
      analysisTask,
      failedTask: task ? undefined : failed.get(d.stem),
      label,
      variant,
      matched: !!asg,
      mixable,
      mixLabel,
    }
  })
})

const selectedNames = computed(() => rowsData.value.filter((r) => !!selected[r.stem]).map((r) => r.stem))
const matchedRows = computed(() => rows.value.filter((r) => r.matched))
const mixedCount = computed(() => rowsData.value.filter((r) => r.mix_exists).length)
// 「已匹配 且 未混音 且 已合并」（且非已删除）= 可直接混音的行。
const mixReadyStems = computed(
  () => rows.value.filter((r) => r.matched && !r.data.mix_exists && r.mixable).map((r) => r.stem),
)
const bgmActive = computed(
  () => taskStore.activeTasks('bgm-analysis').concat(taskStore.activeTasks('bgm-mix')),
)

// ---------------------------------------------------------------------------
// 刷新（磁盘口径）
// ---------------------------------------------------------------------------
async function refreshRows() {
  loading.value = true
  loadError.value = ''
  try {
    const [res, libRes] = await Promise.all([getChapters(), getLibrary()])
    rowsData.value = res.chapters
    mode.value = res.mode || 'llm'
    lib.value = libRes
    for (const k of Object.keys(selected)) {
      if (!rowsData.value.some((r) => r.stem === k)) delete selected[k]
    }
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// 工具栏
// ---------------------------------------------------------------------------
function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}
function onSelectChange(stem: string, e: Event) {
  if ((e.target as HTMLInputElement).checked) selected[stem] = true
  else delete selected[stem]
}
function selectMixReady() {
  const ready = mixReadyStems.value
  if (!ready.length) return
  const all = ready.every((s) => !!selected[s])
  if (!all) clearSelection()
  for (const s of ready) {
    if (all) delete selected[s]
    else selected[s] = true
  }
}
function selectAllAll() {
  if (!rowsData.value.length) return
  clearSelection()
  const all = rowsData.value.every((r) => !!selected[r.stem])
  if (!all) for (const r of rowsData.value) selected[r.stem] = true
}

// ---------------------------------------------------------------------------
// 提交 / 取消 / 重试
// ---------------------------------------------------------------------------
async function doAnalyze() {
  if (!selectedNames.value.length || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await analyzeChapters(selectedNames.value)
    await taskStore.refresh()
  } catch (e: any) {
    error.value = e?.message || '启动失败'
  } finally {
    submitting.value = false
  }
}

async function doMix() {
  if (!selectedNames.value.length || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await mixChapters(selectedNames.value)
    await taskStore.refresh()
  } catch (e: any) {
    const msg = e?.message || '启动失败'
    error.value = msg
    if (msg.includes('在途')) toast({ title: '提交被拒绝', variant: 'destructive', description: msg })
  } finally {
    submitting.value = false
  }
}

async function doMixRow(stem: string) {
  try {
    await mixChapters([stem])
    await taskStore.refresh()
  } catch (e: any) {
    toast({ title: '混音启动失败', variant: 'destructive', description: e?.message })
  }
}

function cancelRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'cancel')
}
function retryRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'retry')
}
function cancelAll() {
  // 无专用 cancel-batch 端点：逐任务 cancel（PENDING 壳就地终结 + RUNNING 协作取消）。
  for (const t of bgmActive.value) taskStore.control(t.id, 'cancel')
}

// ---------------------------------------------------------------------------
// 行操作
// ---------------------------------------------------------------------------
async function doAnalyzeRow(stem: string) {
  try {
    await analyzeChapters([stem])
    await taskStore.refresh()
  } catch (e: any) {
    toast({ title: '分析启动失败', variant: 'destructive', description: e?.message })
  }
}

const matching = ref(false)
const matchNote = ref('')

async function doRematch(stem: string) {
  if (matching.value) return
  matching.value = true
  try {
    const r = await matchChapters([stem], mode.value)
    matchNote.value = `匹配完成：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({ title: '重匹配完成', variant: 'success', description: `${stem}（${r.matched} 命中 / ${r.no_bgm} 无 BGM）` })
    await refreshRows()
  } catch (e: any) {
    toast({ title: '匹配失败', variant: 'destructive', description: e?.message })
  } finally {
    matching.value = false
  }
}

// 模式切换 = 立即全量重匹配（assignments.mode 持久化在后端）。
async function switchMode(m: string) {
  if (m === mode.value || matching.value) return
  matching.value = true
  try {
    const r = await matchChapters(null, m)
    mode.value = m
    matchNote.value = `已切换为「${m === 'random' ? '全章节随机' : 'LLM 标签匹配'}」并重匹配：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({ title: '重匹配完成', variant: 'success', description: matchNote.value })
    await refreshRows()
  } catch (e: any) {
    toast({ title: '匹配失败', variant: 'destructive', description: e?.message })
  } finally {
    matching.value = false
  }
}

async function doLock(stem: string, locked: boolean) {
  try {
    await updateChapter(stem, { locked })
    toast({ title: locked ? '已锁定' : '已解锁', variant: 'default', description: stem })
    await refreshRows()
  } catch (e: any) {
    toast({ title: '操作失败', variant: 'destructive', description: e?.message })
  }
}

function downloadRow(stem: string) {
  downloadFile('08_bgm', `${stem}.mp3`)
}

// ---------------------------------------------------------------------------
// 手动选曲弹层（enabled 曲目 + 试听 + 锁定 checkbox）
// ---------------------------------------------------------------------------
const manualStem = ref<string | null>(null)
const manualPick = ref<string | null>(null)
const manualLock = ref(false)
const manualBusy = ref(false)

function openManual(row: BgmRow) {
  manualStem.value = row.stem
  manualPick.value = row.data.assignment?.music ?? null
  manualLock.value = row.data.assignment?.locked ?? false
}

const manualTracks = computed(() =>
  Object.entries(lib.value?.tracks ?? {})
    .map(([name, tr]) => ({ name, ...tr }))
    .filter((tr) => tr.enabled)
    .sort((a, b) => a.name.localeCompare(b.name)),
)

async function saveManual() {
  if (!manualStem.value || manualBusy.value) return
  manualBusy.value = true
  try {
    await updateChapter(manualStem.value, {
      music: manualPick.value,
      locked: manualLock.value,
      __setMusic: true,
    })
    toast({ title: '已手动选曲', variant: 'success', description: manualPick.value || '清除（无 BGM）' })
    manualStem.value = null
    await refreshRows()
  } catch (e: any) {
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    manualBusy.value = false
  }
}

// ---------------------------------------------------------------------------
// 编辑标签弹层（四类分桶 + 自定义；写入 analysis，edited:true）
// ---------------------------------------------------------------------------
const editStem = ref<string | null>(null)
const editTags = reactive<TrackTags>({ scene: [], mood: [], emotion: [], custom: [] })
const editCustom = ref('')
const editBusy = ref(false)

function openEdit(row: BgmRow) {
  editStem.value = row.stem
  const src = row.data.analysis ?? row.data.assignment?.tags ?? { scene: [], mood: [], emotion: [], custom: [] }
  for (const c of ['scene', 'mood', 'emotion', 'custom'] as MusicTagCategory[]) {
    editTags[c] = [...(src[c] ?? [])]
  }
  editCustom.value = ''
}

function toggleEditTag(cat: MusicTagCategory, name: string) {
  const i = editTags[cat].indexOf(name)
  if (i >= 0) editTags[cat].splice(i, 1)
  else editTags[cat].push(name)
}

function addEditCustom() {
  const v = editCustom.value.trim()
  if (v && !editTags.custom.includes(v)) editTags.custom.push(v)
  editCustom.value = ''
}

async function saveEdit() {
  if (!editStem.value || editBusy.value) return
  editBusy.value = true
  try {
    await updateChapter(editStem.value, { tags: { ...editTags } })
    toast({ title: '标签已更新', variant: 'success', description: editStem.value })
    editStem.value = null
    await refreshRows()
  } catch (e: any) {
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    editBusy.value = false
  }
}

// ---------------------------------------------------------------------------
// 音频参数（config.bgm fire-and-forget 保存，分集页先例）
// ---------------------------------------------------------------------------
const draft = reactive({
  volume: 0.18,
  fade_in: 1.5,
  fade_out: 3.0,
  loop: true,
  min_match_score: 1,
  analysis_chars: 6000,
})
const paramsSaved = ref(false)
const paramsSaving = ref(false)

function seedDraft() {
  const b = settings.config?.bgm
  if (!b) return
  draft.volume = b.volume
  draft.fade_in = b.fade_in
  draft.fade_out = b.fade_out
  draft.loop = b.loop
  draft.min_match_score = b.min_match_score
  draft.analysis_chars = b.analysis_chars
}

async function saveParams() {
  if (paramsSaving.value) return
  paramsSaving.value = true
  try {
    await settings.save({ bgm: { ...draft } })
    paramsSaved.value = true
    setTimeout(() => (paramsSaved.value = false), 2000)
  } catch (e: any) {
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    paramsSaving.value = false
  }
}

// ---------------------------------------------------------------------------
// SSE 驱动（getter 式 watch）：终态任务 → toast + 重拉磁盘口径。
// processed 集合防重复（快照重放 / 断线重连）；转回非终态（重试）时释放。
// ---------------------------------------------------------------------------
const processed = new Set<string>()
let watcherArmed = false

watch(
  () =>
    taskStore.tasks
      .filter((t) => BGM_MODULES.includes(t.module))
      .map((t) => `${t.id}:${t.status}`)
      .join('|'),
  () => {
    if (!watcherArmed) {
      watcherArmed = true
      for (const t of taskStore.tasks) {
        if (BGM_MODULES.includes(t.module) && TERMINAL.has(t.status)) processed.add(t.id)
      }
      return
    }
    for (const t of taskStore.tasks) {
      if (!BGM_MODULES.includes(t.module)) continue
      if (!TERMINAL.has(t.status)) {
        processed.delete(t.id)
        continue
      }
      if (processed.has(t.id)) continue
      processed.add(t.id)
      if (t.status === 'succeeded') {
        const stem = stemOfLabel(t.label)
        toast({
          title: t.module === 'bgm-mix' ? '混音完成' : '章节分析完成',
          variant: 'success',
          description: stem,
        })
        void refreshRows()
      }
    }
  },
)

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  seedDraft()
  await taskStore.refresh()
  await refreshRows()
})

// keep-alive 缓存页：重新进入时刷新磁盘口径。
onActivated(() => {
  if (settings.loaded) void refreshRows()
})

const TAG_CATS: { key: MusicTagCategory; label: string; cls: string }[] = [
  { key: 'scene', label: '场景', cls: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30' },
  { key: 'mood', label: '气氛', cls: 'bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30' },
  { key: 'emotion', label: '情绪', cls: 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30' },
  { key: 'custom', label: '自定义', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30' },
]
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        <Music4 class="h-6 w-6" />背景音乐
      </h1>
      <p class="mt-1 text-muted-foreground">
        章节气氛分析（LLM，缓存于
        <code class="text-xs">08_bgm/chapter_music_analysis.json</code>）→ 匹配（标签评分 / 全章节随机）→
        最终混音（<code class="text-xs">06_audio_merge</code> 旁白 + 音乐库曲目 →
        <code class="text-xs">08_bgm/&lt;章&gt;.mp3</code>；无 BGM 章直接复制原声）。
      </p>
    </div>

    <WorkspaceGateAlert />

    <!-- 模式 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Shuffle class="h-5 w-5" />匹配模式</CardTitle>
        <CardDescription>
          切换模式立即对全部章节重匹配（已锁定章节整体跳过）。「LLM 段落级匹配」为后续版本功能。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div class="flex flex-wrap gap-4">
          <label class="flex cursor-pointer items-center gap-2 text-sm">
            <input type="radio" :checked="mode === 'llm'" :disabled="matching" class="h-4 w-4 accent-primary" @change="switchMode('llm')" />
            LLM 标签匹配
          </label>
          <label class="flex cursor-pointer items-center gap-2 text-sm">
            <input type="radio" :checked="mode === 'random'" :disabled="matching" class="h-4 w-4 accent-primary" @change="switchMode('random')" />
            全章节随机
          </label>
          <label class="flex cursor-not-allowed items-center gap-2 text-sm text-muted-foreground" title="后续版本">
            <input type="radio" value="segment" disabled class="h-4 w-4" />
            LLM 段落级匹配
            <Badge variant="secondary">后续版本</Badge>
          </label>
        </div>
        <p v-if="matchNote" class="text-xs text-muted-foreground">{{ matchNote }}</p>
      </CardContent>
    </Card>

    <!-- 音频参数 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Music class="h-5 w-5" />音频参数</CardTitle>
        <CardDescription>混音音量 / 淡入淡出 / 循环策略（混音时 fade 自动钳制到时长一半以内）；匹配最低分。</CardDescription>
      </CardHeader>
      <CardContent class="flex flex-wrap items-end gap-4">
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          BGM 音量（0~1）
          <input v-model.number="draft.volume" type="number" min="0" max="1" step="0.01"
            class="h-8 w-20 rounded-md border border-input bg-background px-2 text-sm" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          淡入（秒）
          <input v-model.number="draft.fade_in" type="number" min="0" step="0.1"
            class="h-8 w-20 rounded-md border border-input bg-background px-2 text-sm" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          淡出（秒）
          <input v-model.number="draft.fade_out" type="number" min="0" step="0.1"
            class="h-8 w-20 rounded-md border border-input bg-background px-2 text-sm" />
        </label>
        <label class="flex items-center gap-2 text-sm">
          循环铺满
          <Switch :model-value="draft.loop" @update:model-value="draft.loop = $event" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          匹配最低分
          <input v-model.number="draft.min_match_score" type="number" min="1" step="1"
            class="h-8 w-20 rounded-md border border-input bg-background px-2 text-sm" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          LLM 采样字数
          <input v-model.number="draft.analysis_chars" type="number" min="500" step="500"
            class="h-8 w-24 rounded-md border border-input bg-background px-2 text-sm" />
        </label>
        <Button variant="outline" size="sm" :disabled="paramsSaving" @click="saveParams">
          <Loader2 v-if="paramsSaving" class="h-3.5 w-3.5 animate-spin" />
          {{ paramsSaved ? '已保存' : '保存参数' }}
        </Button>
      </CardContent>
    </Card>

    <!-- 章节匹配结果 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Music4 class="h-5 w-5" />章节匹配结果</CardTitle>
        <CardDescription>
          行状态：混音中 · 分析中 · 已混音（无 BGM 章 = 已混音 / 无 BGM）· 已匹配 · 无 BGM · 已分析 · 未分析；
          「未合并」= 06 旁白缺失（混音禁用）；「⚠ 已删除」= 所指曲目已从音乐库删除（混音禁用，可重匹配恢复）。
          【全选】只勾「已匹配 且 未混音 且 已合并」行；【全量全选】无视状态。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="loadError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ loadError }}
        </Alert>

        <div v-else-if="rows.length" class="max-h-[28rem] space-y-1 overflow-y-auto rounded-md border p-2">
          <div v-for="row in rows" :key="row.stem" class="rounded px-2 py-1.5 hover:bg-accent/50">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <label class="flex min-w-0 flex-1 basis-52 cursor-pointer items-center gap-2">
                <input type="checkbox" class="h-4 w-4 shrink-0 accent-primary"
                  :checked="!!selected[row.stem]"
                  :disabled="submitting || !!row.task"
                  @change="onSelectChange(row.stem, $event)" />
                <span class="min-w-0 truncate text-sm font-medium" :title="row.stem">{{ row.stem }}</span>
              </label>
              <!-- 标签（analysis 实时值；无 analysis 时回退匹配快照） -->
              <div class="flex flex-wrap gap-1">
                <template v-for="cat in TAG_CATS" :key="cat.key">
                  <Badge
                    v-for="t in (row.data.analysis ?? row.data.assignment?.tags ?? {})[cat.key] ?? []"
                    :key="cat.key + t"
                    variant="outline"
                    :class="cat.cls"
                  >{{ t }}</Badge>
                </template>
              </div>
              <!-- BGM -->
              <div class="flex min-w-0 items-center gap-2" style="max-width: 16rem">
                <template v-if="row.data.assignment?.music">
                  <span class="min-w-0 truncate text-xs" :class="{ 'text-destructive line-through': row.data.music_missing }"
                    :title="row.data.music_missing ? '曲目已从音乐库删除——重匹配可恢复' : row.data.assignment.music">
                    {{ row.data.assignment.music }}
                  </span>
                  <MiniAudioPlayer v-if="!row.data.music_missing" :src="musicPreviewUrl(row.data.assignment.music)" />
                  <Badge v-else variant="destructive">已删除</Badge>
                </template>
                <span v-else-if="row.data.assignment" class="text-xs text-muted-foreground">—（无 BGM）</span>
                <span v-else class="text-xs text-muted-foreground">—</span>
              </div>
              <!-- 匹配分 -->
              <span class="w-14 shrink-0 text-right text-xs tabular-nums text-muted-foreground"
                :title="row.data.assignment?.reason || ''">
                {{ row.data.assignment?.score ?? '—' }}
              </span>
              <Badge :variant="row.variant" class="shrink-0">{{ row.label }}</Badge>
              <!-- 操作 -->
              <div class="flex shrink-0 items-center gap-1.5">
                <template v-if="row.task">
                  <Progress :value="row.task.progress" class="h-1.5 w-20 sm:w-24" />
                  <span class="w-9 text-right text-xs tabular-nums text-muted-foreground">
                    {{ Math.round(row.task.progress * 100) }}%
                  </span>
                  <Button variant="outline" size="sm" @click="cancelRow(row.task)">
                    <XCircle class="h-3.5 w-3.5" />取消
                  </Button>
                </template>
                <template v-else>
                  <Button v-if="row.failedTask" variant="outline" size="sm" @click="retryRow(row.failedTask)">
                    <RefreshCw class="h-3.5 w-3.5" />重试
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :disabled="!workspaceSet || !row.mixable || submitting"
                    @click="doMixRow(row.stem)"
                    :title="!row.mixable
                      ? (row.data.narration_exists
                          ? (row.data.music_missing ? '曲目已删除——请重匹配' : '该章从未匹配——请先匹配')
                          : '06 旁白缺失——请先完成音频合并')
                      : '混音本章'"
                  >
                    {{ row.mixLabel }}
                  </Button>
                  <Button
                    v-if="row.data.mix_exists"
                    variant="outline"
                    size="sm"
                    @click="downloadRow(row.stem)"
                  >
                    <Download class="h-3.5 w-3.5" />下载
                  </Button>
                  <MiniAudioPlayer v-if="row.data.mix_exists" :src="bgmFileUrl(row.stem)" />
                  <Button variant="outline" size="sm" :disabled="!workspaceSet || submitting" @click="doAnalyzeRow(row.stem)">
                    <Wand2 class="h-3.5 w-3.5" />分析
                  </Button>
                  <Button variant="outline" size="sm" :disabled="!workspaceSet || matching" @click="doRematch(row.stem)">
                    <Shuffle class="h-3.5 w-3.5" />重匹配
                  </Button>
                  <Button variant="outline" size="sm" @click="openManual(row)">
                    <Music class="h-3.5 w-3.5" />选曲
                  </Button>
                  <Button variant="outline" size="sm" @click="openEdit(row)">
                    <Pencil class="h-3.5 w-3.5" />标签
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :disabled="!workspaceSet"
                    @click="doLock(row.stem, !(row.data.assignment?.locked ?? false))"
                  >
                    <Lock v-if="row.data.assignment?.locked" class="h-3.5 w-3.5" />
                    <LockOpen v-else class="h-3.5 w-3.5" />
                    {{ row.data.assignment?.locked ? '解锁' : '锁定' }}
                  </Button>
                </template>
              </div>
            </div>
            <p v-if="row.data.assignment?.reason" class="mt-0.5 pl-7 text-xs text-muted-foreground">
              {{ row.data.assignment.reason }}
            </p>
            <p v-if="row.failedTask" class="mt-1 pl-7 text-xs text-destructive">{{ row.failedTask.error || '任务失败' }}</p>
            <div v-if="row.task && (row.task.status === 'running' || row.task.status === 'paused')" class="mt-2">
              <LiveLogPanel :task="row.task" :max-height-class="'h-40'" />
            </div>
          </div>
        </div>
        <p v-else class="text-sm text-muted-foreground">
          02_split_text/ 下暂无章节文件——请先到「排版与分册」完成分册。
        </p>

        <div class="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" :disabled="submitting || !rows.length" @click="selectMixReady">
            <ListChecks class="h-3.5 w-3.5" />{{ mixReadyStems.length && mixReadyStems.every((s) => !!selected[s]) ? '已选' : '全选' }}
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || !rows.length" @click="selectAllAll">
            <ListChecks class="h-3.5 w-3.5" />{{ rows.length && rows.every((r) => !!selected[r.stem]) ? '已全选' : '全量全选' }}
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || !rows.length" @click="clearSelection">
            <Eraser class="h-3.5 w-3.5" />清空
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || loading" @click="refreshRows">
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
          </Button>
          <span class="ml-auto text-xs text-muted-foreground">
            已选 {{ selectedNames.length }} / {{ rows.length }} 章
            <span v-if="matchedRows.length"> · 已匹配 {{ matchedRows.length }}</span>
            <span v-if="mixedCount"> · 已混音 {{ mixedCount }}</span>
          </span>
        </div>

        <div class="flex flex-wrap gap-2">
          <Button
            variant="outline"
            class="min-w-[11rem] flex-1"
            :disabled="!workspaceSet || submitting || !selectedNames.length"
            @click="doAnalyze"
          >
            <Wand2 class="h-4 w-4" />
            分析所选（{{ selectedNames.length }} 章）
          </Button>
          <Button
            class="min-w-[11rem] flex-1"
            :disabled="!workspaceSet || submitting || !selectedNames.length"
            @click="doMix"
          >
            <Loader2 v-if="submitting" class="h-4 w-4 animate-spin" />
            <Music4 v-else class="h-4 w-4" />
            混音所选（{{ selectedNames.length }} 章）
          </Button>
          <Button v-if="bgmActive.length" variant="destructive" @click="cancelAll">
            <XCircle class="h-4 w-4" />取消全部
          </Button>
        </div>
      </CardContent>
      <CardFooter class="justify-start">
        <span class="text-xs text-muted-foreground">
          分析 = 每章一个 LLM 任务（共享并发闸）；混音 = 每章一个任务（ffmpeg/CPU 闸，上限按核心数评估）；
          行内【分析】/【重匹配】单章执行；锁定章节任何重匹配都不会改动。
        </span>
      </CardFooter>
    </Card>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>

    <!-- 手动选曲弹层 -->
    <div
      v-if="manualStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="manualStem = null"
    >
      <div class="w-full max-w-lg rounded-xl border bg-background p-4 shadow-lg">
        <h2 class="text-lg font-semibold">手动选曲：{{ manualStem }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">
          仅列出启用的曲目；选择后该章标记为「手动指定」（未锁定仍会被重匹配覆盖，锁定后重匹配跳过）。
        </p>
        <ScrollArea class="mt-3 h-72 rounded-md border">
          <div class="space-y-1 p-2">
            <label class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50">
              <input type="radio" :value="null" v-model="manualPick" class="h-4 w-4 accent-primary" />
              <span class="text-sm text-muted-foreground">无 BGM（混音时直接复制原声）</span>
            </label>
            <label
              v-for="tr in manualTracks"
              :key="tr.name"
              class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50"
            >
              <input type="radio" :value="tr.name" v-model="manualPick" class="h-4 w-4 accent-primary" />
              <span class="min-w-0 flex-1 truncate text-sm" :title="tr.name">{{ tr.name }}</span>
              <MiniAudioPlayer :src="musicPreviewUrl(tr.name)" />
            </label>
            <p v-if="!manualTracks.length" class="px-2 py-3 text-xs text-muted-foreground">
              音乐库中没有启用的曲目——请先到「音乐库」上传并启用。
            </p>
          </div>
        </ScrollArea>
        <label class="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="manualLock" class="h-4 w-4 accent-primary" />
          锁定（重匹配时整章跳过）
        </label>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="manualStem = null">关闭</Button>
          <Button size="sm" :disabled="manualBusy" @click="saveManual">
            <Loader2 v-if="manualBusy" class="h-3.5 w-3.5 animate-spin" />
            确认
          </Button>
        </div>
      </div>
    </div>

    <!-- 编辑标签弹层 -->
    <div
      v-if="editStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="editStem = null"
    >
      <div class="w-full max-w-lg rounded-xl border bg-background p-4 shadow-lg">
        <h2 class="text-lg font-semibold">编辑章节标签：{{ editStem }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">
          写入章节分析缓存（标记「手动编辑」）；改标签后请【重匹配】让新标签生效。
        </p>
        <div class="mt-3 space-y-3">
          <div v-for="cat in TAG_CATS" :key="cat.key" class="space-y-1">
            <p class="text-xs font-medium text-muted-foreground">{{ cat.label }}</p>
            <div class="flex flex-wrap gap-1">
              <button
                v-for="t in (lib?.tags ?? {})[cat.key] ?? []"
                :key="t"
                type="button"
                class="rounded-md border px-2 py-0.5 text-xs transition-colors"
                :class="editTags[cat.key].includes(t)
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border text-muted-foreground hover:bg-accent/50'"
                @click="toggleEditTag(cat.key, t)"
              >{{ t }}</button>
              <button
                v-if="cat.key === 'custom'"
                type="button"
                class="rounded-md border border-dashed border-border px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent/50"
                @click="addEditCustom"
              >+ {{ editCustom || '自定义标签' }}</button>
              <span v-if="!((lib?.tags ?? {})[cat.key] ?? []).length && !editTags[cat.key].length" class="text-xs text-muted-foreground">（空）</span>
            </div>
            <div v-if="cat.key === 'custom'" class="flex gap-2">
              <input
                v-model="editCustom"
                type="text"
                placeholder="输入自定义标签后点上方 + 添加"
                class="h-7 flex-1 rounded-md border border-input bg-background px-2 text-xs"
                @keyup.enter="addEditCustom"
              />
            </div>
          </div>
          <p v-if="editTags.custom.length" class="text-xs text-muted-foreground">
            已选自定义：{{ editTags.custom.join('、') }}
          </p>
        </div>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="editStem = null">关闭</Button>
          <Button size="sm" :disabled="editBusy" @click="saveEdit">
            <Loader2 v-if="editBusy" class="h-3.5 w-3.5 animate-spin" />
            保存
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
