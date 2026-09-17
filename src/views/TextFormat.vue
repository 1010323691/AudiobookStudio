<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { formatText } from '@/api/text'
import { analyzeBook, splitBook } from '@/api/book'
import { downloadFile, pickFile } from '@/utils/fileops'
import { formatNumber } from '@/utils/format'
import type { BookAnalyzeResult, BookSplitResult, TextFormatResult, TextToggles } from '@/types'

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
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import Table from '@/components/ui/Table.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableRow from '@/components/ui/TableRow.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableCell from '@/components/ui/TableCell.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { FileText, ArrowRight, RefreshCw, Download, Scissors, AlertTriangle } from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

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

const busyFormat = ref(false)
const formatResult = ref<TextFormatResult | null>(null)
const busyAnalyze = ref(false)
const analysis = ref<BookAnalyzeResult | null>(null)
const busySplit = ref(false)
const splitResult = ref<BookSplitResult | null>(null)
const asZip = ref(false)
// 零章节时用户选择「不处理，按整本继续」（whole_book 分册）。
const wholeBookArmed = ref(false)
// 章节序号警告（缺号/重号/乱序）被「不处理，继续」关闭（非阻断，仅为提示）。
const seqWarningDismissed = ref(false)
const error = ref('')

const zeroChapters = computed(() => !!analysis.value && analysis.value.chapter_count === 0)
const showWholeBookPrompt = computed(() => !!analysis.value && zeroChapters.value && !wholeBookArmed.value)
const wholeBookArmedView = computed(() => !!analysis.value && zeroChapters.value && wholeBookArmed.value)
const showSeqWarning = computed(
  () => !!analysis.value && !zeroChapters.value && analysis.value.sequence.hasIssues && !seqWarningDismissed.value,
)
const splitEnabled = computed(
  () => !!analysis.value && (analysis.value.chapter_count > 0 || wholeBookArmed.value) && !busySplit.value && workspaceSet.value,
)

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  if (settings.config) Object.assign(toggles, settings.config.text)
})

// 清掉排版之后的所有状态（分析 / 分册 / 选择标记）——重新排版或换文件后
// 一切结果必须重新推导，绝不让旧状态存活。
function resetDownstream() {
  formatResult.value = null
  analysis.value = null
  splitResult.value = null
  error.value = ''
  wholeBookArmed.value = false
  seqWarningDismissed.value = false
}

// 「重新上传原文」：整页回到选文件状态（排版开关保留——它们是持久配置）。
function resetPage() {
  file.value = null
  resetDownstream()
}

async function choose() {
  const picked = await pickFile([{ name: '文本文件', extensions: ['txt'] }])
  if (picked) {
    file.value = { path: picked.path, name: picked.name }
    resetDownstream()
  }
}

// 排版 → 自动章节分析（对排版输出分析；排版失败则不分析）。
async function run(auto = false) {
  if (!file.value || busyFormat.value || busyAnalyze.value) return
  busyFormat.value = true
  resetDownstream()
  try {
    const r = await formatText(file.value.path, { ...toggles })
    formatResult.value = r
    await settings.save({ text: { ...toggles } }).catch(() => {})
    if (!auto) toast({ title: '排版完成', variant: 'success', description: r.output_path })
    await analyzeAfterFormat(auto)
  } catch (e: any) {
    error.value = e?.message || '排版失败'
    if (!auto) toast({ title: '排版失败', variant: 'destructive', description: error.value })
  } finally {
    busyFormat.value = false
  }
}

async function analyzeAfterFormat(auto: boolean) {
  if (!formatResult.value) return
  busyAnalyze.value = true
  try {
    analysis.value = await analyzeBook(formatResult.value.output_path)
    // 零章节的 error 是「提示」而非失败——由整本/重传提示条承接，不进 error。
  } catch (e: any) {
    error.value = e?.message || '章节分析失败'
    toast({ title: '章节分析失败', variant: 'destructive', description: error.value })
  } finally {
    busyAnalyze.value = false
  }
}

// Live: reformat (and re-analyze) automatically when a toggle flips and a
// result already exists.
watch(
  () => Object.values(toggles).join('|'),
  () => {
    if (toggles.live && file.value && formatResult.value) run(true)
  },
)

function armWholeBook() {
  wholeBookArmed.value = true
}

function proceedAnyway() {
  seqWarningDismissed.value = true
}

