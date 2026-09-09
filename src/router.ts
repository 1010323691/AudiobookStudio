import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'

// Hash history: works in a plain browser with no server-side route handling.
// All modules live under the persistent MainLayout so the left sidebar stays
// fixed while the workspace swaps.
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
          meta: { title: '开始' },
        },
        {
          path: 'text',
          name: 'text',
          component: () => import('@/views/TextFormat.vue'),
          meta: { title: '文本排版' },
        },
        {
          path: 'book',
          name: 'book',
          component: () => import('@/views/BookSplit.vue'),
          meta: { title: '分册切割' },
        },
        {
          path: 'script',
          name: 'script',
          component: () => import('@/views/ScriptParse.vue'),
          meta: { title: '文本解析' },
        },
        {
          path: 'voices',
          name: 'voices',
          component: () => import('@/views/Voices.vue'),
          meta: { title: '角色配音' },
        },
        {
          path: 'batch',
          name: 'batch',
          component: () => import('@/views/BatchTTS.vue'),
          meta: { title: '音频合成' },
        },
        {
          path: 'merge',
          name: 'merge',
          component: () => import('@/views/Merge.vue'),
          meta: { title: '音频合并' },
        },
        {
          path: 'audio',
          name: 'audio',
          component: () => import('@/views/AudioSplit.vue'),
          meta: { title: '音频分集' },
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
