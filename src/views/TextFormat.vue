<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/components/ui/toast'
import { formatText } from '@/api/text'
import { isTauri, downloadFile, reveal, pickFile } from '@/utils/tauri'
import { formatNumber } from '@/utils/format'
import type { TextFormatResult, TextToggles } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Label from '@/components/ui/Label.vue'
import Switch from '@/components/ui/Switch.vue'
import Alert from '@/components/ui/Alert.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import { FileText, ArrowRight, RefreshCw, FolderOpen, Download } from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const { push: toast } = useToast()

const inTauri = isTauri()

const file = ref<{ path: string; name: string } | null>(null)
const toggles = reactive<TextToggles>({
  keep_single_space: false,
  sentence_break: true,
  dialogue_separate: true,
  detect_chapters: true,
  punct_ellipsis: true,
  punct_repeated: true,
  punct_lone_ascii: false,
  punct_quotes: false,
  punct_dash: false,
  live: true,
})

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

const busy = ref(false)
const result = ref<TextFormatResult | null>(null)
const error = ref('')

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  if (settings.config) Object.assign(toggles, settings.config.text)
})

async function choose() {
  const picked = await pickFile([{ name: '文本文件', extensions: ['txt'] }])
  if (picked) {
    file.value = { path: picked.path, name: picked.name }
    error.value = ''
  }
}

async function run(auto = false) {
  if (!file.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const r = await formatText(file.value.path, { ...toggles })
    result.value = r
    project.recordText(r)
    await settings.save({ text: { ...toggles } }).catch(() => {})
    if (!auto) toast({ title: '排版完成', variant: 'success', description: r.output_path })
  } catch (e: any) {
    error.value = e?.message || '排版失败'
    if (!auto) toast({ title: '排版失败', variant: 'destructive', description: error.value })
  } finally {
    busy.value = false
  }
}

// Live: reformat automatically when a toggle flips and a result already exists.
watch(
  () => Object.values(toggles).join('|'),
  () => {
    if (toggles.live && file.value && result.value) run(true)
  },
)

function goNext() {
  if (project.textOutput) router.push('/book')
  else toast({ title: '请先完成排版', variant: 'destructive' })
}

function download(p: string) {
  downloadFile('text', p)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">文本排版</h1>
      <p class="text-muted-foreground mt-1">
        将小说原文规整为段落与标点，输出到 <code class="text-xs">output/text/</code>。
      </p>
    </div>

    <!-- 选择文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <FileText class="h-5 w-5" />选择文件
        </CardTitle>
      </CardHeader>
      <CardContent class="flex items-center gap-3">
        <Button @click="choose" :disabled="busy">选择 TXT 文件</Button>
        <template v-if="file">
          <span class="text-sm font-medium">{{ file.name }}</span>
          <span class="text-xs text-muted-foreground truncate max-w-[260px]" :title="file.path">{{ file.path }}</span>
        </template>
        <span v-else class="text-sm text-muted-foreground">尚未选择文件</span>
      </CardContent>
    </Card>

    <!-- 排版选项 -->
    <Card>
      <CardHeader>
        <CardTitle>排版选项</CardTitle>
      </CardHeader>
      <CardContent>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3">
          <div v-for="t in TOGGLES" :key="t.key" class="flex items-center justify-between">
            <Label class="font-normal">{{ t.label }}</Label>
            <Switch :model-value="toggles[t.key]" @update:model-value="toggles[t.key] = $event" />
          </div>
        </div>
      </CardContent>
    </Card>

    <!-- 操作 -->
    <div class="flex flex-wrap items-center gap-3">
      <Button @click="run()" :disabled="busy || !file">
        <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': busy }" />
        {{ busy ? '排版中…' : '开始排版' }}
      </Button>
      <Button variant="outline" @click="goNext" :disabled="!result">
        前往下一步<ArrowRight class="h-4 w-4" />
      </Button>
    </div>

    <Alert v-if="error" variant="destructive">{{ error }}</Alert>

    <!-- 结果 -->
    <Card v-if="result">
      <CardHeader>
        <CardTitle>排版结果</CardTitle>
        <p class="text-xs text-muted-foreground">编码 {{ result.encoding }} · 输出 {{ result.output_path }}</p>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="flex flex-wrap gap-6">
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(result.stats.chars) }}</div>
            <div class="text-xs text-muted-foreground">字数</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(result.stats.paras) }}</div>
            <div class="text-xs text-muted-foreground">段落</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(result.stats.chapters) }}</div>
            <div class="text-xs text-muted-foreground">章节</div>
          </div>
        </div>
        <div>
          <div class="mb-1 text-xs text-muted-foreground">预览（前 2000 字）</div>
          <ScrollArea class="h-64 rounded-md border">
            <pre class="whitespace-pre-wrap p-3 text-sm">{{ result.preview }}</pre>
          </ScrollArea>
        </div>
      </CardContent>
      <CardFooter class="flex-wrap justify-between">
        <div class="flex items-center gap-2">
          <Button variant="outline" size="sm" @click="download(result.output_path)">
            <Download class="h-4 w-4" />下载
          </Button>
          <Button v-if="inTauri" variant="outline" size="sm" @click="reveal(result.output_path)">
            <FolderOpen class="h-4 w-4" />打开
          </Button>
        </div>
        <Button size="sm" @click="goNext">前往下一步（分册切割）<ArrowRight class="h-4 w-4" /></Button>
      </CardFooter>
    </Card>
  </div>
</template>
