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
  Server,
  SlidersHorizontal,
  MessageSquareText,
  AudioWaveform,
  AudioLines,
  Music4,
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
        <CardContent class="space-y-4">
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
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">解析日志显示</Label>
              <Switch v-model="draft.ui.show_parse_logs" />
            </div>
            <p class="text-xs text-muted-foreground">
              开启 = 文本解析页显示「解析进度」日志区（每文件实时日志 + 流式反馈，三性能指标在日志区内）；
              关闭（默认）= 隐藏整个日志区，三性能指标移到「开始处理」按钮下方。
              每文件行内的进度条 / 速度 / 状态始终显示，不受此开关影响。
            </p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">音频分集导航项</Label>
              <Switch v-model="draft.ui.show_audio_split" />
            </div>
            <p class="text-xs text-muted-foreground">
              开启 = 侧边栏显示「音频分集」导航项；
              关闭（默认）= 隐藏该导航项（音频合并页的「前往音频分集」按钮随之隐藏），
              页面本身仍保留、可直接访问。
            </p>
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
          <CardDescription>分段大小与采样设置，作用于文本解析的每次 LLM 请求（含解析内校验 / 抽样）。</CardDescription>
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
          <div class="space-y-1.5">
            <Label>归属抽样率（0 = 关闭）</Label>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model.number="draft.generation.spot_check_rate" type="number" step="0.01" min="0" max="0.5" class="max-w-[8rem]" />
              <span class="text-xs text-muted-foreground">
                解析完成后按比例抽条目重判 speaker（1/3 纯随机做整书错误率仪表 + 2/3 风险加权：
                无归属标签 / ≤10 字 / 多角色场景），高置信改判直接写入解析结果。
                每本的纯随机桶错误率会记入任务日志与
                <code class="text-xs">config/spot_check_history.json</code>
                ——连续几本 &lt;1% 时可在此手动调低（不会自动降）。
              </span>
            </div>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div class="flex items-center justify-between">
              <Label class="font-normal">角色匹配检查</Label>
              <Switch v-model="draft.generation.check_boundary_speakers" />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">断句失败校验</Label>
              <Switch v-model="draft.generation.revalidate_splits" />
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">纯归属标签删除</Label>
              <Switch v-model="draft.generation.delete_saying_tags" />
            </div>
            <div class="flex items-center justify-between">
              <div class="flex flex-col">
                <Label class="font-normal">超长段落检查</Label>
                <span class="text-xs text-muted-foreground">
                  超长条目先 LLM 语义重切、后机械分段兜底
                </span>
              </div>
              <div class="flex items-center gap-2">
                <Input
                  v-model.number="draft.generation.max_paragraph_chars"
                  type="number" min="10" step="10" class="w-20"
                />
                <Switch v-model="draft.generation.check_long_paragraphs" />
              </div>
            </div>
            <div class="flex items-center justify-between">
              <Label class="font-normal">纯标点条目吸收</Label>
              <Switch v-model="draft.generation.absorb_punct_entries" />
            </div>
          </div>
          <p class="text-xs text-muted-foreground">
            解析内的五个检查阶段：角色匹配检查（chunk 切割会切断跨段上下文，用跨 chunk 上下文窗口
            重判边界两侧条目，上下文仅辅助判断、只有目标条目可被修改）、断句失败校验（疑似断句失败的
            条目逐条重判）、纯归属标签删除（独立短标签条确定性删除，不经 LLM）、超长段落检查
            （超过右侧字数上限的条目先带上下文重跑 LLM 重切，仍超长则按句界 / 子句界 / 定宽机械
            切开——硬保证最终没有任何段落超过上限）、纯标点条目吸收（整条无内容的「……」类条目
            并入相邻 NARRATOR，无邻接则删除）。关闭 = 解析时跳过该阶段并记录日志。
          </p>
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

      <!-- TTS 子批规划 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioWaveform class="h-5 w-5" />TTS 子批规划</CardTitle>
          <CardDescription>音频合成与角色配音·克隆共用的张量批规划检查（默认全开 = 现有行为不变）。关闭某项 = 规划子批时跳过该约束，运行日志留一行「…已关闭（配置）」；实测显存的动态调节（VramGovernor）不受开关影响、始终生效。「批内行数」上限不在此列（合成页手动输入）。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">段长分档</Label>
              <Switch v-model="draft.tts.planner_length_bands" />
            </div>
            <p class="text-xs text-muted-foreground">短段跑满上限、长段自动降档、&gt;2048 字单独成批（O(L²) 注意力峰值随批内最长行二次增长）。</p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">单批字符上限</Label>
              <Switch v-model="draft.tts.planner_batch_chars" />
            </div>
            <p class="text-xs text-muted-foreground">一个子批的总字数 ≤ 上限（防超大 prefill / TDR 挂起）。</p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">超长行独批</Label>
              <Switch v-model="draft.tts.planner_seq_chars" />
            </div>
            <p class="text-xs text-muted-foreground">&gt;2500 字的行不与短行混批（独批；独批大小仍受显存约束）。</p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">批内长度比</Label>
              <Switch v-model="draft.tts.planner_length_ratio" />
            </div>
            <p class="text-xs text-muted-foreground">批内最长/最短 ≤3（防短行按混入长行的解码上限跑全程）。</p>
          </div>
          <div class="space-y-1">
            <div class="flex items-center justify-between">
              <Label class="font-normal">显存静态估算</Label>
              <Switch v-model="draft.tts.planner_vram" />
            </div>
            <p class="text-xs text-muted-foreground">静态 L² 显存估算门（对短行偏保守）；关闭后实测显存的动态调节仍生效。</p>
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
              <Input v-model="draft.audio.naming_format" placeholder="书名 第 {} 集" class="max-w-[160px]" />
              <span class="text-xs text-muted-foreground">完整文件名，{} 为编号</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">起始编号</Label>
              <Input v-model="draft.audio.start_number" class="max-w-[100px]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 背景音乐 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Music4 class="h-5 w-5" />背景音乐</CardTitle>
          <CardDescription>
            章节级 BGM 匹配与最终混音的默认参数（混音 = 06 旁白 + 音乐库曲目 → 08_bgm）。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">BGM 音量</Label>
              <Input v-model.number="draft.bgm.volume" type="number" step="0.01" min="0" max="1" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">0~1</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">循环策略</Label>
              <Switch v-model="draft.bgm.loop" />
              <span class="text-xs text-muted-foreground">开 = 循环铺满；关 = 只播一遍，其余静音</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">淡入时间</Label>
              <Input v-model.number="draft.bgm.fade_in" type="number" step="0.5" min="0" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒（混音时钳 章节时长/2）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">淡出时间</Label>
              <Input v-model.number="draft.bgm.fade_out" type="number" step="0.5" min="0" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒（混音时钳 章节时长/2）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">最低匹配分</Label>
              <Input v-model.number="draft.bgm.min_match_score" type="number" min="1" step="1" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">低于此分的音乐不进候选</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">分析采样字数</Label>
              <Input v-model.number="draft.bgm.analysis_chars" type="number" min="500" step="500" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">章节 LLM 气氛分析的头/中/尾采样字数</span>
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
