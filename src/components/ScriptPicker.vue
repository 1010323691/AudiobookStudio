<script setup lang="ts">
/**
 * Picker for a parsed script JSON (a file name in ``03_parsed_json/``). Shared by the
 * downstream 角色配音 and 音频合成 pages so both operate on the same file (they both
 * bind to ``project.activeScript``). The backend does the listing (``GET /api/files/
 * list/03_parsed_json``); this only shows the names and echoes the chosen one up via
 * ``v-model``. An empty selection means "the most recently written JSON" (the backend
 * fallback), so a fresh parse is picked up without the user having to re-select.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { listDir } from '@/api/files'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import Label from '@/components/ui/Label.vue'
import Select from '@/components/ui/Select.vue'
import Button from '@/components/ui/Button.vue'
import { RefreshCw } from 'lucide-vue-next'

const props = defineProps<{
  /** The selected file name (or '' for "most recent"). */
  modelValue?: string
  /** Directory to list; defaults to ``03_parsed_json``. */
  module?: string
  /** Field label; defaults to ``解析 JSON``. */
  label?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const { workspaceSet } = useWorkspaceGate()
const dir = computed(() => props.module || '03_parsed_json')
const label = computed(() => props.label || '解析 JSON')

const options = ref<string[]>([])
const loading = ref(false)

async function load() {
  if (!workspaceSet.value) {
    options.value = []
    return
  }
  loading.value = true
  try {
    const r = await listDir(dir.value)
    // Show only the base ``<stem>.json`` files: a ``<stem>_checked.json`` is a derived
    // copy of its base, so the backend's ``resolve_parsed_json`` transparently upgrades
    // any base selection to the ``_checked`` variant (when it exists). Listing both
    // would show a confusing duplicate per chapter.
    options.value = r.items
      .filter(
        (i) =>
          !i.is_dir &&
          i.name.toLowerCase().endsWith('.json') &&
          !i.name.toLowerCase().endsWith('_checked.json'),
      )
      .map((i) => i.name)
  } catch {
    options.value = []
  } finally {
    loading.value = false
  }
}

function onInput(v: string | number) {
  emit('update:modelValue', String(v))
}

onMounted(load)
// Re-list once the workspace appears / is cleared (the list is empty until then).
watch(workspaceSet, load)
</script>

<template>
  <div v-if="workspaceSet" class="space-y-1.5">
    <Label>{{ label }}</Label>
    <div class="flex items-center gap-2">
      <Select :model-value="modelValue" @update:model-value="onInput">
        <option value="">（最近修改）</option>
        <option v-for="n in options" :key="n" :value="n">{{ n }}</option>
      </Select>
      <Button variant="outline" size="sm" :disabled="loading" @click="load">
        <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
      </Button>
    </div>
    <p v-if="!options.length" class="text-xs text-muted-foreground">
      {{ dir }}/ 下暂无 JSON——请先在「文本解析」生成脚本。
    </p>
  </div>
</template>
