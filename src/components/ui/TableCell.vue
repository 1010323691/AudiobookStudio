<script setup lang="ts">
import { cn } from '@/lib/utils'

const props = defineProps<{ class?: string }>()
</script>

<template>
  <td v-bind="$attrs" :class="cn('p-2 align-middle', props.class)">
    <slot />
  </td>
</template>
