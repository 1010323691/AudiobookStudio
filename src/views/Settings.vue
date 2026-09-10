<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { useToast } from '@/components/ui/toast'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { health } from '@/api/client'
import type { AppConfig, TextToggles } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Switch from '@/components/ui/Switch.vue'
import Select from '@/components/ui/Select.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import {
  Save,
  Monitor,
  Sun,
  Moon,
  RefreshCw,
  Palette,
  FolderCog,
  Type,
  BookOpen,
  Server,
  SlidersHorizontal,
  MessageSquareText,
  ShieldCheck,
  AudioLines,
  FileVideo,
  ScrollText,
  Mic,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const app = useAppStore()
const { push: toast } = useToast()
const { workspaceSet } = useWorkspaceGate()

const draft = ref<AppConfig | null>(null)
const saving = ref(false)
const healthInfo = ref<{ ok: boolean; service: string; port: number } | null>(null)

const TOGGLES: { key: keyof TextToggles; label: string }[] = [
  { key: 'sentence_break', label: '断句换段' },
  { key: 'dialogue_separate', label: '对话独立成段' },
  { key: 'detect_chapters', label: '识别章节标题' },
  { key: 'keep_single_space', label: '保留单个空格' },
  { key: 'punct_ellipsis', label: '省略号统一' },
  { key: 'punct_repeated', label: '合并重复标点' },
  { key: 'punct_quotes', label: '引号成对' },
  { key: 'punct_lone_ascii', label: '半角标点转全角' },
  { key: 'punct_dash', label: '破折号统一' },
  { key: 'live', label: '实时预览' },
]

const THEMES = [
  { key: 'system', label: '跟随系统', icon: Monitor },
  { key: 'light', label: '浅色', icon: Sun },
  { key: 'dark', label: '深色', icon: Moon },
]

onMounted(async () => {
  app.ping()
  if (!settings.loaded) await settings.load()
  if (settings.config) draft.value = JSON.parse(JSON.stringify(settings.config))
  try {
    healthInfo.value = await health()
  } catch {
    healthInfo.value = null
  }
})

function setTheme(theme: string) {
  if (!draft.value) return
  draft.value.ui.theme = theme
  settings.applyTheme(theme) // apply immediately; persisted on 保存
}

