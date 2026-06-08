<script setup>
import { computed, ref } from 'vue'
import BookUpload from './components/BookUpload.vue'
import BookList from './components/BookList.vue'
import BookDetail from './components/BookDetail.vue'
import { useToast } from './composables/useToast.js'
import { useTheme } from './composables/useTheme.js'
import { usePolling } from './composables/usePolling.js'
import { useStateEvents } from './composables/useStateEvents.js'
import {
  getState,
  getBookDetail,
  setProfile,
  updateRuntimeConfig,
  enqueueBook,
  enqueueBooks,
  cancelQueuedBook,
  prioritizeQueuedBook,
  moveQueuedBook,
  deleteBook,
  deleteBooks,
  getAccessToken,
  setAccessToken
} from './api.js'

const { toast, success: toastSuccess, error: toastError } = useToast()
const { theme, toggle: toggleTheme } = useTheme()

const books = ref([])
const profiles = ref([{ name: 'auto', display_name: '自动识别' }])
const runtimeConfig = ref(null)
const configReady = ref(false)
const selectedBook = ref(null)
const selectedBookId = ref(null)
const loading = ref(true)
const initialUrlToken = new URLSearchParams(window.location.search).get('token') || ''
if (initialUrlToken) {
  setAccessToken(initialUrlToken)
  const cleanUrl = new URL(window.location.href)
  cleanUrl.searchParams.delete('token')
  window.history.replaceState({}, '', cleanUrl.toString())
}
const accessTokenInput = ref(getAccessToken())
const configForm = ref({
  max_workers: '',
  rpm_limit: '',
  tpm_limit: '',
  rate_limit_scope: 'auto',
  general_scan_max_chunks: '',
  general_scan_smart_density: true,
  general_scan_content_aware_sampling: true,
  general_scan_incremental_reuse: true,
  general_scan_writing_quality: true,
  general_scan_narrative_architecture: true,
  general_scan_foreshadowing_engineering: true,
  general_scan_semantic_layers: true,
  general_scan_reader_experience: true,
  general_scan_continuity_audit: true,
  general_scan_rolling_context: true,
  general_scan_knowledge_base_llm_merge: false,
  general_scan_context_max_chars: '1600',
  harem_plus_general_scan: false
})
const savingRuntimeConfig = ref(false)
const runtimeConfigDirty = ref(false)
const storageStatus = computed(() => {
  const storage = runtimeConfig.value?.web?.storage || {}
  const novelsWritable = storage.novels?.writable !== false
  const resultsWritable = storage.results?.writable !== false
  return {
    ok: novelsWritable && resultsWritable,
    label: novelsWritable && resultsWritable ? '正常' : '异常',
    title: [
      `novels: ${storage.novels?.path || '—'}${novelsWritable ? '' : ` (${storage.novels?.error || '不可写'})`}`,
      `results: ${storage.results?.path || '—'}${resultsWritable ? '' : ` (${storage.results?.error || '不可写'})`}`
    ].join('\n')
  }
})

// Race-condition guard for detail loading
let detailRequestId = 0

async function applyState(data) {
  books.value = data.books || []
  profiles.value = data.profiles || profiles.value
  runtimeConfig.value = data.config || runtimeConfig.value
  if (!runtimeConfigDirty.value) {
    syncConfigForm(runtimeConfig.value)
  }
  configReady.value = data.config_ready
  if (selectedBookId.value) {
    const found = (books.value || []).find((b) => b.id === selectedBookId.value)
    if (found) {
      await loadDetail(selectedBookId.value)
    } else {
      selectedBookId.value = null
      selectedBook.value = null
    }
  }
  loading.value = false
}

async function refresh() {
  try {
    const data = await getState()
    await applyState(data)
  } catch (e) {
    console.error('刷新失败:', e)
    if (String(e.message || '').includes('unauthorized')) {
      toastError('访问令牌无效或缺失')
    }
  } finally {
    loading.value = false
  }
}

async function saveAccessToken() {
  setAccessToken(accessTokenInput.value)
  toastSuccess(accessTokenInput.value.trim() ? '访问令牌已保存' : '访问令牌已清除')
  await refresh()
}

