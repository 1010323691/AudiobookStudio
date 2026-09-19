<script setup lang="ts">
// 音乐库（全局资源，工作空间外、跨工程共享）——曲目上传 / 试听 / 打标 / AI 推荐 /
// 批量操作 / 标签管理。页面不经 WorkspaceGateAlert（与工作空间无关）。
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useToast } from '@/components/ui/toast'
import { useTaskStore } from '@/stores/task'
import {
  batchDelete,
  batchEnable,
  batchTags,
  createTag,
  deleteTag,
  deleteTrack,
  getLibrary,
  musicPreviewUrl,
  renameTag,
  suggestTags,
  suggestTagsBatch,
  updateTrack,
  uploadMusic,
} from '@/api/music'
import { pickFiles } from '@/utils/fileops'
import { formatDuration } from '@/utils/format'
import type { MusicLibrary, MusicSuggestion, MusicTagCategory, TaskSnapshot, TrackTags } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Input from '@/components/ui/Input.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Switch from '@/components/ui/Switch.vue'
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import {
  Disc3,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Tags,
  Trash2,
  Upload,
  X,
} from 'lucide-vue-next'

const { push: toast } = useToast()

// ---------------------------------------------------------------------------
// 常量（标签四桶 + 展示名 + 颜色）
// ---------------------------------------------------------------------------

const CATEGORIES: MusicTagCategory[] = ['scene', 'mood', 'emotion', 'custom']
const CAT_LABEL: Record<MusicTagCategory, string> = {
  scene: '场景',
  mood: '气氛',
  emotion: '情绪',
  custom: '自定义',
}
const CAT_BADGE: Record<MusicTagCategory, string> = {
  scene: 'border-sky-500/30 bg-sky-500/15 text-sky-600 dark:text-sky-400',
  mood: 'border-violet-500/30 bg-violet-500/15 text-violet-600 dark:text-violet-400',
  emotion: 'border-rose-500/30 bg-rose-500/15 text-rose-600 dark:text-rose-400',
  custom: 'border-amber-500/30 bg-amber-500/15 text-amber-600 dark:text-amber-400',
}

// ---------------------------------------------------------------------------
// 数据
// ---------------------------------------------------------------------------

const lib = ref<MusicLibrary | null>(null)
const loading = ref(false)
const loadError = ref('')
const uploading = ref(false)
const search = ref('')
const tagFilter = ref('') // '' = 全部；否则 "category:name"
const selected = reactive<Record<string, boolean>>({})

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    lib.value = await getLibrary()
    // 清掉已消失曲目的勾选（仅 UI 状态，不触碰任何数据）。
    const names = new Set(Object.keys(lib.value.tracks))
    for (const k of Object.keys(selected)) if (!names.has(k)) delete selected[k]
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

const allTracks = computed(() =>
  Object.entries(lib.value?.tracks ?? {}).sort((a, b) => a[0].localeCompare(b[0])),
)
const trackList = computed(() => {
  const q = search.value.trim().toLowerCase()
  let list = allTracks.value
  if (q) list = list.filter(([n]) => n.toLowerCase().includes(q))
  if (tagFilter.value) {
    const [cat, ...rest] = tagFilter.value.split(':')
    const tag = rest.join(':')
    list = list.filter(
      ([, t]) => Array.isArray(t.tags?.[cat as MusicTagCategory]) && t.tags[cat as MusicTagCategory].includes(tag),
    )
  }
  return list
})
const enabledCount = computed(() => allTracks.value.filter(([, t]) => t.enabled).length)

const filterOptions = computed(() => {
  const out: { value: string; label: string }[] = []
  for (const c of CATEGORIES) {
    for (const t of lib.value?.tags?.[c] ?? []) out.push({ value: `${c}:${t}`, label: `${CAT_LABEL[c]} · ${t}` })
  }
  return out
})

// ---------------------------------------------------------------------------
// 多选
// ---------------------------------------------------------------------------

const visibleNames = computed(() => trackList.value.map(([n]) => n))
const allVisibleSelected = computed(
  () => visibleNames.value.length > 0 && visibleNames.value.every((n) => !!selected[n]),
)
const selectedNames = computed(() => Object.keys(selected))

