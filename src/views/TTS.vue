<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { synthesize, ttsStatus } from '@/api/tts'
import { isTauri, downloadFile, reveal, downloadUrl } from '@/utils/tauri'
import type { TTSStatus, TTSSynthesizeResult } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Input from '@/components/ui/Input.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Label from '@/components/ui/Label.vue'
import Select from '@/components/ui/Select.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import {
  Mic,
  Sparkles,
  Loader2,
  XCircle,
  CheckCircle2,
  Download,
  FolderOpen,
  ArrowLeft,
  ArrowRight,
  ListTodo,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const taskStore = useTaskStore()
const { push: toast } = useToast()
const inTauri = isTauri()

const status = ref<TTSStatus | null>(null)

// The core input.
const text = ref('你好，世界。这是 AudiobookStudio 的一次语音合成测试。')

// Optional parameters (seeded from config.tts; blank falls back to those defaults).
const speaker = ref('serena')
const language = ref('chinese')
const instruct = ref('')

const LANGUAGES = [
  { value: 'chinese', label: '中文' },
  { value: 'english', label: '英文' },
  { value: 'japanese', label: '日语' },
  { value: 'korean', label: '韩语' },
]

const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<TTSSynthesizeResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value))
const playUrl = computed(() => (result.value ? downloadUrl('tts', result.value.file) : ''))
// Newest-first log lines (the store prepends live log events) — the latest activity
// stays visible at the top while the job runs.
const logLines = computed(() => (task.value?.logs ?? []).slice(0, 8).map((l) => l.msg))

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  const t = settings.config?.tts
  if (t) {
    speaker.value = t.speaker || 'serena'
    language.value = t.language || 'chinese'
  }
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  taskStore.refresh()
})

async function doGenerate() {
  const body = text.value.trim()
  if (!body || busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const { task_id } = await synthesize(body, {
      speaker: speaker.value,
      language: language.value,
      instruct: instruct.value,
    })
    taskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    error.value = e?.message || '启动合成失败'
    busy.value = false
  }
}

watch(
  () => task.value?.status,
  (st) => {
    const t = task.value
    if (!st || !t) return
    if (st === 'succeeded') {
      result.value = t.result as TTSSynthesizeResult
      taskId.value = null
      busy.value = false
      toast({ title: '合成完成', variant: 'success', description: `已生成 ${result.value?.file || '音频'}` })
    } else if (st === 'failed') {
      error.value = t.error || '合成失败'
      taskId.value = null
      busy.value = false
      toast({ title: '合成失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      taskId.value = null
      busy.value = false
    }
    // running / pending / paused: leave busy on; the live Alert shows via v-if="task".
  },
)

function cancel() {
  if (task.value) taskStore.control(task.value.id, 'cancel')
}

function download() {
  if (result.value) downloadFile('tts', result.value.path)
}

function openDir() {
  if (!result.value) return
  const p = result.value.path
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  reveal(i > 0 ? p.slice(0, i) : p)
}

function goBack() {
  router.push('/book')
}
// The audio module is independent (it takes any audio file), so navigation stays
// available whether or not a TTS clip was just produced.
function goNext() {
  router.push('/audio')
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        TTS 合成
        <Badge :variant="status?.implemented ? 'success' : 'secondary'">
          {{ status?.implemented ? '可用' : '引擎未就绪' }}
        </Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">
        输入文本，用本地 Qwen3-TTS 引擎合成语音（<code class="text-xs">.venv-tts</code> 独立环境 · GPU），
        输出 mp3 到 <code class="text-xs">output/tts/</code>。
      </p>
    </div>

    <!-- 引擎未就绪（未装 .venv-tts 或后端未连接） -->
    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <!-- 输入 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Mic class="h-5 w-5" />要合成的文本</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <Textarea v-model="text" rows="5" placeholder="输入要合成的文本…" :disabled="busy" />
          <div class="grid gap-4 sm:grid-cols-3">
            <div class="space-y-1.5">
              <Label>音色</Label>
              <Input v-model="speaker" placeholder="serena" :disabled="busy" />
            </div>
            <div class="space-y-1.5">
              <Label>语言</Label>
              <Select v-model="language" :disabled="busy">
                <option v-for="l in LANGUAGES" :key="l.value" :value="l.value">{{ l.label }}</option>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label>风格指令</Label>
              <Input v-model="instruct" placeholder="如：温柔，语速偏慢" :disabled="busy" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 生成 + 状态 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Sparkles class="h-5 w-5" />生成</CardTitle>
          <CardDescription>
            首次运行会自动下载模型（约 1–2 GB）并缓存到本机；之后每次合成只需加载，速度很快。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button @click="doGenerate" :disabled="busy || !text.trim()">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Mic v-else class="h-4 w-4" />
              {{ busy ? '合成中…' : '开始合成' }}
            </Button>
            <span v-if="!busy" class="text-sm text-muted-foreground">共 {{ text.trim().length }} 字</span>
          </div>

          <!-- 进行中（实时状态 + 日志） -->
          <Alert v-if="task" variant="info" class="items-center">
            <template #icon><Loader2 class="h-4 w-4 shrink-0 animate-spin" /></template>
            <div class="min-w-0 flex-1 space-y-2">
              <div class="flex items-center justify-between gap-3">
                <span class="truncate">{{ task.current || '合成中…' }}</span>
                <Button variant="outline" size="sm" @click="cancel">取消</Button>
              </div>
              <Progress :value="task.progress" />
              <div
                class="max-h-40 space-y-0.5 overflow-y-auto rounded bg-black/20 p-2 font-mono text-xs text-muted-foreground"
              >
                <div v-for="(l, i) in logLines" :key="i" class="truncate">{{ l }}</div>
              </div>
            </div>
          </Alert>

          <!-- 结果（播放 + 下载） -->
          <div v-if="result" class="space-y-3">
            <Alert variant="default" class="items-center">
              <template #icon><CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-500" /></template>
              <span class="flex-1 truncate">合成完成：{{ result.file }}</span>
              <Button variant="outline" size="sm" @click="download"><Download class="h-3.5 w-3.5" />下载</Button>
              <Button v-if="inTauri" variant="outline" size="sm" @click="openDir"><FolderOpen class="h-3.5 w-3.5" />打开目录</Button>
            </Alert>
            <audio :src="playUrl" controls class="w-full" />
          </div>
        </CardContent>
        <CardFooter class="justify-between">
          <Button variant="outline" size="sm" @click="goBack"><ArrowLeft class="h-4 w-4" />返回分册切割</Button>
          <div class="flex gap-2">
            <Button variant="outline" size="sm" @click="router.push('/tasks')"><ListTodo class="h-4 w-4" />任务中心</Button>
            <Button size="sm" @click="goNext">前往下一步（音频分集）<ArrowRight class="h-4 w-4" /></Button>
          </div>
        </CardFooter>
      </Card>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