function syncConfigForm(config) {
  if (!config) return
  configForm.value = {
    max_workers: config.max_workers || '',
    rpm_limit: config.rpm_limit || '',
    tpm_limit: config.tpm_limit || '',
    rate_limit_scope: config.rate_limit_scope || 'auto',
    general_scan_max_chunks: config.general_scan_max_chunks || '80',
    general_scan_smart_density: config.general_scan_smart_density !== false,
    general_scan_content_aware_sampling: config.general_scan_content_aware_sampling !== false,
    general_scan_incremental_reuse: config.general_scan_incremental_reuse !== false,
    general_scan_writing_quality: config.general_scan_writing_quality !== false,
    general_scan_narrative_architecture: config.general_scan_narrative_architecture !== false,
    general_scan_foreshadowing_engineering: config.general_scan_foreshadowing_engineering !== false,
    general_scan_semantic_layers: config.general_scan_semantic_layers !== false,
    general_scan_reader_experience: config.general_scan_reader_experience !== false,
    general_scan_continuity_audit: config.general_scan_continuity_audit !== false,
    general_scan_rolling_context: config.general_scan_rolling_context !== false,
    general_scan_knowledge_base_llm_merge: Boolean(config.general_scan_knowledge_base_llm_merge),
    general_scan_context_max_chars: config.general_scan_context_max_chars || '1600',
    harem_plus_general_scan: Boolean(config.harem_plus_general_scan)
  }
}

