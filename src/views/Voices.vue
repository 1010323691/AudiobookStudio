<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listVoices, makeClones, prepareFoundations, ttsStatus } from '@/api/tts'
import { downloadUrl } from '@/utils/fileops'
import type { MakeClonesResult, PrepareFoundationsResult, TTSStatus, VoiceItem } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import DirPicker from '@/components/DirPicker.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Users,
  Sparkles,
  AudioWaveform,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  ArrowRight,
  FolderOpen,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)
const hasScript = ref(false)
const speakers = ref<VoiceItem[]>([])

// Per-character optional description overrides (a single-char Phase-1 regenerate honours these).
const prompts = reactive<Record<string, string>>({})

const error = ref('')

// Phase 1 (语音推理基础, LLM only) task state.
const foundationBusy = ref(false)
const foundationTaskId = ref<string | null>(null)
const foundationResult = ref<PrepareFoundationsResult | null>(null)
const foundationTask = computed(() => taskStore.tasks.find((t) => t.id === foundationTaskId.value) ?? null)

// Phase 2 (克隆音频, TTS only) task state.
const cloneBusy = ref(false)
const cloneTaskId = ref<string | null>(null)
const cloneResult = ref<MakeClonesResult | null>(null)
const cloneTask = computed(() => taskStore.tasks.find((t) => t.id === cloneTaskId.value) ?? null)

// TTS concurrency for Phase 2 (parallel subprocesses; VRAM scales with it). Seeded from config.
const cloneConcurrency = ref(1)

// Which characters the in-flight phase task targets: null = 全部（批量）; a list = the specific
// row(s) of a single-character run. Lets the per-row 生成中/制作中 overlay light up ONLY the rows
// that run actually touches (so a single-character regen doesn't mark every 未生成 row as in-flight).
const foundationTargets = ref<string[] | null>(null)
const cloneTargets = ref<string[] | null>(null)

// The parsed-JSON selection on THIS page. Local (not the shared store) so the whole-book
// "全部文件" scope ('__all__') never leaks into 音频合成, which is per-file.
// '' = most recent; a file name = that file; '__all__' = every file in 03_parsed_json/.
const ALL_SCRIPT = '__all__'
const scope = ref(project.activeScript || '')
// Which parsed JSON(s) to read (mirrors the picker; '__all__' aggregates every file).
const script = computed(() => scope.value)

// A phase is "running" while any of its tasks is active (drives the per-row 生成中/制作中 overlay).
const ACTIVE: string[] = ['pending', 'running', 'paused']
const foundationRunning = computed(() => taskStore.tasks.some((t) => t.module === 'voices-foundation' && ACTIVE.includes(t.status)))
const cloneRunning = computed(() => taskStore.tasks.some((t) => t.module === 'voices-clone' && ACTIVE.includes(t.status)))

// Button gating: a phase is blocked while its own launch is in flight, while the OTHER phase
// is running (so the LLM and TTS never share the GPU), or with no workspace / script.
const foundationBlocked = computed(() => foundationBusy.value || cloneRunning.value || !hasScript.value || !workspaceSet.value)
const cloneBlocked = computed(() => cloneBusy.value || foundationRunning.value || !hasScript.value || !workspaceSet.value)

// Progress + readiness (denominator = non-alias characters).
const nonAlias = computed(() => speakers.value.filter((s) => !s.alias_of))
const foundationDone = computed(() => nonAlias.value.filter((s) => s.foundation_status === 'done').length)
const cloneDone = computed(() => nonAlias.value.filter((s) => s.clone_status === 'done').length)
const readyCount = computed(() => speakers.value.filter((s) => s.status === 'ready').length)

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
interface PhaseBadge { label: string; variant: BadgeVariant; spin: boolean }

// A run lights up a row when it is a batch (targets === null) or explicitly names that character.
function inTargets(list: string[] | null, name: string) {
  return list === null || list.includes(name)
}

