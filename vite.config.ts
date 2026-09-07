import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite dev server + build config for the AudiobookStudio frontend.
//
// The app talks to the Python backend at http://127.0.0.1:8642. In the Tauri
// shell the WebView loads this app and calls the backend cross-origin (CORS is
// wide open on the backend); in a plain browser during dev the same absolute URL
// works, with a same-origin `/api` proxy kept here as a fallback.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      // Keep Vite's file watcher out of the Rust project. Its target/ dir
      // churns and holds locked .exe files while `cargo` builds, which makes
      // Vite crash with EBUSY on Windows. `tauri dev` watches the Rust side
      // itself; Vite only needs to watch the frontend.
      ignored: ['**/src-tauri/**'],
    },
    proxy: {
      '/api': { target: 'http://127.0.0.1:8642', changeOrigin: false },
    },
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
    sourcemap: false,
  },
})