async function saveRuntimeConfig() {
  savingRuntimeConfig.value = true
  try {
    const response = await updateRuntimeConfig({
      max_workers: configForm.value.max_workers,
      rpm_limit: configForm.value.rpm_limit,
      tpm_limit: configForm.value.tpm_limit,
      rate_limit_scope: configForm.value.rate_limit_scope,
      general_scan_max_chunks: configForm.value.general_scan_max_chunks,
      general_scan_smart_density: configForm.value.general_scan_smart_density,
      general_scan_content_aware_sampling: configForm.value.general_scan_content_aware_sampling,
      general_scan_incremental_reuse: configForm.value.general_scan_incremental_reuse,
      general_scan_writing_quality: configForm.value.general_scan_writing_quality,
      general_scan_narrative_architecture: configForm.value.general_scan_narrative_architecture,
      general_scan_foreshadowing_engineering:
        configForm.value.general_scan_foreshadowing_engineering,
      general_scan_semantic_layers: configForm.value.general_scan_semantic_layers,
      general_scan_reader_experience: configForm.value.general_scan_reader_experience,
      general_scan_continuity_audit: configForm.value.general_scan_continuity_audit,
      general_scan_rolling_context: configForm.value.general_scan_rolling_context,
      general_scan_knowledge_base_llm_merge: configForm.value.general_scan_knowledge_base_llm_merge,
      general_scan_context_max_chars: configForm.value.general_scan_context_max_chars,
      harem_plus_general_scan: configForm.value.harem_plus_general_scan
    })
    runtimeConfig.value = response.config || runtimeConfig.value
    runtimeConfigDirty.value = false
    syncConfigForm(runtimeConfig.value)
    toastSuccess('运行配置已更新')
    await refresh()
  } catch (e) {
    toastError('更新运行配置失败: ' + e.message)
  } finally {
    savingRuntimeConfig.value = false
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

async function handlePrioritize(bookId) {
  try {
    await prioritizeQueuedBook(bookId)
    toastSuccess('已置顶排队')
    await refresh()
  } catch (e) {
    toastError('置顶排队失败: ' + e.message)
  }
}

async function handleMoveQueued(bookId, direction) {
  try {
    await moveQueuedBook(bookId, direction)
    toastSuccess('已调整排队顺序')
    await refresh()
  } catch (e) {
    toastError('调整排队顺序失败: ' + e.message)
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

async function handleBatchDelete(bookIds) {
  try {
    const response = await deleteBooks(bookIds)
    const deleted = response.result?.deleted?.length || 0
    const skipped = response.result?.skipped?.length || 0
    if (deleted) {
      toastSuccess(skipped ? `已删除 ${deleted} 本，跳过 ${skipped} 本` : `已删除 ${deleted} 本`)
    } else {
      toastError(skipped ? `没有可删除的书籍，跳过 ${skipped} 本` : '没有可删除的书籍')
    }
    if ((response.result?.deleted || []).some((item) => item.book_id === selectedBookId.value)) {
      selectedBookId.value = null
      selectedBook.value = null
    }
    await refresh()
  } catch (e) {
    toastError('批量删除失败: ' + e.message)
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

const polling = usePolling(refresh, 3000, { autoStart: false })
useStateEvents(applyState, {
  onOpen: polling.stop,
  onFallback: polling.start
})
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
        <button
          class="theme-toggle"
          @click="toggleTheme"
          :title="theme === 'dark' ? '切换亮色模式' : '切换暗色模式'"
        >
          {{ theme === 'dark' ? '☀️' : '🌙' }}
        </button>
      </div>
    </div>
  </header>

  <div class="container card-wrap">
    <div class="banner warn" v-if="!configReady">
      <span>⚠️</span> API 配置未就绪：可以先上传和排队，但开始扫描前需要在 .env 或环境变量中配置
      API_KEY/API_KEY_POOL。
    </div>

    <div class="runtime-strip" v-if="runtimeConfig">
      <span class="runtime-item"><b>模型</b>{{ runtimeConfig.model_name || '—' }}</span>
      <span class="runtime-item"><b>并发</b>{{ runtimeConfig.max_workers || '—' }}</span>
      <span class="runtime-item"
        ><b>限流</b>{{ runtimeConfig.rpm_limit || '—' }} RPM /
        {{ runtimeConfig.tpm_limit || '—' }} TPM</span
      >
      <span class="runtime-item"
        ><b>Key</b
        >{{
          runtimeConfig.api_key_configured ? `${runtimeConfig.api_key_count || 1} 个` : '未配置'
        }}</span
      >
      <span class="runtime-item"
        ><b>上传上限</b
        >{{ Math.round((runtimeConfig.web?.max_upload_size || 0) / 1024 / 1024) || '—' }} MB</span
      >
      <span class="runtime-item"
        ><b>访问保护</b>{{ runtimeConfig.web?.auth_enabled ? '已开启' : '未开启' }}</span
      >
      <span class="runtime-item"
        ><b>Key校验</b
        >{{ runtimeConfig.web?.api_key_required_on_start ? '启动必需' : '允许跳过' }}</span
      >
      <span class="runtime-item" :class="{ danger: !storageStatus.ok }" :title="storageStatus.title"
        ><b>存储</b>{{ storageStatus.label }}</span
      >
    </div>

    <div class="access-token-row" v-if="!runtimeConfig || runtimeConfig.web?.auth_enabled">
      <input
        v-model="accessTokenInput"
        type="password"
        autocomplete="current-password"
        placeholder="Web 访问令牌"
        @keyup.enter="saveAccessToken"
      />
      <button class="token-save" @click="saveAccessToken">保存</button>
    </div>

    <div class="runtime-config-panel" v-if="runtimeConfig">
      <label>
        <span>并发</span>
        <input
          v-model="configForm.max_workers"
          type="number"
          min="1"
          max="64"
          @input="runtimeConfigDirty = true"
        />
      </label>
      <label>
        <span>RPM</span>
        <input
          v-model="configForm.rpm_limit"
          type="number"
          min="0"
          @input="runtimeConfigDirty = true"
        />
      </label>
      <label>
        <span>TPM</span>
        <input
          v-model="configForm.tpm_limit"
          type="number"
          min="0"
          @input="runtimeConfigDirty = true"
        />
      </label>
      <label>
        <span>限流域</span>
        <select v-model="configForm.rate_limit_scope" @change="runtimeConfigDirty = true">
          <option value="auto">auto</option>
          <option value="global">global</option>
          <option value="per_key">per_key</option>
        </select>
      </label>
      <label>
        <span>通用片段</span>
        <input
          v-model="configForm.general_scan_max_chunks"
          type="number"
          min="0"
          @input="runtimeConfigDirty = true"
        />
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_smart_density"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>智能密度</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_content_aware_sampling"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>内容抽样</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_incremental_reuse"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>增量复用</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_writing_quality"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>写作质量</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_narrative_architecture"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>叙事架构</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_foreshadowing_engineering"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>伏笔工程</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_semantic_layers"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>深层语义</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_reader_experience"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>读者体验</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_continuity_audit"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>一致性审计</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_rolling_context"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>滚动上下文</span>
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.general_scan_knowledge_base_llm_merge"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>知识库合并</span>
      </label>
      <label>
        <span>上下文</span>
        <input
          v-model="configForm.general_scan_context_max_chars"
          type="number"
          min="0"
          max="10000"
          @input="runtimeConfigDirty = true"
        />
      </label>
      <label class="runtime-toggle">
        <input
          v-model="configForm.harem_plus_general_scan"
          type="checkbox"
          @change="runtimeConfigDirty = true"
        />
        <span>后宫增强</span>
      </label>
      <button class="runtime-save" :disabled="savingRuntimeConfig" @click="saveRuntimeConfig">
        {{ savingRuntimeConfig ? '保存中...' : '保存运行配置' }}
      </button>
    </div>

    <BookUpload :profiles="profiles" @uploaded="handleUploaded" @error="toastError" />

    <div v-if="loading" class="skeleton-card">
      <div class="skeleton" style="height: 200px"></div>
    </div>

    <BookList
      v-else
      :books="books"
      :profiles="profiles"
      @scan="handleScan"
      @batchScan="handleBatchScan"
      @cancel="handleCancel"
      @prioritize="handlePrioritize"
      @moveQueued="handleMoveQueued"
      @delete="handleDelete"
      @batchDelete="handleBatchDelete"
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
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
body {
  font-family:
    'Inter',
    -apple-system,
    BlinkMacSystemFont,
    'Segoe UI',
    Roboto,
    sans-serif;
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
  background: rgba(255, 255, 255, 0.04);
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
header h1 {
  font-size: 1.9rem;
  font-weight: 700;
  letter-spacing: -0.8px;
}
header p {
  margin: 6px 0 0;
  opacity: 0.85;
  font-size: 0.95rem;
}

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
  background: rgba(255, 255, 255, 0.18);
  color: white;
  backdrop-filter: blur(4px);
}
.badge.ready {
  background: rgba(16, 185, 129, 0.25);
}

.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.15);
  border: none;
  cursor: pointer;
  font-size: 1.1rem;
  color: white;
  transition: background 0.15s;
}
.theme-toggle:hover {
  background: rgba(255, 255, 255, 0.25);
}

/* Card wrap */
.card-wrap {
  padding-bottom: 32px;
}

.runtime-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 16px;
}
.runtime-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 0.78rem;
  line-height: 1.2;
}
.runtime-item b {
  color: var(--text-heading);
  font-weight: 600;
}
.runtime-item.danger {
  border-color: var(--danger);
  background: var(--danger-bg);
  color: var(--danger-text);
}

.access-token-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 0 0 16px;
  max-width: 520px;
}
.access-token-row input {
  flex: 1;
  min-width: 0;
  height: 38px;
  padding: 8px 10px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-card);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.88rem;
}
.token-save {
  height: 38px;
  padding: 0 14px;
  border: 0;
  border-radius: var(--radius-xs);
  background: var(--primary);
  color: white;
  cursor: pointer;
  font: inherit;
  font-size: 0.88rem;
}
.token-save:hover {
  background: var(--primary-hover);
}

