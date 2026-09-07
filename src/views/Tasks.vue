<script setup lang="ts">
import { computed, onMounted, reactive } from 'vue'
import { useTaskStore } from '@/stores/task'
import { useAppStore } from '@/stores/app'
import type { TaskSnapshot, TaskStatus } from '@/types'
import { formatClock } from '@/utils/format'

import Button from '@/components/ui/Button.vue'
import Badge from '@/components/ui/Badge.vue'
import Progress from '@/components/ui/Progress.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import Alert from '@/components/ui/Alert.vue'
import {
  ListTodo,
  RefreshCw,
  Pause,
  Play,
  XCircle,
  RotateCw,
  ChevronDown,
  Type,
  BookOpen,
  Mic,
  AudioLines,
} from 'lucide-vue-next'

const taskStore = useTaskStore()
const app = useAppStore()

const expanded = reactive(new Set<string>())

const STATUS_META: Record<
  TaskStatus,
  { label: string; variant: 'default' | 'secondary' | 'destructive' | 'success' | 'warning' }
> = {
  pending: { label: '等待中', variant: 'secondary' },
  running: { label: '运行中', variant: 'default' },
  paused: { label: '已暂停', variant: 'warning' },
  cancelled: { label: '已取消', variant: 'secondary' },
  succeeded: { label: '已完成', variant: 'success' },
  failed: { label: '失败', variant: 'destructive' },
}

const MODULE_LABEL: Record<string, string> = {
  text: '文本排版',
  book: '分册切割',
  audio: '音频分集',
  tts: 'TTS 合成',
}
const MODULE_ICON: Record<string, any> = {
  text: Type,
  book: BookOpen,
  audio: AudioLines,
  tts: Mic,
}
const moduleIcon = (m: string) => MODULE_ICON[m] ?? ListTodo

const active = computed(() =>
  taskStore.tasks.filter((t) => ['pending', 'running', 'paused'].includes(t.status)),
)

function toggle(id: string) {
  if (expanded.has(id)) expanded.delete(id)
  else expanded.add(id)
}

function control(t: TaskSnapshot, action: 'pause' | 'resume' | 'cancel' | 'retry') {
  taskStore.control(t.id, action)
}

const logColor = (level: string) =>
  level === 'ERROR' ? 'text-destructive' : level === 'WARNING' ? 'text-amber-500' : 'text-muted-foreground'

onMounted(() => {
  const first = active.value[0]
  if (first) toggle(first.id)
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">任务中心</h1>
        <p class="mt-1 text-muted-foreground">
          统一异步任务：进度、日志与开始 / 暂停 / 取消 / 重试。进行中的任务实时刷新。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <Button v-if="!app.backendUp" variant="outline" size="sm" @click="app.ping()">
          <RefreshCw class="h-4 w-4" />重新连接
        </Button>
        <Button variant="outline" size="sm" @click="taskStore.refresh()">
          <RefreshCw class="h-4 w-4" />刷新
        </Button>
      </div>
    </div>

    <Alert v-if="!app.backendUp" variant="destructive">
      后端未连接（{{ app.lastError || '无法连接 127.0.0.1:8642' }}）。请确认 Python 后端已启动。
    </Alert>

    <Alert v-else-if="!taskStore.tasks.length" variant="default">
      <ListTodo class="h-4 w-4 shrink-0" />
      <span>
        暂无任务。运行「音频分集」的停顿检测 / 切割等长任务后，会出现在这里并实时刷新进度与日志。
      </span>
    </Alert>

    <div v-else class="space-y-3">
      <div v-for="t in taskStore.tasks" :key="t.id" class="rounded-xl border bg-card shadow-sm">
        <button class="flex w-full items-center gap-3 p-4 text-left" @click="toggle(t.id)">
          <component :is="moduleIcon(t.module)" class="h-5 w-5 shrink-0 text-muted-foreground" />
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm font-medium">{{ t.label }}</div>
            <div class="text-xs text-muted-foreground">
              {{ MODULE_LABEL[t.module] || t.module }} · {{ formatClock(t.created) }}
            </div>
          </div>
          <Badge :variant="STATUS_META[t.status].variant">{{ STATUS_META[t.status].label }}</Badge>
          <ChevronDown
            class="h-4 w-4 shrink-0 text-muted-foreground transition-transform"
            :class="{ 'rotate-180': expanded.has(t.id) }"
          />
        </button>

        <div v-if="expanded.has(t.id)" class="space-y-3 border-t px-4 pb-4 pt-3">
          <div class="flex items-center gap-3">
            <Progress :value="t.progress" class="flex-1" />
            <span class="w-10 text-right text-xs text-muted-foreground">{{ Math.round(t.progress * 100) }}%</span>
          </div>
          <p v-if="t.current" class="text-xs text-muted-foreground">当前：{{ t.current }}</p>
          <p v-if="t.error" class="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{{ t.error }}</p>

          <div class="mb-1 text-xs text-muted-foreground">日志</div>
          <ScrollArea class="h-56 rounded-md bg-muted/40">
            <div class="p-3 font-mono text-xs leading-relaxed">
              <div v-for="(l, i) in t.logs" :key="i" :class="logColor(l.level)">
                <span class="text-muted-foreground/60">{{ formatClock(l.t) }}</span>
                <span class="mx-1.5 font-semibold">{{ l.level }}</span>
                {{ l.msg }}
              </div>
              <div v-if="!t.logs.length" class="text-muted-foreground/60">（暂无日志）</div>
            </div>
          </ScrollArea>

          <div class="flex flex-wrap gap-2 pt-1">
            <Button v-if="t.status === 'running'" variant="outline" size="sm" @click="control(t, 'pause')">
              <Pause class="h-4 w-4" />暂停
            </Button>
            <Button v-if="t.status === 'paused'" variant="outline" size="sm" @click="control(t, 'resume')">
              <Play class="h-4 w-4" />继续
            </Button>
            <Button
              v-if="['running', 'paused', 'pending'].includes(t.status)"
              variant="outline"
              size="sm"
              @click="control(t, 'cancel')"
            >
              <XCircle class="h-4 w-4" />取消
            </Button>
            <Button
              v-if="['succeeded', 'failed', 'cancelled'].includes(t.status)"
              variant="outline"
              size="sm"
              @click="control(t, 'retry')"
            >
              <RotateCw class="h-4 w-4" />重试
            </Button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