// Phase 1 (语音推理基础) badge: done/failed from the stored state; a row the running task
// targets (and that isn't done/failed yet) shows 生成中 (spinner) — only the rows that run touches.
function foundationBadge(v: VoiceItem): PhaseBadge {
  if (v.foundation_status === 'done') return { label: '已生成', variant: 'success', spin: false }
  if (v.foundation_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  if (foundationRunning.value && inTargets(foundationTargets.value, v.name)) return { label: '生成中', variant: 'warning', spin: true }
  return { label: '未生成', variant: 'secondary', spin: false }
}

// Phase 2 (克隆音频) badge: same derivation against clone_status / cloneRunning / cloneTargets.
function cloneBadge(v: VoiceItem): PhaseBadge {
  if (v.clone_status === 'done') return { label: '已完成', variant: 'success', spin: false }
  if (v.clone_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  if (cloneRunning.value && inTargets(cloneTargets.value, v.name)) return { label: '制作中', variant: 'warning', spin: true }
  return { label: '未制作', variant: 'secondary', spin: false }
}

async function loadVoices() {
  try {
    const r = await listVoices(script.value || undefined)
    hasScript.value = r.has_script
    speakers.value = r.speakers
  } catch {
    hasScript.value = false
    speakers.value = []
  }
}

// Local → store: a concrete file / most-recent keeps 音频合成 in step; the "all files"
// scope is Voices-local and must not be written to the shared selection.
watch(scope, (v) => {
  loadVoices()
  if (v !== ALL_SCRIPT) project.activeScript = v
})
// Store → local: under keep-alive this page is cached, so a pick made on 音频合成 must
// refresh the (cached) character list. Guarded so an active "all files" view is kept.
watch(() => project.activeScript, (v) => {
  if (scope.value !== ALL_SCRIPT && v !== scope.value) scope.value = v
})

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  cloneConcurrency.value = settings.config?.tts.parallel_workers ?? 1
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await loadVoices()
  taskStore.refresh()
})

async function doFoundations(opts: {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
}) {
  if (foundationBusy.value || cloneRunning.value) return
  foundationBusy.value = true
  error.value = ''
  foundationResult.value = null
  foundationTargets.value = opts.speakers ?? null
  try {
    const { task_id } = await prepareFoundations({ ...opts, script: script.value || undefined })
    foundationTaskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on foundationTask.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    foundationBusy.value = false
  }
}

async function doClones(opts: {
  speakers?: string[]
  new_only?: boolean
}) {
  if (cloneBusy.value || foundationRunning.value) return
  cloneBusy.value = true
  error.value = ''
  cloneResult.value = null
  cloneTargets.value = opts.speakers ?? null
  try {
    const { task_id } = await makeClones({
      ...opts,
      concurrency: cloneConcurrency.value,
      script: script.value || undefined,
    })
    cloneTaskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on cloneTask.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    cloneBusy.value = false
  }
}

// Coerce the TTS concurrency input (the Input component emits a string) to a sane integer ≥ 1.
function onConcurrency(v: string | number) {
  const n = Math.trunc(Number(v))
  cloneConcurrency.value = Number.isFinite(n) && n >= 1 ? n : 1
}

// Phase 1: re-call the LLM for this character's foundation (honours the per-row prompt override).
function regenFoundation(v: VoiceItem) {
  const prompt = (prompts[v.name] || '').trim()
  doFoundations({ speakers: [v.name], overrides: prompt ? { [v.name]: prompt } : {} })
}

// Phase 2: render this character's clone audio from its existing foundation (force a redo).
function remakeClone(v: VoiceItem) {
  doClones({ speakers: [v.name] })
}

function previewUrl(v: VoiceItem): string {
  return v.preview ? downloadUrl('04_voice_profiles', v.preview) : ''
}

function cancelFoundation() {
  if (foundationTask.value) taskStore.control(foundationTask.value.id, 'cancel')
}
function cancelClone() {
  if (cloneTask.value) taskStore.control(cloneTask.value.id, 'cancel')
}

watch(
  () => foundationTask.value?.status,
  (st) => {
    const t = foundationTask.value
    if (!st || !t) return
    if (st === 'succeeded') {
      foundationResult.value = t.result as PrepareFoundationsResult
      foundationBusy.value = false
      project.recordVoices(foundationResult.value)
      toast({ title: '语音推理基础生成完成', variant: 'success', description: `已为 ${foundationResult.value?.count ?? 0} 个角色生成基础（${foundationResult.value?.aliases ?? 0} 个别名）` })
      // Keep foundationTaskId set so the log panel stays visible with the final logs; the next
      // run simply overwrites it.
      loadVoices()
    } else if (st === 'failed') {
      error.value = t.error || '语音推理基础生成失败'
      foundationBusy.value = false
      toast({ title: '语音推理基础生成失败', variant: 'destructive', description: error.value })
      loadVoices() // reflect characters that completed before the failure
    } else if (st === 'cancelled') {
      foundationBusy.value = false
      loadVoices() // reflect characters that completed before the cancel
    }
  },
)

// While the foundation task is running, refresh the character list on each progress event so
// each character's 语音推理基础 badge (and any preview) updates the moment it completes — the
// backend persists per-character as each LLM call finishes, so this poll picks it up live.
watch(
  () => foundationTask.value?.progress,
  (p) => {
    const t = foundationTask.value
    if (p == null || !t) return
    if (ACTIVE.includes(t.status)) loadVoices()
  },
)

