<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/components/ui/toast'
import { analyzeBook, splitBook } from '@/api/book'
import { isTauri, downloadFile, reveal, pickFile } from '@/utils/tauri'
import { formatNumber } from '@/utils/format'
import type { BookAnalyzeResult, BookSplitResult } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import Switch from '@/components/ui/Switch.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import Table from '@/components/ui/Table.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableRow from '@/components/ui/TableRow.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableCell from '@/components/ui/TableCell.vue'
import {
  BookOpen,
  ArrowRight,
  Search,
  Scissors,
  FolderOpen,
  Download,
  AlertTriangle,
  ArrowLeft,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const { push: toast } = useToast()
const inTauri = isTauri()

const file = ref<{ path: string; name: string } | null>(null)
const targetChars = ref<number>(0) // 0 -> fall back to the config default
const asZip = ref(false)

const busyAnalyze = ref(false)
const busySplit = ref(false)
const analysis = ref<BookAnalyzeResult | null>(null)
const splitResult = ref<BookSplitResult | null>(null)
const error = ref('')

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  targetChars.value = settings.config?.book.target_chars ?? 100000
  // Offer the upstream 文本排版 output as a one-click input.
  if (project.textOutput && !file.value) {
    const p = project.textOutput
    file.value = { path: p, name: p.split(/[\\/]/).pop() || p }
  }
})

async function choose() {
  const picked = await pickFile([{ name: '文本文件', extensions: ['txt'] }])
  if (picked) {
    file.value = { path: picked.path, name: picked.name }
    analysis.value = null
    splitResult.value = null
    error.value = ''
  }
}

function useUpstream() {
  const p = project.textOutput
  if (!p) return
  file.value = { path: p, name: p.split(/[\\/]/).pop() || p }
  analysis.value = null
  splitResult.value = null
}

const effTarget = () =>
  targetChars.value > 0 ? targetChars.value : (settings.config?.book.target_chars ?? 100000)

async function analyze() {
  if (!file.value || busyAnalyze.value) return
  busyAnalyze.value = true
  error.value = ''
  splitResult.value = null
  try {
    const r = await analyzeBook(file.value.path, effTarget())
    analysis.value = r
    if (r.error) error.value = r.error
  } catch (e: any) {
    error.value = e?.message || '分析失败'
  } finally {
    busyAnalyze.value = false
  }
}

async function split() {
  if (!file.value || busySplit.value) return
  busySplit.value = true
  error.value = ''
  try {
    const r = await splitBook(file.value.path, { targetChars: effTarget(), asZip: asZip.value })
    splitResult.value = r
    project.recordBook(r)
    toast({ title: '分册完成', variant: 'success', description: `生成 ${r.file_count} 个分册` })
  } catch (e: any) {
    error.value = e?.message || '分册失败'
    toast({ title: '分册失败', variant: 'destructive', description: error.value })
  } finally {
    busySplit.value = false
  }
}

function goNext() {
  if (project.bookOutputs.length) router.push('/tts')
  else toast({ title: '请先完成分册', variant: 'destructive' })
}