function toggleSelectAll(e: Event) {
  const on = (e.target as HTMLInputElement).checked
  for (const n of visibleNames.value) {
    if (on) selected[n] = true
    else delete selected[n]
  }
}
function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}

// ---------------------------------------------------------------------------
// AI 识别任务派生（module music-ai-tags，label「AI 推荐标签：{name}」）
// 行任务按 label 尾部「：{name}」归位（与 BGM 页 stemOfLabel 同形）：
// 在途（非终态）取 seq 升序首个；失败取 seq 降序最新（重试走同一任务 id）。
// ---------------------------------------------------------------------------

const taskStore = useTaskStore()
const AI_MODULE = 'music-ai-tags'
const AI_TERMINAL = new Set(['cancelled', 'succeeded', 'failed'])

function aiTrackOfLabel(label: string): string {
  // 与后端 _inflight_ai_names 的 re.search(r"：(.+)$") 同一口径：取第一个「：」后全部。
  const i = label.indexOf('：')
  return i >= 0 ? label.slice(i + 1) : ''
}

const aiTasks = computed(() => {
  const active = new Map<string, TaskSnapshot>()
  const failed = new Map<string, TaskSnapshot>()
  for (const t of taskStore.tasks) {
    if (t.module !== AI_MODULE) continue
    const name = aiTrackOfLabel(t.label)
    if (!name) continue
    if (t.status === 'failed') {
      const cur = failed.get(name)
      if (!cur || t.seq > cur.seq) failed.set(name, t)
    } else if (!AI_TERMINAL.has(t.status)) {
      const cur = active.get(name)
      if (!cur || t.seq < cur.seq) active.set(name, t)
    }
  }
  return { active, failed }
})

function suggestionOf(name: string): MusicSuggestion | undefined {
  return lib.value?.suggestions?.[name]
}
function suggestionHasTags(name: string): boolean {
  const s = suggestionOf(name)
  return !!s && CATEGORIES.some((c) => (s.tags?.[c]?.length ?? 0) > 0)
}
function suggestionSummary(name: string): string {
  const s = suggestionOf(name)
  if (!s) return ''
  const parts = CATEGORIES.flatMap((c) => s.tags?.[c] ?? [])
  return parts.length ? parts.join('、') : '（无标签）'
}

// ---------------------------------------------------------------------------
// 上传（逐文件上传，逐文件拿 409/400 语义）
// ---------------------------------------------------------------------------

async function doUpload() {
  if (uploading.value) return
  uploading.value = true
  try {
    const files = await pickFiles()
    if (!files.length) return
    let ok = 0
    for (const f of files) {
      try {
        await uploadMusic(f)
        ok++
      } catch (e: any) {
        toast({
          title: '上传失败',
          variant: 'destructive',
          description: `${f.name}：${e?.message || '未知错误'}`,
        })
      }
    }
    if (ok) toast({ title: '上传完成', variant: 'success', description: `成功 ${ok} / ${files.length} 首` })
    await refresh()
  } finally {
    uploading.value = false
  }
}

// ---------------------------------------------------------------------------
// 行操作（启用开关 = 乐观更新；删除 / 批量）
// ---------------------------------------------------------------------------

async function toggleEnabled(name: string, value: boolean) {
  const tr = lib.value?.tracks[name]
  if (!tr) return
  const prev = tr.enabled
  tr.enabled = value
  try {
    const r = await updateTrack(name, { enabled: value })
    lib.value!.tracks[name] = r.track
  } catch (e: any) {
    tr.enabled = prev
    toast({ title: '操作失败', variant: 'destructive', description: e?.message || '保存失败' })
  }
}

async function doDeleteOne(name: string) {
  if (!window.confirm(`删除音乐「${name}」？（音乐文件与索引条目都会被删除）`)) return
  try {
    const r = await deleteTrack(name)
    if (r.skipped.length) {
      toast({
        title: '未删除（被锁定引用）',
        variant: 'default',
        description: r.skipped.map((s) => `${s.name}：${s.reason}`).join('；'),
      })
    } else {
      toast({ title: '已删除', variant: 'success', description: name })
    }
    delete selected[name]
    await refresh()
  } catch (e: any) {
    toast({ title: '删除失败', variant: 'destructive', description: e?.message || '' })
  }
}

