<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from '@/components/ui/toast'
import { ttsStatus } from '@/api/tts'
import type { TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import { Mic, ArrowRight, ArrowLeft, Hourglass } from 'lucide-vue-next'

const router = useRouter()
const { push: toast } = useToast()
const status = ref<TTSStatus | null>(null)

onMounted(async () => {
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
})

function goBack() {
  router.push('/book')
}
// The audio module is independent (it takes any audio file), so navigation to
// it stays available even while TTS itself is a placeholder.
function goNext() {
  if (!status.value?.implemented) {
    toast({ title: 'TTS 尚未实装', variant: 'destructive', description: '本模块为占位，即将推出。' })
  }
  router.push('/audio')
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        TTS 合成
        <Badge variant="secondary">即将推出</Badge>
      </h1>
      <p class="mt-1 text-muted-foreground">本模块规划中，用于把分册文本合成为有声书音频。</p>
    </div>

    <Card>
      <CardHeader class="items-center text-center">
        <div class="mx-auto mb-3 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
          <Mic class="h-8 w-8 text-muted-foreground" />
        </div>
        <CardTitle class="justify-center">即将推出</CardTitle>
        <CardDescription class="px-8">{{ status?.message ?? 'TTS 合成即将推出' }}</CardDescription>
      </CardHeader>
      <CardContent class="flex flex-col items-center gap-4">
        <Alert variant="default" class="max-w-md text-center">
          <Hourglass class="h-4 w-4 shrink-0" />
          <span>该模块的结构与配置（引擎、路由、<code class="text-xs">config.tts</code> 分区）已就位，实装后即可直接投入使用，无需重构。</span>
        </Alert>
        <div class="flex gap-2">
          <Button variant="outline" @click="goBack">
            <ArrowLeft class="h-4 w-4" />返回分册切割
          </Button>
          <Button @click="goNext">前往下一步（音频分集）<ArrowRight class="h-4 w-4" /></Button>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
