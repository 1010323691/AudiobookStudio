<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listVoices, prepareVoices, ttsStatus } from '@/api/tts'
import { downloadUrl } from '@/utils/fileops'
import type { PrepareVoicesResult, TTSStatus, VoiceItem } from '@/types'

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
import ScriptPicker from '@/components/ScriptPicker.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import {
  Users,
  Sparkles,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  Play,
  ArrowRight,
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

// Per-character optional description overrides (a single-char regenerate honours these).
const prompts = reactive<Record<string, string>>({})

const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<PrepareVoicesResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value) ?? null)
// Which parsed JSON to read (shared with 音频合成 via the project store; '' → most recent).
const script = computed(() => project.activeScript)
const activePreview = ref<{ name: string; url: string } | null>(null)
const readyCount = computed(() => speakers.value.filter((s) => s.status === 'ready').length)

function typeLabel(v: VoiceItem) {
  if (v.alias_of) return '别名'
  return v.type === 'clone' ? '克隆' : v.type === 'design' ? '设计' : v.type === 'custom' ? '预置' : '—'
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

// Re-list the characters when the user picks a different parsed JSON.
watch(script, () => {
  loadVoices()
})

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await loadVoices()
  taskStore.refresh()
})

async function doPrepare(opts: {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
}) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  activePreview.value = null
  try {
    const { task_id } = await prepareVoices({ ...opts, script: script.value || undefined })
    taskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

function regenOne(v: VoiceItem) {
  const prompt = (prompts[v.name] || '').trim()
  doPrepare({ speakers: [v.name], overrides: prompt ? { [v.name]: prompt } : {} })
}

function play(v: VoiceItem) {
  if (!v.preview) return
  activePreview.value = { name: v.name, url: downloadUrl('04_voice_profiles', v.preview) }
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
      result.value = t.result as PrepareVoicesResult
      taskId.value = null
      busy.value = false
      project.recordVoices(result.value)
      toast({ title: '角色配音准备完成', variant: 'success', description: `已处理 ${result.value?.count ?? 0} 个角色` })
      loadVoices()
    } else if (st === 'failed') {
      error.value = t.error || '准备失败'
      taskId.value = null
      busy.value = false
      toast({ title: '准备失败', variant: 'destructive', description: error.value })
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
        角色配音
        <Badge :variant="status?.implemented ? 'success' : 'secondary'">
          {{ status?.implemented ? '可用' : '引擎未就绪' }}
        </Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">
        自动识别脚本中的每个角色，为每个角色生成 / 克隆独特声音（一键全部就绪），并对单个角色按提示词重新生成；
        声音配置与预览保存到工作空间的 <code class="text-xs">04_voice_profiles/</code>。
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

      <!-- 角色列表 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Users class="h-5 w-5" />角色（{{ speakers.length }}）
          </CardTitle>
          <CardDescription v-if="speakers.length">已就绪 {{ readyCount }} / {{ speakers.length }}</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <ScriptPicker v-model="project.activeScript" label="解析 JSON（03_parsed_json/）" />
          <div v-if="speakers.length" class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-xs text-muted-foreground">
                  <th class="pb-2 font-medium">角色</th>
                  <th class="pb-2 font-medium">台词数</th>
                  <th class="pb-2 font-medium">状态</th>
                  <th class="pb-2 font-medium">类型</th>
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
                    <Badge :variant="v.status === 'ready' ? 'success' : 'secondary'">
                      {{ v.status === 'ready' ? '已就绪' : '待生成' }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-3 text-muted-foreground">{{ typeLabel(v) }}</td>
                  <td class="py-2 pr-3">
                    <div class="max-w-xs truncate text-xs text-muted-foreground" :title="v.description">
                      {{ v.description || '—' }}
                    </div>
                    <Input
                      v-model="prompts[v.name]"
                      class="mt-1.5 h-8 text-xs"
                      placeholder="可选：自定义声音描述"
                      :disabled="busy"
                    />
                  </td>
                  <td class="py-2">
                    <div class="flex items-center justify-end gap-2">
                      <Button v-if="v.preview" variant="ghost" size="sm" @click="play(v)">
                        <Play class="h-3.5 w-3.5" />试听
                      </Button>
                      <Button variant="outline" size="sm" :disabled="busy || !workspaceSet" @click="regenOne(v)">
                        <RefreshCw class="h-3.5 w-3.5" />重生成
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

      <!-- 操作 + 实时日志 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Sparkles class="h-5 w-5" />生成 / 重新生成</CardTitle>
          <CardDescription>
            一键为所有角色生成声音（LLM 描述 + 预览克隆）；也可仅处理新增，或对单个角色按提示词重生成。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || !hasScript || !workspaceSet" @click="doPrepare({})">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Sparkles v-else class="h-4 w-4" />
              {{ busy ? '生成中…' : '一键准备所有角色声音' }}
            </Button>
            <Button variant="outline" :disabled="busy || !hasScript || !workspaceSet" @click="doPrepare({ new_only: true })">
              <Users class="h-4 w-4" />仅新增角色
            </Button>
            <Button variant="outline" size="sm" @click="loadVoices">
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

          <div
            v-if="result"
            class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
          >
            <CheckCircle2 class="h-4 w-4 shrink-0" />
            完成：处理 {{ result.count }} 个角色，识别 {{ result.aliases }} 个别名。
          </div>
        </CardContent>
      </Card>

      <!-- 试听 -->
      <Card v-if="activePreview">
        <CardHeader>
          <CardTitle class="text-base">试听：{{ activePreview.name }}</CardTitle>
        </CardHeader>
        <CardContent>
          <audio :src="activePreview.url" controls class="w-full" />
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