.runtime-config-panel {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  margin: 0 0 16px;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-card);
}
.runtime-config-panel label {
  display: grid;
  gap: 4px;
  min-width: 96px;
}
.runtime-config-panel label span {
  color: var(--text-secondary);
  font-size: 0.75rem;
  font-weight: 600;
}
.runtime-config-panel input,
.runtime-config-panel select {
  height: 34px;
  padding: 6px 8px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xs);
  background: var(--bg-card);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.84rem;
}
.runtime-toggle {
  grid-auto-flow: column;
  align-items: center;
  min-height: 34px;
}
.runtime-toggle input {
  width: 16px;
  height: 16px;
  accent-color: var(--primary);
}
.runtime-save {
  height: 34px;
  padding: 0 14px;
  border: 0;
  border-radius: var(--radius-xs);
  background: var(--primary);
  color: white;
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
}
.runtime-save:hover:not(:disabled) {
  background: var(--primary-hover);
}
.runtime-save:disabled {
  background: #d1d5db;
  color: #9ca3af;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  header {
    padding: 28px 16px 40px;
  }
  header h1 {
    font-size: 1.35rem;
  }
  .access-token-row {
    align-items: stretch;
  }
  .runtime-config-panel label,
  .runtime-save {
    flex: 1 1 140px;
  }
}
</style>
