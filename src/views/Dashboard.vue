<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Folder, FolderOpen, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Badge from '@/components/ui/Badge.vue'
import Input from '@/components/ui/Input.vue'
import { useSettingsStore } from '@/stores/settings'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { getWorkspace, setWorkspace } from '@/api/workspace'
import { useToast } from '@/components/ui/toast'
import type { WorkspaceInfo } from '@/types'

const settings = useSettingsStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

// ------------------------------ 工作空间 ------------------------------
const ws = ref<WorkspaceInfo | null>(null)
const manualPath = ref('')
const wsBusy = ref(false)

const WS_DIR_LABELS: [string, string][] = [
  ['00_temp', '临时文件'],
  ['01_input', '原始输入 / 排版文本'],
  ['02_split_text', '分册切割结果'],
  ['03_parsed_json', '文本解析 JSON'],
  ['04_voice_profiles', '角色配音配置'],
  ['05_audio_chunk', '音频合成片段'],
  ['06_audio_merge', '音频合并成品'],
  ['07_output', '最终分集'],
]

const wsPath = computed(() => ws.value?.path || settings.config?.paths?.working_dir || '')

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    ws.value = await getWorkspace()
  } catch {
    /* 后端未启动 —— 保持 null */
  }
})

async function applyWorkspace(path: string) {
  const p = path.trim()
  if (!p || wsBusy.value) return
  wsBusy.value = true
  try {
    ws.value = await setWorkspace(p)
    await settings.load() // 刷新全局配置 → 解除全站锁定门
    manualPath.value = ''
    toast({ title: '工作空间已设置', variant: 'success', description: ws.value.path })
  } catch (e: any) {
    toast({ title: '设置工作空间失败', variant: 'destructive', description: e?.message || String(e) })
  } finally {
    wsBusy.value = false
  }
}

async function clearWorkspace() {
  if (wsBusy.value) return
  wsBusy.value = true
  try {
    ws.value = await setWorkspace('')
    await settings.load()
    toast({ title: '工作空间已清除', description: '流水线已重新锁定' })
  } catch (e: any) {
    toast({ title: '清除失败', variant: 'destructive', description: e?.message || String(e) })
  } finally {
    wsBusy.value = false
  }
}

</script>

<template>
  <div class="space-y-8">
    <header>
      <h1 class="text-2xl font-bold tracking-tight">开始</h1>
      <p class="mt-1 text-muted-foreground">
        小说原文 → 最终有声书音频，一个窗口走完整个流程。
      </p>
    </header>

    <!-- 工作空间（流程运行的前提） -->
    <section
      class="rounded-xl border p-5"
      :class="workspaceSet ? 'border-primary/40 bg-primary/5' : 'border-amber-500/60 bg-amber-500/10'"
    >
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="flex items-center gap-2 text-base font-semibold">
          <Folder class="h-5 w-5 text-primary" />
          工作空间
          <Badge :variant="workspaceSet ? 'success' : 'warning'">
            {{ workspaceSet ? '已设置' : '未设置' }}
          </Badge>
        </h2>
        <div v-if="workspaceSet" class="flex gap-2">
          <Button variant="outline" size="sm" :disabled="wsBusy" @click="clearWorkspace">
            <Trash2 class="h-4 w-4" />清除
          </Button>
        </div>
      </div>

      <p
        v-if="!workspaceSet"
        class="mt-3 text-sm font-medium text-amber-700 dark:text-amber-400"
      >
        尚未设置工作空间 —— 流水线已锁定，请先选择一个本地文件夹。
      </p>
      <p v-else class="mt-3 text-sm text-muted-foreground">
        流水线的所有产物都会按固定结构保存在该目录下。
      </p>

      <!-- 设置 / 更换 -->
      <div class="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          v-model="manualPath"
          class="min-w-0 flex-1 font-mono text-sm"
          placeholder="请输入文件夹路径，如 D:\AudiobookProjects\MyBook"
          :disabled="wsBusy"
        />
        <Button
          class="shrink-0"
          :disabled="wsBusy || !manualPath.trim()"
          @click="applyWorkspace(manualPath)"
        >
          {{ wsBusy ? '处理中…' : workspaceSet ? '更新' : '确认设置' }}
        </Button>
      </div>
      <p class="mt-2 text-xs text-muted-foreground">
        请手动输入工作空间文件夹的完整路径。
      </p>

      <!-- 当前路径 + 子目录结构 -->
      <template v-if="workspaceSet">
        <div class="mt-4 flex items-center gap-2 rounded-md bg-accent/50 px-3 py-2">
          <FolderOpen class="h-4 w-4 shrink-0 text-muted-foreground" />
          <span class="break-all font-mono text-sm" :title="wsPath">{{ wsPath }}</span>
        </div>
        <dl v-if="ws" class="mt-3 grid grid-cols-1 gap-x-8 gap-y-1 sm:grid-cols-2">
          <div v-for="[name, label] in WS_DIR_LABELS" :key="name" class="flex items-baseline gap-2 text-xs">
            <dt class="w-32 shrink-0 font-mono text-foreground">{{ name }}/</dt>
            <dd class="text-muted-foreground">{{ label }}</dd>
          </div>
        </dl>
      </template>
    </section>
  </div>
</template>
