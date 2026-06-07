import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  root: '.',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    rollupOptions: {
      input: resolve(__dirname, 'index.html')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8765',
      '/files': 'http://localhost:8765',
      '/upload': 'http://localhost:8765',
      '/healthz': 'http://localhost:8765'
    }
  }
})
