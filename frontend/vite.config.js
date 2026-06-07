import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { fileURLToPath } from 'url'

const PROXY_TARGET = process.env.API_PROXY || 'http://localhost:8765'
const ROOT_DIR = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  plugins: [vue()],
  root: '.',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    rollupOptions: {
      input: resolve(ROOT_DIR, 'index.html')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: PROXY_TARGET, changeOrigin: true },
      '/files': { target: PROXY_TARGET, changeOrigin: true },
      '/upload': { target: PROXY_TARGET, changeOrigin: true },
      '/healthz': { target: PROXY_TARGET, changeOrigin: true }
    }
  }
})