async function split() {
  if (!splitEnabled.value || !formatResult.value || busySplit.value) return
  busySplit.value = true
  error.value = ''
  const whole = zeroChapters.value && wholeBookArmed.value
  try {
    const r = await splitBook(formatResult.value.output_path, { asZip: asZip.value, wholeBook: whole })
    splitResult.value = r
    toast(
      whole
        ? { title: '分册完成', variant: 'success', description: `已生成整本文件 ${r.files[0]?.name ?? ''}` }
        : { title: '分册完成', variant: 'success', description: `生成 ${r.file_count} 个分册文件` },
    )
  } catch (e: any) {
    error.value = e?.message || '分册失败'
    toast({ title: '分册失败', variant: 'destructive', description: error.value })
  } finally {
    busySplit.value = false
  }
}

function goNext() {
  if (splitResult.value?.files.length) router.push('/script')
  else toast({ title: '请先完成分册', variant: 'destructive' })
}

function download(p: string) {
  downloadFile('01_input', p)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">排版与分册</h1>
      <p class="text-muted-foreground mt-1">
        将小说原文规整为段落与标点（输出 <code class="text-xs">01_input/</code>，原件保留），自动分析章节并按章节边界分册——
        每章一个文件（<code class="text-xs">02_split_text/</code>）；绝不重编号，所有分册拼接可精确复原原文。
      </p>
    </div>

    <WorkspaceGateAlert />

    <!-- 选择文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <FileText class="h-5 w-5" />选择文件
        </CardTitle>
      </CardHeader>
      <CardContent class="flex items-center gap-3">
        <Button @click="choose" :disabled="busyFormat || busyAnalyze || busySplit">选择 TXT 文件</Button>
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
    <div class="flex flex-wrap items-center gap-x-5 gap-y-3">
      <Button @click="run()" :disabled="busyFormat || busyAnalyze || !file || !workspaceSet">
        <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': busyFormat || busyAnalyze }" />
        {{ busyFormat ? '排版中…' : busyAnalyze ? '章节分析中…' : '开始排版' }}
      </Button>
      <div class="flex items-center gap-2">
        <Label class="font-normal">同时打包</Label>
        <Switch v-model="asZip" />
        <span class="text-xs text-muted-foreground">额外生成一个 .zip</span>
      </div>
      <Button @click="split" :disabled="!splitEnabled">
        <Scissors class="h-4 w-4" />
        {{ busySplit ? '分册中…' : (wholeBookArmedView ? '分册（整本）' : '开始分册') }}
      </Button>
      <Button variant="outline" @click="goNext" :disabled="!splitResult">
        前往下一步<ArrowRight class="h-4 w-4" />
      </Button>
    </div>

    <Alert v-if="error" variant="destructive">{{ error }}</Alert>

    <!-- 零章节：整本继续 / 重新上传 二选一 -->
    <Alert v-else-if="showWholeBookPrompt" variant="warning">
      <AlertTriangle class="h-4 w-4 shrink-0" />
      <div class="space-y-2">
        <p>{{ analysis?.error }}</p>
        <div class="flex gap-2">
          <Button size="sm" @click="armWholeBook">不处理，按整本继续</Button>
          <Button size="sm" variant="outline" @click="resetPage">重新上传原文</Button>
        </div>
      </div>
    </Alert>
    <Alert v-else-if="wholeBookArmedView" variant="info">
      已按整本处理：全部文本将写为单个文件 <code class="text-xs">{{ analysis?.base }} 全书.txt</code>。
    </Alert>

    <!-- 排版结果 -->
    <Card v-if="formatResult">
      <CardHeader>
        <CardTitle>排版结果</CardTitle>
        <p class="text-xs text-muted-foreground">编码 {{ formatResult.encoding }} · 输出 {{ formatResult.output_path }}</p>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="flex flex-wrap gap-6">
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.chars) }}</div>
            <div class="text-xs text-muted-foreground">字数</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.paras) }}</div>
            <div class="text-xs text-muted-foreground">段落</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.chapters) }}</div>
            <div class="text-xs text-muted-foreground">章节标题（排版口径，含 节/回/卷 等）</div>
          </div>
        </div>
        <div>
          <div class="mb-1 text-xs text-muted-foreground">预览（前 2000 字）</div>
          <ScrollArea class="h-64 rounded-md border">
            <pre class="whitespace-pre-wrap p-3 text-sm">{{ formatResult.preview }}</pre>
          </ScrollArea>
        </div>
      </CardContent>
      <CardFooter>
        <Button variant="outline" size="sm" @click="download(formatResult.output_path)">
          <Download class="h-4 w-4" />下载
        </Button>
      </CardFooter>
    </Card>

    <!-- 章节分析 -->
    <Card v-if="analysis && !zeroChapters">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Scissors class="h-5 w-5" />章节分析</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>总字数 <b>{{ formatNumber(analysis.total_chars) }}</b></span>
          <span>章节 <b>{{ analysis.chapter_count }}</b></span>
          <span>编码 <b>{{ analysis.encoding }}</b></span>
        </div>
      </CardHeader>
      <CardContent class="space-y-5">
        <!-- 章节序号警告（非阻断）：列出具体缺号/重号/乱序 + 预期格式 + 两个选择 -->
        <Alert v-if="showSeqWarning" variant="warning">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <div class="space-y-1">
            <p class="font-medium">检测到章节号问题（不影响分册，可继续）</p>
            <ul class="list-disc pl-5 space-y-0.5">
              <li v-for="(g, gi) in analysis.sequence.gaps" :key="'g' + gi">
                缺号：第{{ g.after }}章之后缺少 {{ g.missing.map((n) => `第${n}章`).join('、') }}
              </li>
              <li v-for="(d, di) in analysis.sequence.duplicates" :key="'d' + di">
                重号：第{{ d.num }}章重复出现（第{{ d.seq }}个章节）
              </li>
              <li v-for="(d, di) in analysis.sequence.disorder" :key="'o' + di">
                乱序：第{{ d.num }}章出现在第{{ d.prevNum }}章之后
              </li>
            </ul>
            <p class="text-muted-foreground">
              系统识别的章节格式：{{ analysis.expected_format }}。每章切一个文件，分册编号为顺序号，缺号不影响分册。
            </p>
            <div class="flex gap-2 pt-1">
              <Button size="sm" @click="proceedAnyway">不处理，继续</Button>
              <Button size="sm" variant="outline" @click="resetPage">重新上传原文</Button>
            </div>
          </div>
        </Alert>

        <!-- 章节列表（含分册文件名预览） -->
        <div v-if="analysis.chapters.length">
          <div class="mb-2 text-sm font-medium">章节列表（{{ analysis.chapter_count }}）</div>
          <ScrollArea class="h-72 rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead class="w-14">#</TableHead>
                  <TableHead class="w-24">编号</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead class="w-24 text-right">字数</TableHead>
                  <TableHead>分册文件</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow v-for="c in analysis.chapters" :key="c.seq">
                  <TableCell class="text-muted-foreground">{{ c.seq }}</TableCell>
                  <TableCell>第{{ c.numStr }}章</TableCell>
                  <TableCell class="max-w-[240px] truncate" :title="c.title">{{ c.title || '—' }}</TableCell>
                  <TableCell class="text-right">{{ formatNumber(c.chars) }}</TableCell>
                  <TableCell class="max-w-[320px] truncate" :title="analysis.filenames[c.seq - 1]">
                    {{ analysis.filenames[c.seq - 1] }}
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      </CardContent>
    </Card>

    <!-- 零章节：将输出的单个文件 -->
    <Card v-else-if="analysis && zeroChapters">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Scissors class="h-5 w-5" />章节分析</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>总字数 <b>{{ formatNumber(analysis.total_chars) }}</b></span>
          <span>章节 <b>0</b></span>
          <span>编码 <b>{{ analysis.encoding }}</b></span>
        </div>
      </CardHeader>
      <CardContent>
        <p class="text-sm text-muted-foreground">
          未检测到任何章节（系统识别的格式：{{ analysis.expected_format }}）。
          <template v-if="wholeBookArmed">
            将输出单个文件：<code class="text-xs">{{ analysis.base }} 全书.txt</code>
          </template>
        </p>
      </CardContent>
    </Card>

    <!-- 分册结果 -->
    <Card v-if="splitResult">
      <CardHeader>
        <CardTitle>分册结果</CardTitle>
        <p class="text-xs text-muted-foreground">
          共 {{ splitResult.file_count }} 个文件 · 输出目录 {{ splitResult.output_dir }}
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>文件名</TableHead>
              <TableHead class="w-24 text-right">字数</TableHead>
              <TableHead class="w-40"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow v-for="f in splitResult.files" :key="f.path">
              <TableCell class="max-w-[420px] truncate" :title="f.path">{{ f.name }}</TableCell>
              <TableCell class="text-right">{{ formatNumber(f.chars) }}</TableCell>
              <TableCell class="text-right">
                <div class="flex items-center justify-end gap-1">
                  <Button variant="ghost" size="sm" @click="downloadFile('02_split_text', f.path)">
                    <Download class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
      <CardFooter class="justify-between">
        <Button v-if="splitResult.zip_path" variant="outline" size="sm" @click="downloadFile('02_split_text', splitResult.zip_path!)">
          <Download class="h-4 w-4" />下载 zip
        </Button>
        <Button size="sm" @click="goNext">前往下一步（文本解析）<ArrowRight class="h-4 w-4" /></Button>
      </CardFooter>
    </Card>
  </div>
</template>
