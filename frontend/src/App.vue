<script setup>
import { ref, onMounted } from 'vue'
import BookUpload from './components/BookUpload.vue'
import BookList from './components/BookList.vue'
import BookDetail from './components/BookDetail.vue'
import { useToast } from './composables/useToast.js'
import { useTheme } from './composables/useTheme.js'
import { usePolling } from './composables/usePolling.js'
import { getState, getBookDetail, setProfile, enqueueBook, enqueueBooks, cancelQueuedBook, deleteBook } from './api.js'

const { toast, success: toastSuccess, error: toastError } = useToast()
const { theme, toggle: toggleTheme } = useTheme()

const books = ref([])
const profiles = ref([{ name: 'auto', display_name: '自动识别' }])
const configReady = ref(false)
const selectedBook = ref(null)
const selectedBookId = ref(null)
const loading = ref(true)

// Race-condition guard for detail loading
let detailRequestId = 0

async function refresh() {
  try {
    const data = await getState()
    books.value = data.books || []
    profiles.value = data.profiles || profiles.value
    configReady.value = data.config_ready
    if (selectedBookId.value) {
      const found = (books.value || []).find(b => b.id === selectedBookId.value)
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
  const reqId = ++detailRequestId
  selectedBookId.value = bookId
  try {
    const detail = await getBookDetail(bookId)
    if (reqId === detailRequestId) {
      selectedBook.value = detail
    }
  } catch (e) {
    if (reqId === detailRequestId) {
      toastError('加载详情失败: ' + e.message)
    }
  }
}

async function handleScan(bookId) {
  try {
    const result = await enqueueBook(bookId)
    if (result.ok) {
      toastSuccess('已加入扫描队列')
      await refresh()
    }
  } catch (e) {
    toastError('加入队列失败: ' + e.message)
  }
}

async function handleBatchScan(bookIds) {
  try {
    const response = await enqueueBooks(bookIds)
    const queued = response.result?.queued?.length || 0
    const skipped = response.result?.skipped?.length || 0
    if (queued) {
      toastSuccess(skipped ? `已加入 ${queued} 本，跳过 ${skipped} 本` : `已加入 ${queued} 本`)
    } else {
      toastError(skipped ? `没有可加入的书籍，跳过 ${skipped} 本` : '没有可加入的书籍')
    }
    await refresh()
  } catch (e) {
    toastError('批量加入失败: ' + e.message)
  }
}

async function handleCancel(bookId) {
  try {
    await cancelQueuedBook(bookId)
    toastSuccess('已取消排队')
    await refresh()
  } catch (e) {
    toastError('取消排队失败: ' + e.message)
  }
}

async function handleDelete(bookId) {
  try {
    await deleteBook(bookId)
    toastSuccess('已删除书籍')
    if (selectedBookId.value === bookId) {
      selectedBookId.value = null
      selectedBook.value = null
    }
    await refresh()
  } catch (e) {
    toastError('删除书籍失败: ' + e.message)
  }
}

async function handleProfileChange(bookId, profile) {
  try {
    await setProfile(bookId, profile)
    toastSuccess('分类已更新')
    await refresh()
  } catch (e) {
    toastError('更新分类失败: ' + e.message)
  }
}

async function handleUploaded() {
  toastSuccess('上传成功')
  await refresh()
}

usePolling(refresh, 3000)
</script>

<template>
  <header>
    <div class="container">
      <div>
        <h1>📚 NovelReportScanner</h1>
        <p>小说扫书分析工具 — 上传、分类、扫描、报告一站式管理</p>
      </div>
      <div class="header-actions">
        <div class="badge" :class="{ ready: configReady }">
          {{ configReady ? '✅ 配置就绪' : '⚠️ 配置未就绪' }}
        </div>
        <button class="theme-toggle" @click="toggleTheme" :title="theme === 'dark' ? '切换亮色模式' : '切换暗色模式'">
          {{ theme === 'dark' ? '☀️' : '🌙' }}
        </button>
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
      @error="toastError"
    />

    <div v-if="loading" class="skeleton-card">
      <div class="skeleton" style="height:200px"></div>
    </div>

    <BookList
      v-else
      :books="books"
      :profiles="profiles"
      @scan="handleScan"
      @batchScan="handleBatchScan"
      @cancel="handleCancel"
      @delete="handleDelete"
      @detail="loadDetail"
      @profileChange="handleProfileChange"
    />

    <BookDetail :book="selectedBook" :profiles="profiles" />
  </div>

  <div class="toast-container" v-if="toast">
    <div class="toast" :class="toast.type">
      {{ toast.message }}
    </div>
  </div>
</template>

<style>
/* Global layout */
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6;
  min-height: 100vh;
}

/* Header */
header {
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
  color: white;
  padding: 36px 24px 48px;
  margin-bottom: -32px;
  position: relative;
  overflow: hidden;
}
header::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -10%;
  width: 600px;
  height: 600px;
  background: rgba(255,255,255,0.04);
  border-radius: 50%;
  pointer-events: none;
}
header .container {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  position: relative;
  z-index: 1;
}
header h1 { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.8px; }
header p { margin: 6px 0 0; opacity: 0.85; font-size: 0.95rem; }

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
  background: rgba(255,255,255,0.18);
  color: white;
  backdrop-filter: blur(4px);
}
.badge.ready { background: rgba(16,185,129,0.25); }

.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: rgba(255,255,255,0.15);
  border: none;
  cursor: pointer;
  font-size: 1.1rem;
  color: white;
  transition: background 0.15s;
}
.theme-toggle:hover { background: rgba(255,255,255,0.25); }

/* Card wrap */
.card-wrap { padding-bottom: 32px; }

@media (max-width: 768px) {
  header { padding: 28px 16px 40px; }
  header h1 { font-size: 1.35rem; }
}
</style>
