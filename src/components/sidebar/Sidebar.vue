<script setup lang="ts">
import { useRoute } from 'vue-router'
import {
  LayoutDashboard,
  Type,
  ScanText,
  BookOpen,
  Mic,
  AudioLines,
  ListTodo,
  Settings,
  Headphones,
} from 'lucide-vue-next'
import { useAppStore } from '@/stores/app'
import { cn } from '@/lib/utils'

const route = useRoute()
const app = useAppStore()

const items = [
  { to: '/dashboard', label: '概览', icon: LayoutDashboard },
  { to: '/text', label: '文本排版', icon: Type },
  { to: '/script', label: '文本解析', icon: ScanText },
  { to: '/book', label: '分册切割', icon: BookOpen },
  { to: '/tts', label: 'TTS 合成', icon: Mic },
  { to: '/audio', label: '音频分集', icon: AudioLines },
  { to: '/tasks', label: '任务', icon: ListTodo },
  { to: '/settings', label: '设置', icon: Settings },
]

function isActive(to: string) {
  return route.path === to || (to !== '/dashboard' && route.path.startsWith(to))
}
</script>

<template>
  <aside class="flex w-60 shrink-0 flex-col border-r bg-card">
    <div class="flex h-14 items-center gap-2.5 border-b px-4">
      <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <Headphones class="h-5 w-5" />
      </div>
      <div class="leading-tight">
        <div class="text-sm font-semibold">有声书工作台</div>
        <div class="text-xs text-muted-foreground">Audiobook Studio</div>
      </div>
    </div>

    <nav class="flex-1 space-y-1 overflow-auto p-3">
      <RouterLink
        v-for="it in items"
        :key="it.to"
        :to="it.to"
        class="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
        :class="isActive(it.to) ? 'bg-accent text-accent-foreground' : 'text-muted-foreground'"
      >
        <component :is="it.icon" class="h-4 w-4 shrink-0" />
        {{ it.label }}
      </RouterLink>
    </nav>

    <div class="border-t p-3">
      <div class="flex items-center gap-2 text-xs" :class="app.backendUp ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground'">
        <span class="h-2 w-2 rounded-full" :class="app.backendUp ? 'bg-emerald-500' : 'bg-red-500'" />
        {{ app.backendUp ? '后端已连接' : '后端未连接' }}
      </div>
      <div v-if="app.lastError" class="mt-1 line-clamp-2 text-xs text-muted-foreground">
        {{ app.lastError }}
      </div>
    </div>
  </aside>
</template>