watch(
  () => cloneTask.value?.status,
  (st) => {
    const t = cloneTask.value
    if (!st || !t) return
    if (st === 'succeeded') {
      cloneResult.value = t.result as MakeClonesResult
      cloneBusy.value = false
      toast({ title: '克隆音频制作完成', variant: 'success', description: `成功 ${cloneResult.value?.ok ?? 0} / 失败 ${cloneResult.value?.failed ?? 0} / 共 ${cloneResult.value?.count ?? 0} 个角色` })
      // Keep cloneTaskId set so the log panel stays visible with the final logs; the next run
      // simply overwrites it.
      loadVoices()
    } else if (st === 'failed') {
      error.value = t.error || '克隆音频制作失败'
      cloneBusy.value = false
      toast({ title: '克隆音频制作失败', variant: 'destructive', description: error.value })
      loadVoices() // reflect characters that completed before the failure
    } else if (st === 'cancelled') {
      cloneBusy.value = false
      loadVoices() // reflect characters that completed before the cancel
    }
  },
)

// While the clone task is running, refresh the character list on each progress event so each
// character's 克隆音频 badge (and its 试听 preview) appears the moment that render completes.
watch(
  () => cloneTask.value?.progress,
  (p) => {
    const t = cloneTask.value
    if (p == null || !t) return
    if (ACTIVE.includes(t.status)) loadVoices()
  },
)
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        角色配音
        <Badge :variant="status?.implemented ? 'success' : 'secondary'">
          {{ status?.implemented ? '可用' : '引擎未就绪' }}
        </Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">
        分两阶段为每个角色配音：阶段 1 用 LLM 生成语音推理基础（声音描述 + 种子文案），阶段 2 用 TTS 渲染克隆音频；
        两阶段互不占用对方显存（建议阶段 1 完成后关闭 LLM 再跑阶段 2），并可对单个角色分别重新生成 / 重新制作。
        配置保存到工作空间的 <code class="text-xs">04_voice_profiles/</code>。
      </p>
    </div>

    <WorkspaceGateAlert />

    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <Alert v-if="!hasScript" variant="default">
        <Users class="h-4 w-4 shrink-0" />
        尚未检测到角色——请先在「文本解析」生成解析 JSON（03_parsed_json/）。
      </Alert>

      <!-- 阶段 1 · 生成语音推理基础（LLM only） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Sparkles class="h-5 w-5" />阶段 1 · 生成语音推理基础</CardTitle>
          <CardDescription>
            仅调用 LLM（<b>不启动 TTS / 不占显存</b>），为每个角色生成声音描述 + 种子文案并保存到工作空间；
            完成后请关闭 LLM 以释放显存，再运行阶段 2。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="foundationBlocked" @click="doFoundations({})">
              <Loader2 v-if="foundationBusy" class="h-4 w-4 animate-spin" />
              <Sparkles v-else class="h-4 w-4" />
              {{ foundationBusy ? '生成中…' : '批量生成所有角色' }}
            </Button>
            <Button variant="outline" :disabled="foundationBlocked" @click="doFoundations({ new_only: true })">
              <Users class="h-4 w-4" />仅新增角色
            </Button>
            <Button variant="outline" size="sm" @click="loadVoices">
              <RefreshCw class="h-4 w-4" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">语音推理基础：{{ foundationDone }} / {{ nonAlias.length }}</span>
          </div>

          <LiveLogPanel :task="foundationTask" :max-height-class="'h-72'">
            <template #actions>
              <Button v-if="foundationTask && ACTIVE.includes(foundationTask.status)" variant="outline" size="sm" @click="cancelFoundation">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <div
            v-if="foundationResult"
            class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
          >
            <CheckCircle2 class="h-4 w-4 shrink-0" />
            完成：为 {{ foundationResult.count }} 个角色生成语音推理基础，识别 {{ foundationResult.aliases }} 个别名（未启动 TTS）。
          </div>
        </CardContent>
      </Card>

      <!-- 阶段 2 · 制作克隆音频（TTS only） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioWaveform class="h-5 w-5" />阶段 2 · 制作克隆音频</CardTitle>
          <CardDescription>
            仅调用 TTS（<b>不使用 LLM</b>），读回已保存的语音推理基础，为每个角色渲染克隆种子音频。
            <br /><span class="font-medium text-amber-500">请先关闭 LLM，以释放显存后再开始 TTS 合成。</span>
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="cloneBlocked" @click="doClones({ new_only: true })">
              <Loader2 v-if="cloneBusy" class="h-4 w-4 animate-spin" />
              <AudioWaveform v-else class="h-4 w-4" />
              {{ cloneBusy ? '制作中…' : '批量制作克隆音频' }}
            </Button>
            <label class="flex items-center gap-2 text-sm text-muted-foreground">
              TTS 并发数
              <Input
                :modelValue="cloneConcurrency"
                type="number"
                min="1"
                class="h-8 w-20"
                :disabled="cloneBlocked"
                @update:modelValue="onConcurrency"
              />
            </label>
            <span class="ml-auto text-xs text-muted-foreground">克隆音频：{{ cloneDone }} / {{ nonAlias.length }}</span>
          </div>

          <LiveLogPanel :task="cloneTask" :max-height-class="'h-72'">
            <template #actions>
              <Button v-if="cloneTask && ACTIVE.includes(cloneTask.status)" variant="outline" size="sm" @click="cancelClone">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <div
            v-if="cloneResult"
            class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
          >
            <CheckCircle2 class="h-4 w-4 shrink-0" />
            完成：克隆音频成功 {{ cloneResult.ok }} / 失败 {{ cloneResult.failed }} / 共 {{ cloneResult.count }} 个角色。
          </div>
        </CardContent>
      </Card>

      <!-- 工作区目录 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><FolderOpen class="h-5 w-5" />工作区目录</CardTitle>
          <CardDescription>选择要配音的解析脚本；下方「角色」列表与配音操作都基于它。</CardDescription>
        </CardHeader>
        <CardContent>
          <DirPicker
            module="03_parsed_json"
            :extensions="['json']"
            exclude-suffix="_checked.json"
            v-model="scope"
            :show-all="true"
            :all-value="ALL_SCRIPT"
            label="解析 JSON（03_parsed_json/）"
          />
        </CardContent>
      </Card>

      <!-- 角色 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Users class="h-5 w-5" />角色（{{ speakers.length }}）
          </CardTitle>
          <CardDescription v-if="speakers.length">已就绪 {{ readyCount }} / {{ speakers.length }} · 基础 {{ foundationDone }} / 克隆 {{ cloneDone }} / {{ nonAlias.length }}</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div v-if="speakers.length" class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-xs text-muted-foreground">
                  <th class="pb-2 font-medium">角色</th>
                  <th class="pb-2 font-medium">台词数</th>
                  <th class="pb-2 font-medium">语音推理基础</th>
                  <th class="pb-2 font-medium">克隆音频</th>
                  <th class="pb-2 font-medium">声音描述 / 提示词</th>
                  <th class="pb-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="v in speakers" :key="v.name" class="border-b align-top last:border-0">
                  <td class="py-2 pr-3 font-medium">
                    {{ v.name }}
                    <span v-if="v.alias_of" class="ml-1 text-xs text-muted-foreground">→ {{ v.alias_of }}</span>
                  </td>
                  <td class="py-2 pr-3 text-muted-foreground">{{ v.line_count }}</td>
                  <td class="py-2 pr-3">
                    <Badge :variant="foundationBadge(v).variant">
                      <Loader2 v-if="foundationBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />
                      {{ foundationBadge(v).label }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-3">
                    <Badge :variant="cloneBadge(v).variant">
                      <Loader2 v-if="cloneBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />
                      {{ cloneBadge(v).label }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-3">
                    <div class="max-w-xs truncate text-xs text-muted-foreground" :title="v.description">
                      {{ v.description || '—' }}
                    </div>
                    <Input
                      v-model="prompts[v.name]"
                      class="mt-1.5 h-8 text-xs"
                      placeholder="可选：自定义声音描述（阶段 1 重新生成时生效）"
                      :disabled="foundationBusy"
                    />
                  </td>
                  <td class="py-2">
                    <div class="flex items-center justify-end gap-2">
                      <MiniAudioPlayer v-if="v.preview" :src="previewUrl(v)" />
                      <Button variant="outline" size="sm" :disabled="foundationBlocked" @click="regenFoundation(v)">
                        <RefreshCw class="h-3.5 w-3.5" />重新生成
                      </Button>
                      <Button variant="outline" size="sm" :disabled="cloneBlocked || v.foundation_status !== 'done'" @click="remakeClone(v)">
                        <AudioWaveform class="h-3.5 w-3.5" />重新制作
                      </Button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="text-sm text-muted-foreground">（暂无角色）</p>
        </CardContent>
      </Card>

      <div v-if="hasScript && readyCount >= speakers.length && speakers.length > 0" class="flex justify-end">
        <Button size="sm" @click="router.push('/batch')">
          前往音频合成<ArrowRight class="h-4 w-4" />
        </Button>
      </div>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