function download(path: string, module: string) {
  downloadFile(module, path)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">分册切割</h1>
      <p class="text-muted-foreground mt-1">
        按章节边界把长文均衡切分为若干分册，输出到 <code class="text-xs">output/books/</code>。仅在章节处切割、绝不重编号。
      </p>
    </div>

    <!-- 选择文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <BookOpen class="h-5 w-5" />选择文件
        </CardTitle>
      </CardHeader>
      <CardContent class="flex flex-wrap items-center gap-3">
        <Button @click="choose" :disabled="busyAnalyze || busySplit">选择 TXT 文件</Button>
        <Button
          v-if="project.textOutput && (!file || file.path !== project.textOutput)"
          variant="outline"
          @click="useUpstream"
        >
          <ArrowLeft class="h-4 w-4" />使用「文本排版」输出
        </Button>
        <template v-if="file">
          <span class="text-sm font-medium">{{ file.name }}</span>
          <span class="text-xs text-muted-foreground truncate max-w-[240px]" :title="file.path">{{ file.path }}</span>
        </template>
        <span v-else class="text-sm text-muted-foreground">尚未选择文件</span>
      </CardContent>
    </Card>

    <!-- 参数 -->
    <Card>
      <CardHeader>
        <CardTitle>分册参数</CardTitle>
      </CardHeader>
      <CardContent class="grid gap-4 sm:grid-cols-2">
        <div class="flex items-center gap-3">
          <Label class="w-28 shrink-0">目标字数</Label>
          <Input v-model.number="targetChars" type="number" min="1" placeholder="留空使用默认" class="max-w-[180px]" />
          <span class="text-xs text-muted-foreground">每册约 {{ formatNumber(effTarget()) }} 字</span>
        </div>
        <div class="flex items-center gap-3">
          <Label class="w-28 shrink-0">同时打包</Label>
          <Switch v-model="asZip" />
          <span class="text-xs text-muted-foreground">额外生成一个 .zip</span>
        </div>
      </CardContent>
    </Card>

    <!-- 操作 -->
    <div class="flex flex-wrap items-center gap-3">
      <Button variant="outline" @click="analyze" :disabled="busyAnalyze || !file">
        <Search class="h-4 w-4" />{{ busyAnalyze ? '分析中…' : '分析' }}
      </Button>
      <Button @click="split" :disabled="busySplit || !file || !analysis || !!analysis.error">
        <Scissors class="h-4 w-4" />{{ busySplit ? '分册中…' : '开始分册' }}
      </Button>
      <Button variant="outline" @click="goNext" :disabled="!splitResult">
        前往下一步<ArrowRight class="h-4 w-4" />
      </Button>
    </div>

    <Alert v-if="error" variant="destructive">{{ error }}</Alert>
    <Alert
      v-else-if="analysis?.sequence.hasIssues"
      variant="info"
      class="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
    >
      <AlertTriangle class="h-4 w-4" />
      章节编号存在{{ analysis.sequence.gaps.length ? '缺号' : '' }}{{ analysis.sequence.duplicates.length ? '重复' : '' }}{{ analysis.sequence.disorder.length ? '乱序' : '' }}，
      共 {{ analysis.sequence.gaps.length + analysis.sequence.duplicates.length + analysis.sequence.disorder.length }} 处；不影响分册，仅供参考。
    </Alert>

    <!-- 分析结果 -->
    <Card v-if="analysis">
      <CardHeader>
        <CardTitle>分析结果</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>总字数 <b>{{ formatNumber(analysis.total_chars) }}</b></span>
          <span>章节 <b>{{ analysis.chapter_count }}</b></span>
          <span>分册 <b>{{ analysis.volume_count }}</b></span>
          <span>编码 <b>{{ analysis.encoding }}</b></span>
        </div>
      </CardHeader>
      <CardContent class="space-y-5">
        <!-- 分册预览 -->
        <div>
          <div class="mb-2 text-sm font-medium">分册预览</div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead class="w-16">分册</TableHead>
                <TableHead>章节数</TableHead>
                <TableHead class="text-right">字数</TableHead>
                <TableHead>文件名</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="v in analysis.volumes" :key="v.index">
                <TableCell>
                  <Badge variant="secondary">分册{{ String(v.index + 1).padStart(2, '0') }}</Badge>
                </TableCell>
                <TableCell>{{ v.lastChapter - v.firstChapter + 1 }}</TableCell>
                <TableCell class="text-right">{{ formatNumber(v.chars) }}</TableCell>
                <TableCell class="max-w-[420px] truncate" :title="analysis.filenames[v.index]">
                  {{ analysis.filenames[v.index] }}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>

        <!-- 章节列表 -->
        <div v-if="analysis.chapters.length">
          <div class="mb-2 text-sm font-medium">章节列表（{{ analysis.chapter_count }}）</div>
          <ScrollArea class="h-72 rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead class="w-16">#</TableHead>
                  <TableHead class="w-20">编号</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead class="w-24 text-right">字数</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow v-for="c in analysis.chapters" :key="c.seq">
                  <TableCell class="text-muted-foreground">{{ c.seq }}</TableCell>
                  <TableCell>第{{ c.numStr }}章</TableCell>
                  <TableCell class="max-w-[360px] truncate" :title="c.title">{{ c.title || '—' }}</TableCell>
                  <TableCell class="text-right">{{ formatNumber(c.chars) }}</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
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
                  <Button variant="ghost" size="sm" @click="download(f.path, 'books')">
                    <Download class="h-3.5 w-3.5" />
                  </Button>
                  <Button v-if="inTauri" variant="ghost" size="sm" @click="reveal(f.path)">
                    <FolderOpen class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
      <CardFooter class="justify-between">
        <Button v-if="splitResult.zip_path" variant="outline" size="sm" @click="download(splitResult.zip_path!, 'books')">
          <Download class="h-4 w-4" />下载 zip
        </Button>
        <Button size="sm" @click="goNext">前往下一步（TTS 合成）<ArrowRight class="h-4 w-4" /></Button>
      </CardFooter>
    </Card>
  </div>
</template>
