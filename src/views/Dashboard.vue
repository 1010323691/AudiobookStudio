<script setup lang="ts">
import { computed } from 'vue'
import type { Component } from 'vue'
import { useRouter } from 'vue-router'
import { Type, BookOpen, Mic, AudioLines, ArrowRight, Circle, Check } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Badge from '@/components/ui/Badge.vue'
import { useProjectStore } from '@/stores/project'
import { formatNumber } from '@/utils/format'

type StageStatus = 'done' | 'pending' | 'soon'

interface Stage {
  to: string
  icon: Component
  title: string
  desc: string
  status: StageStatus
  badge: string
  badgeVariant: 'success' | 'secondary' | 'warning'
  summary: string | null
}

const router = useRouter()
const project = useProjectStore()

// basename of a (Windows or POSIX) path — for a compact "输出：xxx.txt" line.
const base = (p: string | null) => (p ? (p.split(/[\\/]/).pop() ?? p) : '')

const stages = computed<Stage[]>(() => {
  const textDone = !!project.textOutput
  const bookDone = project.bookOutputs.length > 0
  const audioDone = project.audioOutputs.length > 0
  return [
    {
      to: '/text',
      icon: Type,
      title: '文本排版',
      desc: '清理原文的标点、空行、断段与章节识别。',
      status: textDone ? 'done' : 'pending',
      badge: textDone ? '已完成' : '待处理',
      badgeVariant: textDone ? 'success' : 'secondary',
      summary:
        textDone && project.textResult
          ? `${base(project.textOutput)} · ${project.textResult.stats.chapters} 章 · ${formatNumber(
              project.textResult.stats.chars,
            )} 字`
          : null,
    },
    {
      to: '/book',
      icon: BookOpen,
      title: '分册切割',
      desc: '按章节把长文切成若干分册，文件名自动编号。',
      status: bookDone ? 'done' : 'pending',
      badge: bookDone ? '已完成' : '待处理',
      badgeVariant: bookDone ? 'success' : 'secondary',
      summary: project.bookResult ? `已切出 ${project.bookResult.file_count} 个分册` : null,
    },
    {
      to: '/tts',
      icon: Mic,
      title: 'TTS 合成',
      desc: '把文字合成为语音。',
      status: 'soon',
      badge: '即将推出',
      badgeVariant: 'warning',
      summary: '本版暂不可用，可先跳过。',
    },
    {
      to: '/audio',
      icon: AudioLines,
      title: '音频分集',
      desc: '把长音频无损切成若干集，支持停顿智能对齐。',
      status: audioDone ? 'done' : 'pending',
      badge: audioDone ? '已完成' : '待处理',
      badgeVariant: audioDone ? 'success' : 'secondary',
      summary: project.audioResult ? `已切出 ${project.audioResult.file_count} 集` : null,
    },
  ]
})

const next = computed(() => {
  if (!project.textOutput) return { to: '/text', title: '文本排版' }
  if (project.bookOutputs.length === 0) return { to: '/book', title: '分册切割' }
  // TTS is a placeholder this version — the actionable next step after book is audio.
  if (project.audioOutputs.length === 0) return { to: '/audio', title: '音频分集' }
  return null
})
</script>

<template>
  <div class="space-y-8">
    <header>
      <h1 class="text-2xl font-bold tracking-tight">概览</h1>
      <p class="mt-1 text-muted-foreground">
        小说原文 → 最终有声书音频，一个窗口走完整个流程。
      </p>
    </header>

    <!-- 下一步 / 全部完成 -->
    <section
      v-if="next"
      class="flex flex-col gap-3 rounded-xl border border-primary/40 bg-primary/5 p-5 sm:flex-row sm:items-center sm:justify-between"
    >
      <div>
        <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">下一步</p>
        <h2 class="mt-1 text-lg font-semibold">{{ next.title }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">从这里继续你的有声书制作流程。</p>
      </div>
      <Button size="lg" class="shrink-0" @click="router.push(next.to)">
        前往{{ next.title }}
        <ArrowRight class="h-4 w-4" />
      </Button>
    </section>

    <section
      v-else
      class="flex items-center gap-3 rounded-xl border border-emerald-500/40 bg-emerald-500/10 p-5"
    >
      <Check class="h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
      <div>
        <h2 class="text-base font-semibold">流程已完成</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          文本、分册与音频分集都已处理完毕，可随时回到任一模块重跑。
        </p>
      </div>
    </section>

    <!-- 模块卡片（含实时状态） -->
    <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <div
        v-for="(s, i) in stages"
        :key="s.to"
        class="flex flex-col rounded-xl border bg-card p-5 shadow-sm"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-accent text-accent-foreground">
              <component :is="s.icon" class="h-5 w-5" />
            </div>
            <div class="flex items-center gap-2">
              <span
                class="flex h-5 w-5 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground"
              >
                {{ i + 1 }}
              </span>
              <h2 class="text-base font-semibold">{{ s.title }}</h2>
            </div>
          </div>
          <Badge :variant="s.badgeVariant">{{ s.badge }}</Badge>
        </div>
        <p class="mt-3 text-sm text-muted-foreground">{{ s.desc }}</p>
        <p
          v-if="s.summary"
          class="mt-2 rounded-md bg-accent/50 px-2 py-1 font-mono text-xs text-foreground"
        >
          {{ s.summary }}
        </p>
        <div class="flex-1" />
        <Button :variant="s.status === 'done' ? 'outline' : 'default'" class="mt-4" @click="router.push(s.to)">
          <span v-if="s.status === 'done'">再次打开</span>
          <span v-else>进入模块</span>
          <ArrowRight class="h-4 w-4" />
        </Button>
      </div>
    </div>

    <section class="rounded-xl border bg-card p-5">
      <h3 class="flex items-center gap-2 text-sm font-semibold">
        <Circle class="h-4 w-4 text-primary" />
        流程衔接
      </h3>
      <p class="mt-2 text-sm text-muted-foreground">
        每完成一步，点击「前往下一步」即可把输出文件自动带入下一个模块，全程无需手动搬文件。上方卡片会实时显示各模块的完成状态。
      </p>
    </section>
  </div>
</template>
