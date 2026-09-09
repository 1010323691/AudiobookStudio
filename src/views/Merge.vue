<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { runMerge, ttsStatus } from '@/api/tts'
import { downloadFile, downloadUrl } from '@/utils/fileops'
import type { MergeResult, TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Combine,
  Loader2,
  XCircle,
  CheckCircle2,
  Download,
  ArrowRight,
  ArrowLeft,
  RefreshCw,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)

// Pre-run check: is there a batch manifest to merge?
const manifestChecked = ref(false)
const manifestReady = ref(false)
const manifestCount = ref(0)

const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<MergeResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value) ?? null)
const playUrl = computed(() => (result.value ? downloadUrl('06_audio_merge', result.value.file) : ''))

async function checkManifest() {
  try {
    const res = await fetch(downloadUrl('05_audio_chunk', 'manifest.json'))
    if (res.ok) {
      const m: any = await res.json()
      if (Array.isArray(m)) {
        manifestReady.value = m.length > 0
        manifestCount.value = m.length
      }
    }
  } catch {
    /* not ready */
  }
  manifestChecked.value = true
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await checkManifest()
  taskStore.refresh()
})

async function doRun(m4b = false) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const { task_id } = await runMerge(m4b)
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
      result.value = t.result as MergeResult
      taskId.value = null
      busy.value = false
      project.recordMerge(result.value)
      toast({ title: '音频合并完成', variant: 'success', description: `已生成 ${result.value?.file || '有声书'}` })
    } else if (st === 'failed') {
      error.value = t.error || '音频合并失败'
      taskId.value = null
      busy.value = false
      toast({ title: '音频合并失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      taskId.value = null
      busy.value = false
    }
  },
)

function download() {
  if (result.value) downloadFile('06_audio_merge', result.value.path)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        音频合并
        <Badge :variant="status?.implemented ? 'success' : 'secondary'">
          {{ status?.implemented ? '可用' : '引擎未就绪' }}
        </Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">
        按 JSON 顺序把所有段合并为一整本有声书（换人停顿 500ms / 同人 250ms），
        输出 <code class="text-xs">cloned_audiobook.mp3</code> 到工作空间的 <code class="text-xs">06_audio_merge/</code>。
      </p>
    </div>

    <WorkspaceGateAlert />

    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <Alert v-if="manifestChecked && !manifestReady" variant="warning">
        未找到合成结果——请先到「音频合成」生成各段音频，再回来合并。
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Combine class="h-5 w-5" />开始合并</CardTitle>
          <CardDescription>
            将
            <span class="font-medium text-foreground">
              {{ manifestReady ? `已合成的 ${manifestCount} 段` : '合成结果' }}
            </span>
            按顺序合并为一整本；缺失的段会跳过并告警。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || !workspaceSet" @click="doRun(false)">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Combine v-else class="h-4 w-4" />
              {{ busy ? '合并中…' : '合并为 MP3' }}
            </Button>
            <Button variant="outline" size="sm" @click="checkManifest">
              <RefreshCw class="h-4 w-4" />刷新
            </Button>
          </div>

          <LiveLogPanel :task="task" :max-height-class="'h-80'">
            <template #actions>
              <Button v-if="task" variant="outline" size="sm" @click="cancel">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <!-- 结果（播放 + 下载） -->
          <div v-if="result" class="space-y-3">
            <Alert variant="default" class="items-center">
              <template #icon>
                <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-500" />
              </template>
              <span class="flex-1 truncate">
                合并完成：{{ result.file }}（{{ (result.size / 1024 / 1024).toFixed(1) }} MB，{{ result.segments }} 段）
              </span>
              <Button variant="outline" size="sm" @click="download">
                <Download class="h-3.5 w-3.5" />下载
              </Button>
            </Alert>
            <audio :src="playUrl" controls class="w-full" />
          </div>
        </CardContent>
        <CardFooter class="justify-between">
          <Button variant="outline" size="sm" @click="router.push('/batch')">
            <ArrowLeft class="h-4 w-4" />返回音频合成
          </Button>
          <Button v-if="result" size="sm" @click="router.push('/audio')">
            前往音频分集<ArrowRight class="h-4 w-4" />
          </Button>
        </CardFooter>
      </Card>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
