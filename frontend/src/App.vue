<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import BookUpload from './components/BookUpload.vue'
import BookList from './components/BookList.vue'
import BookDetail from './components/BookDetail.vue'
import { getState, getBookDetail, setProfile, enqueueBook } from './api.js'

const books = ref([])
const tasks = ref([])
const profiles = ref([{ name: 'auto', display_name: '自动识别' }])
const configReady = ref(false)
const selectedBook = ref(null)
const selectedBookId = ref(null)
const loading = ref(true)
const toast = ref(null)
let timer = null

function showToast(message, type = 'info') {
  toast.value = { message, type }
  setTimeout(() => { toast.value = null }, 3000)
}

async function refresh() {
  try {
    const data = await getState()
    books.value = data.books || []
    tasks.value = data.tasks || []
    profiles.value = data.profiles || profiles.value
    configReady.value = data.config_ready
    if (selectedBookId.value) {
      const found = books.value.find(b => b.id === selectedBookId.value)
      if (found) {
        await loadDetail(selectedBookId.value)
      } else {
        selectedBookId.value = null
        selectedBook.value = null
      }
    }
  } catch (e) {
    console.error('刷新失败:', e)
  } finally {
    loading.value = false
  }
}

async function loadDetail(bookId) {
  selectedBookId.value = bookId
  try {
    selectedBook.value = await getBookDetail(bookId)
  } catch (e) {
    showToast('加载详情失败: ' + e.message, 'error')
  }
}

async function handleScan(bookId) {
  try {
    const result = await enqueueBook(bookId)
    if (result.ok) {
      showToast('已加入扫描队列', 'success')
      await refresh()
    }
  } catch (e) {
    showToast('加入队列失败: ' + e.message, 'error')
  }
}

async function handleProfileChange(bookId, profile) {
  try {
    await setProfile(bookId, profile)
    showToast('分类已更新', 'success')
    await refresh()
  } catch (e) {
    showToast('更新分类失败: ' + e.message, 'error')
  }
}

async function handleUploaded() {
  showToast('上传成功', 'success')
  await refresh()
}

function startPolling() {
  if (timer || document.hidden) return
  timer = setInterval(refresh, 3000)
}

function stopPolling() {
  if (!timer) return
  clearInterval(timer)
  timer = null
}

function handleVisibilityChange() {
  if (document.hidden) {
    stopPolling()
  } else {
    refresh()
    startPolling()
  }
}

onMounted(() => {
  refresh()
  startPolling()
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <header>
    <div class="container">
      <div>
        <h1>📚 NovelReportScanner</h1>
        <p>小说扫书分析工具 — 上传、分类、扫描、报告一站式管理</p>
      </div>
      <div class="badge" :class="{ ready: configReady }">
        {{ configReady ? '✅ 配置就绪' : '⚠️ 配置未就绪' }}
      </div>
    </div>
  </header>

  <div class="container card-wrap">
    <div class="banner warn" v-if="!configReady">
      <span>⚠️</span> API 配置未就绪：可以先上传和排队，但开始扫描前需要在 api.txt 中写入可用 API Key。
    </div>

    <BookUpload
      :profiles="profiles"
      @uploaded="handleUploaded"
      @error="showToast($event, 'error')"
    />

    <div v-if="loading" class="skeleton-card">
      <div class="skeleton" style="height:200px"></div>
    </div>

    <BookList
      v-else
      :books="books"
      :profiles="profiles"
      @scan="handleScan"
      @detail="loadDetail"
      @profileChange="handleProfileChange"
    />

    <BookDetail :book="selectedBook" />
  </div>

  <div class="toast-container" v-if="toast">
    <div class="toast" :class="toast.type">
      {{ toast.message }}
    </div>
  </div>
</template>

<style>
/* Global styles */
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f8f9fb; color: #1f2937; line-height: 1.6; min-height: 100vh;
}
.container { max-width: 1280px; margin: 0 auto; padding: 0 24px; }

/* Header */
header {
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
  color: white; padding: 36px 24px 48px; margin-bottom: -32px;
  position: relative; overflow: hidden;
}
header::before {
  content: ''; position: absolute; top: -50%; right: -10%;
  width: 600px; height: 600px;
  background: rgba(255,255,255,0.04); border-radius: 50%; pointer-events: none;
}
header .container {
  display: flex; align-items: flex-start; justify-content: space-between;
  flex-wrap: wrap; gap: 16px; position: relative; z-index: 1;
}
header h1 { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.8px; }
header p { margin: 6px 0 0; opacity: 0.85; font-size: 0.95rem; }
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  background: rgba(255,255,255,0.18); color: white; backdrop-filter: blur(4px);
}
.badge.ready { background: rgba(16,185,129,0.25); }

/* Card wrap */
.card-wrap { padding-bottom: 32px; }

/* Banner */
.banner {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 18px; border-radius: 10px; margin-bottom: 20px;
  font-size: 0.9rem; font-weight: 500;
}
.banner.warn { background: #fffbeb; color: #92400e; border: 1px solid #fcd34d; }

/* Skeleton */
.skeleton-card {
  background: #fff; border-radius: 14px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04);
  padding: 28px; margin-bottom: 20px;
  border: 1px solid #e5e7eb;
}
.skeleton {
  background: linear-gradient(90deg, #f3f4f6 25%, #e5e7eb 50%, #f3f4f6 75%);
  background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 8px;
}
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

/* Toast */
.toast-container {
  position: fixed; bottom: 24px; right: 24px; z-index: 1000;
}
.toast {
  background: #fff; color: #1f2937; padding: 14px 22px; border-radius: 10px;
  box-shadow: 0 10px 25px rgba(0,0,0,0.1), 0 4px 10px rgba(0,0,0,0.06);
  border: 1px solid #e5e7eb; font-size: 0.88rem; font-weight: 500;
  display: flex; align-items: center; gap: 10px;
  animation: slideIn 0.3s ease;
  max-width: 360px;
}
.toast.success { border-left: 4px solid #10b981; }
.toast.error { border-left: 4px solid #ef4444; }
.toast.info { border-left: 4px solid #3b82f6; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

@media (max-width: 768px) {
  header { padding: 28px 16px 40px; }
  header h1 { font-size: 1.35rem; }
  .container { padding: 0 16px; }
  .toast-container { left: 16px; right: 16px; bottom: 16px; }
  .toast { max-width: 100%; }
}
</style>