async function save() {
  if (!draft.value) return
  saving.value = true
  const ok = await settings.save(draft.value)
  saving.value = false
  if (ok) toast({ title: '设置已保存', variant: 'success', description: '已写入工作空间的 config/app.json，重启后自动恢复。' })
  else toast({ title: '保存失败', variant: 'destructive' })
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">设置</h1>
        <p class="mt-1 text-muted-foreground">统一配置（工作空间目录下的 <code class="text-xs">config/app.json</code>），持久化并在重启后恢复。</p>
      </div>
      <Button @click="save" :disabled="saving || !draft || !workspaceSet">
        <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
      </Button>
    </div>

    <Alert v-if="!draft" variant="destructive">
      无法加载配置——请确认后端已启动（127.0.0.1:8642）。
    </Alert>

    <template v-else>
      <!-- 未设工作空间时提示（配置随工程，未开工不可保存） -->
      <Alert v-if="!workspaceSet" variant="warning">
        尚未选择工作空间。配置按工程（工作空间）管理，需先在「开始」页选择一个文件夹后才能保存设置。
      </Alert>

      <!-- 后端状态 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><RefreshCw class="h-5 w-5" />后端状态</CardTitle>
        </CardHeader>
        <CardContent class="flex flex-wrap items-center gap-3">
          <Badge :variant="app.backendUp ? 'success' : 'destructive'">
            <span class="mr-1 inline-block h-2 w-2 rounded-full" :class="app.backendUp ? 'bg-emerald-500' : 'bg-red-500'" />
            {{ app.backendUp ? '已连接' : '未连接' }}
          </Badge>
          <span v-if="healthInfo" class="text-sm text-muted-foreground">
            {{ healthInfo.service }} · 端口 {{ healthInfo.port }}
          </span>
          <Button variant="outline" size="sm" class="ml-auto" @click="app.ping()">
            <RefreshCw class="h-4 w-4" />重新检测
          </Button>
        </CardContent>
      </Card>

      <!-- 外观 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Palette class="h-5 w-5" />外观主题</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="flex gap-2">
            <Button
              v-for="t in THEMES"
              :key="t.key"
              :variant="draft.ui.theme === t.key ? 'default' : 'outline'"
              @click="setTheme(t.key)"
            >
              <component :is="t.icon" class="h-4 w-4" />{{ t.label }}
            </Button>
          </div>
        </CardContent>
      </Card>

      <!-- 工作空间（在「开始」页设置，此处只读） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><FolderCog class="h-5 w-5" />工作空间</CardTitle>
        </CardHeader>
        <CardContent class="space-y-2">
          <Label>目录</Label>
          <Input
            :model-value="draft.paths.working_dir"
            readonly
            :placeholder="workspaceSet ? '' : '未设置——请在「开始」页选择文件夹'"
          />
          <p class="text-xs text-muted-foreground">
            工作空间在「开始」页选择。配置、日志与全部产物都按固定子目录保存在该目录下：config/ · logs/ · 00_temp/ … 07_output/。
          </p>
        </CardContent>
      </Card>

      <!-- 文本排版 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Type class="h-5 w-5" />文本排版</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div v-for="t in TOGGLES" :key="t.key" class="flex items-center justify-between">
              <Label class="font-normal">{{ t.label }}</Label>
              <Switch v-model="draft.text[t.key]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 分册切割 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><BookOpen class="h-5 w-5" />分册切割</CardTitle>
        </CardHeader>
        <CardContent class="flex items-center gap-3">
          <Label class="w-28 shrink-0">目标字数</Label>
          <Input v-model.number="draft.book.target_chars" type="number" min="1" class="max-w-[180px]" />
          <span class="text-xs text-muted-foreground">每分册约多少字</span>
        </CardContent>
      </Card>

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
            <Input v-model="draft.llm.base_url" placeholder="http://localhost:11434/v1" />
          </div>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label>API Key</Label>
              <Input v-model="draft.llm.api_key" placeholder="local" />
            </div>
            <div class="space-y-1.5">
              <Label>模型名称</Label>
              <Input v-model="draft.llm.model_name" placeholder="如 qwen3:14b（必填）" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 生成参数 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><SlidersHorizontal class="h-5 w-5" />生成参数</CardTitle>
          <CardDescription>分段大小与采样设置，作用于文本解析 / Speaker 检查的每次 LLM 请求。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div class="space-y-1.5">
              <Label>分段大小（字）</Label>
              <Input v-model.number="draft.generation.chunk_size" type="number" min="1" />
            </div>
            <div class="space-y-1.5">
              <Label>最大返回（tokens）</Label>
              <Input v-model.number="draft.generation.max_tokens" type="number" min="1" />
            </div>
            <div class="space-y-1.5">
              <Label>温度</Label>
              <Input v-model.number="draft.generation.temperature" type="number" step="0.1" min="0" max="2" />
            </div>
            <div class="space-y-1.5">
              <Label>Top-P</Label>
              <Input v-model.number="draft.generation.top_p" type="number" step="0.05" min="0" max="1" />
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>并发数（同时解析的文件数）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.max_concurrency" type="number" min="1" step="1" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                受 LLM 服务 / 资源限制；超出并发的文件会排队，待有槽位时逐个进行。
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Prompt 配置 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><MessageSquareText class="h-5 w-5" />Prompt 配置</CardTitle>
          <CardDescription>文本解析的默认 Prompt 来自源项目；可在此查看、修改并保存。留空则使用内置默认。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1.5">
            <Label>System Prompt</Label>
            <Textarea v-model="draft.prompts.system_prompt" rows="8" class="font-mono text-xs" />
          </div>
          <div class="space-y-1.5">
            <Label>User Prompt（模板，含 <code class="text-xs">context</code> / <code class="text-xs">chunk</code> 占位符）</Label>
            <Textarea v-model="draft.prompts.user_prompt" rows="8" class="font-mono text-xs" />
          </div>
        </CardContent>
      </Card>

      <!-- Speaker 检查（独立于解析提示词 / 生成参数） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><ShieldCheck class="h-5 w-5" />Speaker 检查</CardTitle>
          <CardDescription>
            解析完成后，对每条用「前后各 N 条」的上下文让 LLM 重新判断 <code class="text-xs">speaker</code>，
            不同则只改 <code class="text-xs">speaker</code>，结果写入 <code class="text-xs">&lt;文件基名&gt;_checked.json</code>
            （原始 <code class="text-xs">.json</code> 不变）。检查提示词与上方解析提示词完全独立。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1.5">
            <Label>上下文窗口大小（当前条前后各取 N 条，共 2N+1 条）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.speaker_check.context_window" type="number" min="0" step="1" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                例如 4 → 前 4 条 + 当前条 + 后 4 条，共 9 条送入 LLM。
              </span>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>检查 System Prompt</Label>
            <Textarea v-model="draft.speaker_check.system_prompt" rows="6" class="font-mono text-xs" />
          </div>
          <div class="space-y-1.5">
            <Label>检查 User Prompt（模板，含 <code class="text-xs">context</code> 占位符）</Label>
            <Textarea v-model="draft.speaker_check.user_prompt" rows="6" class="font-mono text-xs" />
          </div>
        </CardContent>
      </Card>

      <!-- 音频分集 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioLines class="h-5 w-5" />音频分集</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">目标时长</Label>
              <Input v-model="draft.audio.target_duration" placeholder="10:00" class="max-w-[120px]" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">智能对齐</Label>
              <Switch v-model="draft.audio.smart_align" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">偏移容差</Label>
              <Input v-model.number="draft.audio.align_tolerance" type="number" min="5" max="30" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">命名格式</Label>
              <Input v-model="draft.audio.naming_format" placeholder="第 {} 集" class="max-w-[160px]" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">起始编号</Label>
              <Input v-model="draft.audio.start_number" class="max-w-[100px]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- FFmpeg -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><FileVideo class="h-5 w-5" />FFmpeg</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-2">
            <Label>ffmpeg 路径</Label>
            <Input v-model="draft.ffmpeg.ffmpeg_path" placeholder="留空则从 PATH 解析" />
          </div>
          <div class="space-y-2">
            <Label>ffprobe 路径</Label>
            <Input v-model="draft.ffmpeg.ffprobe_path" placeholder="留空则从 PATH 解析" />
          </div>
          <p class="text-xs text-muted-foreground">音频分集依赖原生 FFmpeg / ffprobe。若系统 PATH 中已有，可留空。</p>
        </CardContent>
      </Card>

      <!-- 日志 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><ScrollText class="h-5 w-5" />日志</CardTitle>
        </CardHeader>
        <CardContent class="flex items-center gap-3">
          <Label class="w-24 shrink-0">级别</Label>
          <Select v-model="draft.log.level" class="max-w-[180px]">
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </Select>
        </CardContent>
      </Card>

      <!-- TTS（本地 Qwen3-TTS 引擎） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Mic class="h-5 w-5" />TTS 引擎
            <Badge variant="success" class="ml-1">本地引擎</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <p class="text-xs text-muted-foreground">
            本地 Qwen3-TTS 引擎，运行在独立的 <code class="text-xs">.venv-tts</code> 环境（Python 3.10 · GPU）。
            若尚未安装，请在项目根目录运行 <code class="text-xs">install_tts_env.ps1</code>。
          </p>
          <div class="grid gap-3 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label>模型</Label>
              <Input v-model="draft.tts.model" placeholder="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice" />
            </div>
            <div class="space-y-1.5">
              <Label>音色 (speaker)</Label>
              <Input v-model="draft.tts.speaker" placeholder="serena" />
            </div>
            <div class="space-y-1.5">
              <Label>语言</Label>
              <Input v-model="draft.tts.language" placeholder="chinese" />
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-16 shrink-0">设备</Label>
              <Select v-model="draft.tts.device" class="max-w-[150px]">
                <option value="auto">自动 (auto)</option>
                <option value="cuda">CUDA（GPU）</option>
                <option value="cpu">CPU</option>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      <div class="flex justify-end">
        <Button @click="save" :disabled="saving || !workspaceSet">
          <Save class="h-4 w-4" />{{ saving ? '保存中…' : '保存设置' }}
        </Button>
      </div>
    </template>
  </div>
</template>
