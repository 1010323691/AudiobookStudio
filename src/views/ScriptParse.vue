<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { generateScript, getScriptResult } from '@/api/script'
import { isTauri, downloadFile, reveal } from '@/utils/tauri'
import type { AppConfig, ScriptGenerateResult } from '@/types'

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
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import {
  Server,
  SlidersHorizontal,
  MessageSquareText,
  FileText,
  ScanText,
  Save,
  Loader2,
  XCircle,
  CheckCircle2,
  Download,
  FolderOpen,
  ListTodo,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const taskStore = useTaskStore()
const { push: toast } = useToast()
const inTauri = isTauri()

// Config sections (local drafts; each is saved independently via settings.save so
// the values persist to config/app.json and survive a restart — requirement #2/#3).
const llm = reactive<AppConfig['llm']>({ base_url: 'http://localhost:11434/v1', api_key: 'local', model_name: '' })
const generation = reactive<AppConfig['generation']>({
  chunk_size: 3000,
  max_tokens: 4096,
  temperature: 0.6,
  top_p: 0.8,
  top_k: 0,
  min_p: 0.0,
  presence_penalty: 0.0,
  banned_tokens: [],
})
const prompts = reactive<AppConfig['prompts']>({ system_prompt: '', user_prompt: '' })

// The core input + task/result state.
const text = ref('')
const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<ScriptGenerateResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value))
// Newest-first log lines (the store prepends live log events) — latest activity on top.
const logLines = computed(() => (task.value?.logs ?? []).slice(0, 8).map((l) => l.msg))

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  const c = settings.config
  if (c) {
    Object.assign(llm, c.llm)
    Object.assign(generation, c.generation)
    Object.assign(prompts, c.prompts) // GET seeds empty prompts from the bundled defaults
  }
  // Restore the last generated script so a reload still shows it (and its download).
  try {
    const r = await getScriptResult()
    if (r && r.entries && r.entries.length) result.value = r
  } catch {
    /* no prior result / backend down */
  }
  taskStore.refresh()
})

async function saveSection(patch: Partial<AppConfig>, title: string) {
  const ok = await settings.save(patch)
  if (ok) toast({ title, variant: 'success', description: '已保存到 config/app.json，重启后自动恢复。' })
  else toast({ title: '保存失败', variant: 'destructive' })
}
function saveLLM() {
  saveSection({ llm: { ...llm } }, 'LLM 配置已保存')
}
function saveGeneration() {
  saveSection({ generation: { ...generation } }, '生成参数已保存')
}
function savePrompts() {
  saveSection({ prompts: { ...prompts } }, 'Prompt 已保存')
}

async function doParse() {
  const body = text.value.trim()
  if (!body || busy.value) return
  if (!(llm.model_name || '').trim()) {
    error.value = '请先填写 LLM 模型名称（模型不能为空）。'
    return
  }
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const { task_id } = await generateScript(body)
    taskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    error.value = e?.message || '启动解析失败'
    busy.value = false
  }
}