// ---------------------------------------------------------------------------
// 批量操作条
// ---------------------------------------------------------------------------

const batchCat = ref<MusicTagCategory>('mood')
const batchTag = ref('')
const batchBusy = ref(false)

async function doBatchTag(op: 'add' | 'remove') {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  if (!batchTag.value) {
    toast({ title: '请先在标签下拉中选择标签', variant: 'default' })
    return
  }
  batchBusy.value = true
  try {
    await batchTags(names, [batchTag.value], batchCat.value, op)
    await refresh()
  } catch (e: any) {
    toast({ title: '批量打标失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

async function doBatchEnable(enabled: boolean) {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  batchBusy.value = true
  try {
    const r = await batchEnable(names, enabled)
    await refresh()
    if (r.missing.length) {
      toast({
        title: '部分曲目不存在',
        variant: 'default',
        description: r.missing.slice(0, 3).join('、') + (r.missing.length > 3 ? ' …' : ''),
      })
    }
  } catch (e: any) {
    toast({ title: '操作失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

async function doBatchDelete() {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  if (!window.confirm(`删除选中的 ${names.length} 首音乐？`)) return
  batchBusy.value = true
  try {
    const r = await batchDelete(names)
    if (r.deleted.length) toast({ title: '已删除', variant: 'success', description: `${r.deleted.length} 首` })
    if (r.skipped.length) {
      toast({
        title: '部分被锁定引用未删除',
        variant: 'default',
        description: `${r.skipped.length} 首：${r.skipped.map((s) => s.name).slice(0, 3).join('、')}${r.skipped.length > 3 ? ' …' : ''}`,
      })
    }
    clearSelection()
    await refresh()
  } catch (e: any) {
    toast({ title: '批量删除失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

// AI 一键识别所选（每曲一任务，共享 LLM 闸）：候选进 suggestions 缓存，
// 用户逐曲确认后才写标签；失败行可重试（重试走同一任务 id）。
const aiBatchBusy = ref(false)
async function doAiSuggestBatch() {
  const names = selectedNames.value
  if (aiBatchBusy.value || !names.length) return
  aiBatchBusy.value = true
  try {
    const r = await suggestTagsBatch(names)
    toast({
      title: `AI 识别已启动（${r.tracks.length} 首）`,
      variant: 'default',
      description: '候选结果出来后可在编辑标签弹层查看并确认采用（仅依据文件名 + 描述，LLM 不读取音频）。',
    })
  } catch (e: any) {
    toast({ title: 'AI 识别启动失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    aiBatchBusy.value = false
  }
}
function cancelAiTask(name: string) {
  const t = aiTasks.value.active.get(name)
  if (t) void taskStore.control(t.id, 'cancel')
}
function retryAiTask(name: string) {
  const t = aiTasks.value.failed.get(name)
  if (t) void taskStore.control(t.id, 'retry')
}

// ---------------------------------------------------------------------------
// 标签编辑弹层（页内自写 overlay，Voices.vue 先例）
// ---------------------------------------------------------------------------

interface EditorState {
  name: string
  tags: TrackTags
  desc: string
}
const editor = ref<EditorState | null>(null)
const editorSaving = ref(false)
const aiLoading = ref(false)
const aiTags = ref<TrackTags | null>(null)
const aiNote = ref('')
const customDraft = ref('')

function emptyTags(): TrackTags {
  return { scene: [], mood: [], emotion: [], custom: [] }
}
function ensureTags(t: Partial<Record<string, string[]>> | null | undefined): TrackTags {
  const out = emptyTags()
  if (t) {
    for (const c of CATEGORIES) {
      const v = t[c]
      if (Array.isArray(v)) out[c] = v.filter((x): x is string => typeof x === 'string')
    }
  }
  return out
}

function openEditor(name: string) {
  const tr = lib.value?.tracks[name]
  if (!tr) return
  editor.value = { name, tags: ensureTags(tr.tags), desc: tr.description || '' }
  aiTags.value = null
  aiNote.value = ''
  customDraft.value = ''
  // 批量 AI 已产出候选（suggestions 缓存）→ 直接预填，不再调用 LLM；
  // 点「AI 推荐」按钮仍可随时重新生成（覆盖预填）。
  const s = suggestionOf(name)
  if (s && CATEGORIES.some((c) => (s.tags?.[c]?.length ?? 0) > 0)) {
    aiTags.value = ensureTags(s.tags)
    aiNote.value = 'AI 推荐结果（仅依据文件名 + 描述，LLM 未读取音频）。点击「全部采用」合并到已勾选标签，或直接修改后保存。'
  }
}
function closeEditor() {
  editor.value = null
}
function toggleEditorTag(cat: MusicTagCategory, tag: string) {
  const e = editor.value
  if (!e) return
  const i = e.tags[cat].indexOf(tag)
  if (i >= 0) e.tags[cat].splice(i, 1)
  else e.tags[cat].push(tag)
}
function addCustomTag() {
  const e = editor.value
  if (!e) return
  const v = customDraft.value.trim()
  if (!v) return
  if (!e.tags.custom.includes(v)) e.tags.custom.push(v)
  customDraft.value = ''
}

// AI 推荐 = 文件名 + 用户描述 + 词表（LLM 不读音频）；结果只是候选，勾选后保存才生效。
async function doSuggest() {
  const e = editor.value
  if (!e || aiLoading.value) return
  aiLoading.value = true
  aiTags.value = null
  aiNote.value = ''
  try {
    const r = await suggestTags(e.name, e.desc.trim() || undefined)
    aiTags.value = ensureTags(r.tags)
    aiNote.value = 'AI 推荐结果（仅依据文件名 + 描述，LLM 未读取音频）。点击「全部采用」合并到已勾选标签，或直接修改后保存。'
  } catch (err: any) {
    toast({ title: 'AI 推荐失败', variant: 'destructive', description: err?.message || '' })
  } finally {
    aiLoading.value = false
  }
}
function adoptAi() {
  const e = editor.value
  const s = aiTags.value
  if (!e || !s) return
  for (const c of CATEGORIES) for (const t of s[c]) if (!e.tags[c].includes(t)) e.tags[c].push(t)
  aiTags.value = null
}

async function saveEditor() {
  const e = editor.value
  if (!e || editorSaving.value) return
  editorSaving.value = true
  try {
    const r = await updateTrack(e.name, { tags: e.tags, description: e.desc })
    lib.value!.tracks[e.name] = r.track
    closeEditor()
    toast({ title: '已保存', variant: 'success', description: e.name })
  } catch (err: any) {
    toast({ title: '保存失败', variant: 'destructive', description: err?.message || '' })
  } finally {
    editorSaving.value = false
  }
}

// ---------------------------------------------------------------------------
// 标签管理（注册表：新增 / 改名 / 删除；改名/删除波及全部曲目与分析缓存）
// ---------------------------------------------------------------------------

const newTag = reactive<Record<MusicTagCategory, string>>({ scene: '', mood: '', emotion: '', custom: '' })
const tagInfo = ref<{ cat: MusicTagCategory; name: string; count: number } | null>(null)

function usageCount(cat: MusicTagCategory, name: string): number {
  return Object.values(lib.value?.tracks ?? {}).filter(
    (t) => Array.isArray(t.tags?.[cat]) && t.tags[cat].includes(name),
  ).length
}
function showTagInfo(cat: MusicTagCategory, name: string) {
  tagInfo.value = { cat, name, count: usageCount(cat, name) }
}

async function doAddTag(cat: MusicTagCategory) {
  const v = newTag[cat].trim()
  if (!v) return
  try {
    const r = await createTag(cat, v)
    if (lib.value) lib.value.tags = r.tags
    newTag[cat] = ''
  } catch (e: any) {
    toast({ title: '新增标签失败', variant: 'destructive', description: e?.message || '' })
  }
}

async function doRenameTag(cat: MusicTagCategory, name: string) {
  const v = window.prompt(`把「${name}」改名为：`, name)
  if (v == null) return
  const nv = v.trim()
  if (!nv) return
  try {
    const r = await renameTag(cat, name, nv)
    if (lib.value) lib.value.tags = r.tags
    toast({ title: '已改名', variant: 'success', description: `波及 ${r.affected_tracks} 首音乐的分析与标签` })
    await refresh()
  } catch (e: any) {
    toast({ title: '改名失败', variant: 'destructive', description: e?.message || '' })
  }
}

async function doDeleteTag(cat: MusicTagCategory, name: string) {
  if (!window.confirm(`删除标签「${name}」？\n仅移除标签（注册表 + 全部音乐 + 章节分析缓存），不删除音乐文件。`)) return
  try {
    const r = await deleteTag(cat, name)
    if (lib.value) lib.value.tags = r.tags
    tagInfo.value = null
    toast({ title: '已删除标签', variant: 'success', description: `${CAT_LABEL[cat]} · ${name}` })
    await refresh()
  } catch (e: any) {
    toast({ title: '删除标签失败', variant: 'destructive', description: e?.message || '' })
  }
}

// ---------------------------------------------------------------------------
// SSE 驱动（getter 式 watch）：AI 任务终态 → 重拉库口径（suggestions 候选）。
// processed 集合防重复（快照重放 / 断线重连）；转回非终态（重试）时释放。
// 批量 N 首不逐条弹 toast（N 大时刷屏）——行状态徽章即反馈。
// ---------------------------------------------------------------------------
const aiProcessed = new Set<string>()
let aiWatcherArmed = false

watch(
  () =>
    taskStore.tasks
      .filter((t) => t.module === AI_MODULE)
      .map((t) => `${t.id}:${t.status}`)
      .join('|'),
  () => {
    if (!aiWatcherArmed) {
      aiWatcherArmed = true
      for (const t of taskStore.tasks) {
        if (t.module === AI_MODULE && AI_TERMINAL.has(t.status)) aiProcessed.add(t.id)
      }
      return
    }
    let dirty = false
    for (const t of taskStore.tasks) {
      if (t.module !== AI_MODULE) continue
      if (!AI_TERMINAL.has(t.status)) {
        aiProcessed.delete(t.id)
        continue
      }
      if (aiProcessed.has(t.id)) continue
      aiProcessed.add(t.id)
      if (t.status === 'succeeded') dirty = true
    }
    if (dirty) void refresh()
  },
)

// ---------------------------------------------------------------------------
// 生命周期（keep-alive 缓存页：重新进入时刷新；F5 后 taskStore.refresh() 使
// 在途 AI 任务按 label 派生重挂，无本地 job 列表）
// ---------------------------------------------------------------------------

onMounted(async () => {
  await taskStore.refresh()
  void refresh()
})
onActivated(() => {
  if (lib.value) void refresh()
})
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        <Disc3 class="h-6 w-6" />音乐库
      </h1>
      <p class="mt-1 text-muted-foreground">
        背景音乐系统的曲目与标签管理。音乐库全局共享，与工作空间无关（<code class="text-xs">music_library/</code>，
        支持 mp3 / wav）。
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Disc3 class="h-5 w-5" />曲目</CardTitle>
        <CardDescription>
          勾选后可批量加/删标签、启用/禁用、删除，或一键 AI 识别标签（仅依据文件名 + 描述，
          LLM 不读取音频；候选需确认后才生效）。标签分四类：场景 / 气氛 / 情绪 / 自定义
          （自动匹配按 气氛 3 · 场景 2 · 情绪 1 · 自定义 1 加权）。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="loadError" variant="destructive">
          {{ loadError }}
        </Alert>

        <div v-else-if="allTracks.length" class="space-y-3">
          <!-- 工具栏 -->
          <div class="flex flex-wrap items-center gap-2">
            <Button size="sm" :disabled="uploading || loading" @click="doUpload">
              <Loader2 v-if="uploading" class="h-3.5 w-3.5 animate-spin" />
              <Upload v-else class="h-3.5 w-3.5" />
              {{ uploading ? '上传中…' : '批量上传' }}
            </Button>
            <div class="relative w-48">
              <Search class="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                v-model="search"
                class="h-8 pl-8 text-xs"
                placeholder="搜索文件名…"
              />
            </div>
            <div class="w-44">
              <select
                v-model="tagFilter"
                class="flex h-8 w-full rounded-md border border-input bg-background px-2 py-1 text-xs shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="">全部标签</option>
                <option v-for="o in filterOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <Button variant="outline" size="sm" :disabled="loading" @click="refresh">
              <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">
              共 {{ allTracks.length }} 首 · 启用 {{ enabledCount }}
            </span>
          </div>

          <!-- 批量操作条（选中 ≥1） -->
          <div
            v-if="selectedNames.length"
            class="flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 p-2"
          >
            <span class="text-xs font-medium">已选 {{ selectedNames.length }} 首</span>
            <div class="flex items-center gap-1.5">
              <select
                v-model="batchCat"
                class="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
              >
                <option v-for="c in CATEGORIES" :key="c" :value="c">{{ CAT_LABEL[c] }}</option>
              </select>
              <select v-model="batchTag" class="h-7 max-w-40 rounded-md border border-input bg-background px-1.5 text-xs">
                <option value="">选择标签…</option>
                <option v-for="t in lib!.tags[batchCat]" :key="t" :value="t">{{ t }}</option>
              </select>
            </div>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchTag('add')">
              <Plus class="h-3.5 w-3.5" />加标签
            </Button>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchTag('remove')">
              <X class="h-3.5 w-3.5" />删标签
            </Button>
            <span class="h-4 w-px bg-border" />
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchEnable(true)">
              启用
            </Button>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchEnable(false)">
              禁用
            </Button>
            <span class="h-4 w-px bg-border" />
            <Button variant="destructive" size="sm" :disabled="batchBusy" @click="doBatchDelete">
              <Trash2 class="h-3.5 w-3.5" />批量删除
            </Button>
            <span class="h-4 w-px bg-border" />
            <Button variant="outline" size="sm" :disabled="aiBatchBusy" @click="doAiSuggestBatch">
              <Sparkles class="h-3.5 w-3.5" />AI 推荐（{{ selectedNames.length }} 首）
            </Button>
            <Button variant="ghost" size="sm" @click="clearSelection">清空选择</Button>
          </div>

          <!-- 行 = 曲目表 -->
          <Table class="max-h-[32rem] overflow-y-auto">
            <TableHeader>
              <TableRow>
                <TableHead class="w-8">
                  <input
                    type="checkbox"
                    class="h-4 w-4 accent-primary"
                    :checked="allVisibleSelected"
                    :disabled="!visibleNames.length || uploading"
                    @change="toggleSelectAll"
                  />
                </TableHead>
                <TableHead>文件名</TableHead>
                <TableHead class="w-14">时长</TableHead>
                <TableHead>标签</TableHead>
                <TableHead class="w-40">AI 识别</TableHead>
                <TableHead class="w-16">启用</TableHead>
                <TableHead class="w-28 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="[name, tr] in trackList" :key="name">
                <TableCell>
                  <input
                    type="checkbox"
                    class="h-4 w-4 accent-primary"
                    :checked="!!selected[name]"
                    @change="
                      (e) => {
                        if ((e.target as HTMLInputElement).checked) selected[name] = true
                        else delete selected[name]
                      }
                    "
                  />
                </TableCell>
                <TableCell class="max-w-64">
                  <div class="flex items-center gap-2">
                    <span class="truncate text-sm font-medium" :title="name">{{ name }}</span>
                    <MiniAudioPlayer :src="musicPreviewUrl(name)" />
                  </div>
                </TableCell>
                <TableCell class="text-xs tabular-nums text-muted-foreground">
                  {{ formatDuration(tr.duration) }}
                </TableCell>
                <TableCell>
                  <div v-if="CATEGORIES.some((c) => tr.tags?.[c]?.length)" class="flex max-w-72 flex-wrap gap-1">
                    <Badge
                      v-for="t in CATEGORIES.flatMap((c) => (tr.tags?.[c] ?? []).map((x) => [c, x]))"
                      :key="t[1]"
                      variant="secondary"
                      :class="CAT_BADGE[t[0] as MusicTagCategory]"
                    >
                      {{ t[1] }}
                    </Badge>
                  </div>
                  <Badge v-else variant="secondary" class="opacity-60">未打标</Badge>
                </TableCell>
                <TableCell>
                  <!-- AI 识别行状态：识别中（在途任务）> 识别失败（可重试）> AI 已推荐（候选待确认）> — -->
                  <div
                    v-if="aiTasks.active.has(name)"
                    class="flex items-center gap-1.5 text-xs text-primary"
                  >
                    <Loader2 class="h-3.5 w-3.5 animate-spin" />
                    <span class="truncate" :title="aiTasks.active.get(name)!.current || '识别中…'">
                      {{ aiTasks.active.get(name)!.current || '识别中…' }}
                    </span>
                    <Button variant="ghost" size="sm" class="h-6 px-1.5 text-xs" @click="cancelAiTask(name)">
                      取消
                    </Button>
                  </div>
                  <div v-else-if="aiTasks.failed.get(name)" class="flex items-center gap-1.5">
                    <Badge
                      variant="destructive"
                      class="text-xs"
                      :title="aiTasks.failed.get(name)!.error || 'AI 识别失败'"
                    >
                      识别失败
                    </Badge>
                    <Button variant="ghost" size="sm" class="h-6 px-1.5 text-xs" @click="retryAiTask(name)">
                      重试
                    </Button>
                  </div>
                  <Badge
                    v-else-if="suggestionHasTags(name)"
                    variant="secondary"
                    class="border-amber-500/30 bg-amber-500/15 text-amber-600 text-xs dark:text-amber-400"
                    :title="`AI 候选：${suggestionSummary(name)}（编辑标签查看并确认采用）`"
                  >
                    <Sparkles class="mr-1 h-3 w-3" />AI 已推荐
                  </Badge>
                  <span
                    v-else-if="suggestionOf(name)"
                    class="text-xs text-muted-foreground"
                  >AI 未推荐到标签</span>
                </TableCell>
                <TableCell>
                  <Switch
                    :model-value="tr.enabled"
                    :class="tr.enabled ? '' : 'opacity-60'"
                    @update:model-value="(v) => toggleEnabled(name, v as boolean)"
                  />
                </TableCell>
                <TableCell>
                  <div class="flex justify-end gap-1">
                    <Button variant="outline" size="sm" @click="openEditor(name)">
                      <Pencil class="h-3.5 w-3.5" />编辑
                    </Button>
                    <Button variant="outline" size="sm" class="text-destructive hover:text-destructive" @click="doDeleteOne(name)">
                      <Trash2 class="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>

          <p v-if="!trackList.length" class="text-sm text-muted-foreground">
            当前筛选下没有曲目。
          </p>
        </div>
        <div v-else class="space-y-2 py-6 text-center">
          <p class="text-sm text-muted-foreground">音乐库为空——上传 mp3 / wav 开始。</p>
          <Button class="mx-auto" :disabled="uploading" @click="doUpload">
            <Loader2 v-if="uploading" class="h-4 w-4 animate-spin" />
            <Upload v-else class="h-4 w-4" />
            批量上传
          </Button>
        </div>
      </CardContent>
    </Card>

    <!-- 标签管理（注册表） -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Tags class="h-5 w-5" />标签管理</CardTitle>
        <CardDescription>
          四类标签词表。改名 / 删除会波及全部曲目与当前工程的章节分析缓存（章节匹配快照不改写）。
          点击标签查看使用数。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <p v-if="tagInfo" class="text-xs text-muted-foreground">
          「{{ CAT_LABEL[tagInfo.cat] }} · {{ tagInfo.name }}」正在被 {{ tagInfo.count }} 首音乐使用。
        </p>
        <div v-for="cat in CATEGORIES" :key="cat" class="space-y-2">
          <div class="text-xs font-semibold text-muted-foreground">{{ CAT_LABEL[cat] }}</div>
          <div class="flex flex-wrap items-center gap-1.5">
            <button
              v-for="t in lib?.tags[cat] ?? []"
              :key="t"
              type="button"
              class="group inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold transition-colors"
              :class="CAT_BADGE[cat]"
              :title="`${CAT_LABEL[cat]} · ${t}（${usageCount(cat, t)} 首音乐使用）`"
              @click="showTagInfo(cat, t)"
            >
              {{ t }}
              <span class="text-[10px] opacity-70">{{ usageCount(cat, t) }}</span>
              <span class="ml-1 hidden gap-0.5 group-hover:inline-flex">
                <span
                  class="cursor-pointer underline opacity-70 hover:opacity-100"
                  @click.stop="doRenameTag(cat, t)"
                >改名</span>
                <span
                  class="cursor-pointer underline opacity-70 hover:opacity-100"
                  @click.stop="doDeleteTag(cat, t)"
                >删除</span>
              </span>
            </button>
            <form
              class="flex items-center gap-1"
              @submit.prevent="doAddTag(cat)"
            >
              <Input
                v-model="newTag[cat]"
                class="h-6 w-24 px-1.5 py-0 text-xs"
                :placeholder="`新增${CAT_LABEL[cat]}标签`"
              />
              <Button variant="outline" size="sm" class="h-6 px-1.5" type="submit">
                <Plus class="h-3 w-3" />
              </Button>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>

    <!-- 标签编辑弹层（页内自写 overlay） -->
    <div
      v-if="editor"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeEditor"
    >
      <div class="max-h-[85vh] w-full max-w-xl space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div>
          <h2 class="text-base font-semibold">{{ editor.name }}</h2>
          <p class="mt-0.5 text-xs text-muted-foreground">勾选标签并保存；词表外标签自动归入「自定义」。</p>
        </div>

        <div v-for="cat in CATEGORIES" :key="cat" class="space-y-1.5">
          <div class="text-xs font-semibold text-muted-foreground">{{ CAT_LABEL[cat] }}</div>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="t in lib?.tags[cat] ?? []"
              :key="t"
              type="button"
              class="rounded-md border px-2 py-0.5 text-xs font-medium transition-colors"
              :class="editor.tags[cat].includes(t)
                ? CAT_BADGE[cat] + ' ring-1 ring-current'
                : 'border-input text-muted-foreground hover:bg-accent'"
              @click="toggleEditorTag(cat, t)"
            >
              {{ t }}
            </button>
            <span v-if="!(lib?.tags[cat]?.length)" class="text-xs text-muted-foreground">（词表为空）</span>
          </div>
          <div v-if="cat === 'custom'" class="flex items-center gap-1.5">
            <Input
              v-model="customDraft"
              class="h-7 w-40 text-xs"
              placeholder="自定义标签，回车添加"
              @keyup.enter="addCustomTag"
            />
            <Button variant="outline" size="sm" class="h-7" @click="addCustomTag">
              <Plus class="h-3 w-3" />添加
            </Button>
          </div>
        </div>

        <div class="space-y-1.5">
          <div class="text-xs font-semibold text-muted-foreground">描述（AI 推荐用，可空）</div>
          <Textarea v-model="editor.desc" class="min-h-16 text-sm" placeholder="例如：低沉弦乐，适合夜间行路场景" />
        </div>

        <div class="rounded-md border p-3">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold">AI 推荐标签</span>
            <Button variant="outline" size="sm" :disabled="aiLoading" @click="doSuggest">
              <Sparkles v-if="!aiLoading" class="h-3.5 w-3.5" />
              <Loader2 v-else class="h-3.5 w-3.5 animate-spin" />
              {{ aiLoading ? '推荐中…' : 'AI 推荐' }}
            </Button>
          </div>
          <p class="mt-1 text-xs text-muted-foreground">
            仅依据文件名 + 描述 + 词表生成（LLM 不读取音频）；结果仅作候选，保存后生效。
          </p>
          <template v-if="aiTags">
            <p v-if="aiNote" class="mt-2 text-xs text-primary">{{ aiNote }}</p>
            <div class="mt-2 flex flex-wrap gap-1.5">
              <Badge
                v-for="t in CATEGORIES.flatMap((c) => aiTags![c].map((x) => [c, x]))"
                :key="t[1]"
                variant="secondary"
                :class="CAT_BADGE[t[0] as MusicTagCategory]"
              >
                {{ t[1] }}
              </Badge>
              <span v-if="!CATEGORIES.some((c) => aiTags![c].length)" class="text-xs text-muted-foreground">
                （AI 未推荐到标签）
              </span>
            </div>
            <Button v-if="aiTags" variant="outline" size="sm" class="mt-2" @click="adoptAi">
              全部采用
            </Button>
          </template>
        </div>

        <div class="flex justify-end gap-2">
          <Button variant="outline" @click="closeEditor">关闭</Button>
          <Button :disabled="editorSaving" @click="saveEditor">
            <Loader2 v-if="editorSaving" class="h-4 w-4 animate-spin" />
            确认并修改
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
