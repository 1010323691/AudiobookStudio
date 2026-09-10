<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listVoices, runBatch, ttsStatus } from '@/api/tts'
import { downloadUrl } from '@/utils/fileops'
import type { BatchResult, TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import ScriptPicker from '@/components/ScriptPicker.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Layers,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  ArrowRight,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)

// Pre-run summary: how many segments / characters are about to be synthesized.
const summaryLoaded = ref(false)
const segmentCount = ref<number | null>(null)
const speakerCount = ref(0)
const readyVoices = ref(0)

const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<BatchResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value) ?? null)
// Which parsed JSON to synthesize (shared with 角色配音 via the project store; '' → most recent).
const script = computed(() => project.activeScript)
const missingVoices = computed(() => Math.max(0, speakerCount.value - readyVoices.value))

async function loadSummary() {
  let resolvedName = ''
  try {
    const r = await listVoices(script.value || undefined)
    speakerCount.value = r.speakers.length
    readyVoices.value = r.speakers.filter((s) => s.status === 'ready').length
    if (r.has_script && r.script_path) {
      // The backend resolves the chosen (or most-recent) JSON — fetch that exact file.
      resolvedName = r.script_path.split(/[\\/]/).pop() || ''
    }
  } catch {
    /* backend down — leave counts blank */
  }
  try {
    if (resolvedName) {
      const res = await fetch(downloadUrl('03_parsed_json', resolvedName))
      if (res.ok) {
        const data: any = await res.json()
        if (Array.isArray(data)) {
          segmentCount.value = data.filter((e: any) => (e.text || '').trim()).length
        }
      }
    }
  } catch {
    /* no script yet */
  }
  summaryLoaded.value = true
}

// Re-summarize when the user picks a different parsed JSON.
watch(script, () => {
  loadSummary()
})

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await loadSummary()
  taskStore.refresh()
})

async function doRun() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const { task_id } = await runBatch({ script: script.value || undefined })
    taskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on task.status.
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
      toast({ title: '音频合成失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      taskId.value = null
      busy.value = false
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
        按 JSON 顺序为整本书的所有段合成语音（模型只加载一次），单段失败会记录而不中断；
        进度、当前段与当前角色实时刷新，音频段与 <code class="text-xs">manifest.json</code> 保存到工作空间的
        <code class="text-xs">05_audio_chunk/</code>。
      </p>
    </div>

    <WorkspaceGateAlert />

    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <!-- 待合成汇总 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />待合成</CardTitle>
          <CardDescription>来自所选解析 JSON（03_parsed_json/）与角色配音配置。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <ScriptPicker v-model="project.activeScript" label="解析 JSON（03_parsed_json/）" />
          <div class="flex flex-wrap items-center gap-x-10 gap-y-2">
            <div>
              <div class="text-2xl font-bold">{{ segmentCount ?? '—' }}</div>
              <div class="text-xs text-muted-foreground">段落</div>
            </div>
            <div>
              <div class="text-2xl font-bold">{{ speakerCount || '—' }}</div>
              <div class="text-xs text-muted-foreground">角色</div>
            </div>
            <div>
              <div class="text-2xl font-bold">{{ readyVoices }}</div>
              <div class="text-xs text-muted-foreground">已就绪声音</div>
            </div>
          </div>
          <Alert
            v-if="summaryLoaded && segmentCount === 0"
            variant="default"
          >
            没有可合成的段——请先到「文本解析」生成脚本。
          </Alert>
          <Alert v-if="missingVoices > 0" variant="warning">
            有 {{ missingVoices }} 个角色尚未就绪声音——建议先到「角色配音」页一键生成，否则这些角色的段会失败。
          </Alert>
        </CardContent>
      </Card>

      <!-- 操作 + 实时日志 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />一键音频合成</CardTitle>
          <CardDescription>
            合成整本书；模型只加载一次，逐段推进，实时显示「第 i / N 段 · 当前角色 · 正在生成」与成功 / 失败。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || !workspaceSet" @click="doRun">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Layers v-else class="h-4 w-4" />
              {{ busy ? '合成中…' : '开始音频合成' }}
            </Button>
            <Button variant="outline" size="sm" @click="loadSummary">
              <RefreshCw class="h-4 w-4" />刷新
            </Button>
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
                完成：成功 {{ result.completed }} / 共 {{ result.total }} 段
                <span v-if="result.failed.length" class="text-amber-600 dark:text-amber-400">
                  ，失败 {{ result.failed.length }}
                </span>
              </span>
              <Button v-if="result.completed > 0" size="sm" @click="router.push('/merge')">
                前往音频合并<ArrowRight class="h-4 w-4" />
              </Button>
            </Alert>

            <div v-if="result.failed.length" class="space-y-1.5">
              <div class="text-xs font-medium text-muted-foreground">失败的段（{{ result.failed.length }}）</div>
              <div class="max-h-56 space-y-1 overflow-y-auto rounded-md border p-2 text-xs">
                <div v-for="f in result.failed" :key="f.index" class="flex items-start gap-2">
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
