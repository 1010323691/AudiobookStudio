import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'

// Hash history: works both in the Tauri WebView and a plain browser with no
// server-side route handling. All seven modules live under the persistent
// MainLayout so the left sidebar stays fixed while the workspace swaps.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', redirect: '/dashboard' },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '概览' },
        },
        {
          path: 'text',
          name: 'text',
          component: () => import('@/views/TextFormat.vue'),
          meta: { title: '文本排版' },
        },
        {
          path: 'script',
          name: 'script',
          component: () => import('@/views/ScriptParse.vue'),
          meta: { title: '文本解析' },
        },
        {
          path: 'book',
          name: 'book',
          component: () => import('@/views/BookSplit.vue'),
          meta: { title: '分册切割' },
        },
        {
          path: 'tts',
          name: 'tts',
          component: () => import('@/views/TTS.vue'),
          meta: { title: 'TTS 合成' },
        },
        {
          path: 'audio',
          name: 'audio',
          component: () => import('@/views/AudioSplit.vue'),
          meta: { title: '音频分集' },
        },
        {
          path: 'tasks',
          name: 'tasks',
          component: () => import('@/views/Tasks.vue'),
          meta: { title: '任务' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/Settings.vue'),
          meta: { title: '设置' },
        },
      ],
    },
  ],
})

export default router