watch(
  () => task.value?.status,
  (st) => {
    const t = task.value
    if (!st || !t) return
    if (st === 'succeeded') {
      result.value = t.result as ScriptGenerateResult
      taskId.value = null
      busy.value = false
      toast({ title: '解析完成', variant: 'success', description: `共 ${result.value?.count ?? 0} 条` })
    } else if (st === 'failed') {
      error.value = t.error || '解析失败'
      taskId.value = null
      busy.value = false
      toast({ title: '解析失败', variant: 'destructive', description: error.value })
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
  if (result.value) downloadFile('tts', result.value.output_path)
}
function openDir() {
  if (!result.value?.output_path) return
  const p = result.value.output_path
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  reveal(i > 0 ? p.slice(0, i) : p)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        <ScanText class="h-6 w-6" />文本解析
      </h1>
      <p class="mt-1 text-muted-foreground">
        输入小说原文，调用 LLM 按 Prompt 生成
        <code class="text-xs">speaker / text / instruct</code> 的 JSON（迁移自源项目），
        结果直接对接本地 TTS 合成。
      </p>
    </div>

    <!-- LLM 配置 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Server class="h-5 w-5" />LLM 配置</CardTitle>
        <CardDescription>
          OpenAI 兼容端点（chat/completions）。本地 Ollama 默认为
          <code class="text-xs">http://localhost:11434/v1</code>。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div class="space-y-1.5">
          <Label>API 地址</Label>
          <Input v-model="llm.base_url" placeholder="http://localhost:11434/v1" />
        </div>
        <div class="grid gap-4 sm:grid-cols-2">
          <div class="space-y-1.5">
            <Label>API Key</Label>
            <Input v-model="llm.api_key" placeholder="local" />
          </div>
          <div class="space-y-1.5">
            <Label>模型名称</Label>
            <Input v-model="llm.model_name" placeholder="如 qwen3:14b（必填）" />
          </div>
        </div>
        <div class="flex justify-end">
          <Button size="sm" @click="saveLLM"><Save class="h-4 w-4" />保存配置</Button>
        </div>
      </CardContent>
    </Card>

    <!-- 生成参数 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><SlidersHorizontal class="h-5 w-5" />生成参数</CardTitle>
        <CardDescription>分段大小与采样设置，作用于每次 LLM 请求。</CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div class="space-y-1.5">
            <Label>分段大小（字）</Label>
            <Input v-model.number="generation.chunk_size" type="number" min="1" />
          </div>
          <div class="space-y-1.5">
            <Label>最大返回（tokens）</Label>
            <Input v-model.number="generation.max_tokens" type="number" min="1" />
          </div>
          <div class="space-y-1.5">
            <Label>温度</Label>
            <Input v-model.number="generation.temperature" type="number" step="0.1" min="0" max="2" />
          </div>
          <div class="space-y-1.5">
            <Label>Top-P</Label>
            <Input v-model.number="generation.top_p" type="number" step="0.05" min="0" max="1" />
          </div>
        </div>
        <div class="flex justify-end">
          <Button size="sm" @click="saveGeneration"><Save class="h-4 w-4" />保存参数</Button>
        </div>
      </CardContent>
    </Card>

    <!-- Prompt 编辑 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><MessageSquareText class="h-5 w-5" />Prompt 配置</CardTitle>
        <CardDescription>默认 Prompt 来自源项目；可在此查看、修改并保存。留空则使用内置默认。</CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div class="space-y-1.5">
          <Label>System Prompt</Label>
          <Textarea v-model="prompts.system_prompt" rows="8" class="font-mono text-xs" />
        </div>
        <div class="space-y-1.5">
          <Label>User Prompt（模板，含 <code class="text-xs">context</code> / <code class="text-xs">chunk</code> 占位符）</Label>
          <Textarea v-model="prompts.user_prompt" rows="8" class="font-mono text-xs" />
        </div>
        <div class="flex justify-end">
          <Button size="sm" @click="savePrompts"><Save class="h-4 w-4" />保存 Prompt</Button>
        </div>
      </CardContent>
    </Card>

    <!-- 文本输入 + 解析 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><FileText class="h-5 w-5" />待解析文本</CardTitle>
        <CardDescription>粘贴小说原文；将按段落 / 句子边界分段后逐段调用 LLM。</CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Textarea v-model="text" rows="10" placeholder="在此粘贴要解析的小说文本…" :disabled="busy" />

        <div class="flex flex-wrap items-center gap-3">
          <Button @click="doParse" :disabled="busy || !text.trim()">
            <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
            <ScanText v-else class="h-4 w-4" />
            {{ busy ? '解析中…' : '开始解析' }}
          </Button>
          <span v-if="!busy" class="text-sm text-muted-foreground">共 {{ text.trim().length }} 字</span>
        </div>

        <!-- 进行中（实时进度 + 日志） -->
        <Alert v-if="task" variant="info" class="items-center">
          <template #icon><Loader2 class="h-4 w-4 shrink-0 animate-spin" /></template>
          <div class="min-w-0 flex-1 space-y-2">
            <div class="flex items-center justify-between gap-3">
              <span class="truncate">{{ task.current || '解析中…' }}</span>
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

        <!-- 结果 -->
        <div v-if="result" class="space-y-3">
          <Alert variant="default" class="items-center">
            <template #icon><CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-500" /></template>
            <span class="flex-1 truncate">
              解析完成：共 {{ result.count }} 条 · 讲者：{{ result.speakers.join('、') }}
            </span>
            <Button variant="outline" size="sm" @click="download"><Download class="h-3.5 w-3.5" />下载 JSON</Button>
            <Button v-if="inTauri" variant="outline" size="sm" @click="openDir"><FolderOpen class="h-3.5 w-3.5" />打开目录</Button>
          </Alert>

          <div class="overflow-x-auto rounded-md border">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-muted-foreground">
                  <th class="px-3 py-1.5 font-medium">#</th>
                  <th class="px-3 py-1.5 font-medium">讲者</th>
                  <th class="px-3 py-1.5 font-medium">文本</th>
                  <th class="px-3 py-1.5 font-medium">演绎指令</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(e, i) in result.entries" :key="i" class="border-b align-top last:border-0">
                  <td class="px-3 py-1.5 text-muted-foreground">{{ i + 1 }}</td>
                  <td class="px-3 py-1.5"><Badge variant="outline">{{ e.speaker }}</Badge></td>
                  <td class="px-3 py-1.5 whitespace-pre-wrap">{{ e.text }}</td>
                  <td class="px-3 py-1.5 whitespace-pre-wrap text-muted-foreground">{{ e.instruct }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </CardContent>
      <CardFooter class="justify-between">
        <span class="text-xs text-muted-foreground">
          输出：<code class="text-xs">output/tts/annotated_script.json</code>（可直接用于 TTS 合成）
        </span>
        <Button variant="outline" size="sm" @click="router.push('/tasks')"><ListTodo class="h-4 w-4" />任务中心</Button>
      </CardFooter>
    </Card>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
