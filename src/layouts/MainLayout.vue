<script setup lang="ts">
import Sidebar from '@/components/sidebar/Sidebar.vue'
</script>

<template>
  <div class="flex h-full overflow-hidden">
    <Sidebar />
    <main class="flex-1 overflow-auto bg-background">
      <div class="mx-auto max-w-6xl p-6">
        <!-- keep-alive: each module's inputs/toggles/preview survive navigation -->
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
