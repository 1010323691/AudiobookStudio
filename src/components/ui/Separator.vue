<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = withDefaults(defineProps<{ class?: string; orientation?: 'horizontal' | 'vertical' }>(), {
  orientation: 'horizontal',
})
</script>

<template>
  <div
    v-bind="$attrs"
    role="separator"
    :aria-orientation="orientation"
    :class="cn('shrink-0 bg-border', orientation === 'vertical' ? 'w-px' : 'h-px', props.class)"
  />
</template>
